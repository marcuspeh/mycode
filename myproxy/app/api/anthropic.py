"""Anthropic-compatible /v1/messages endpoint.

Translates Anthropic API requests into internal provider calls,
and formats responses back into Anthropic-compatible JSON / SSE.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, Request as FastAPIRequest
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.observability import Observation, get_observer
from app.observability.payload_log import get_payload_logger
from app.providers import get_provider
from app.providers.base import ProviderResponse
from app.providers.key_rotator import KeyRotator
from app.registry.model_registry import get_registry
from app.scrubber import get_scrubber

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["anthropic"])


# ---- Anthropic request models (subset we care about) ----

class ContentBlock(BaseModel):
    type: str
    text: str | None = None
    name: str | None = None
    id: str | None = None
    input: dict[str, object] | None = None
    tool_use_id: str | None = None
    content: str | list[object] | None = None
    is_error: bool | None = None
    # Extended fields used by the Messages spec for multimodal input and
    # prompt caching. Forwarded through to providers when present.
    source: dict[str, object] | None = None
    cache_control: dict[str, object] | None = None
    thinking: str | None = None
    signature: str | None = None


class Message(BaseModel):
    role: str
    content: str | list[ContentBlock]


class ToolDefinition(BaseModel):
    name: str
    description: str = ""
    input_schema: dict[str, object] = Field(default_factory=dict)
    cache_control: dict[str, object] | None = None


class ToolChoice(BaseModel):
    type: str  # auto | any | none | tool
    name: str | None = None  # required when type == "tool"


class MessagesRequest(BaseModel):
    model: str
    messages: list[Message]
    system: str | list[object] | None = None
    max_tokens: int = 4096
    temperature: float = 1.0
    stream: bool = False
    tools: list[ToolDefinition] | None = None
    metadata: dict[str, object] | None = None
    # Spec fields that were previously dropped at this layer.
    thinking: dict[str, object] | None = None
    top_p: float | None = None
    service_tier: str | None = None
    tool_choice: ToolChoice | None = None


# ---- Helper ----

def _build_anthropic_response(
    content_blocks: list[dict[str, object]],
    stop_reason: str,
    usage: dict[str, int],
    model: str,
    response_id: str | None = None,
) -> dict[str, object]:
    if response_id is None:
        response_id = f"msg_{uuid.uuid4().hex[:24]}"
    return {
        "id": response_id,
        "type": "message",
        "role": "assistant",
        "content": content_blocks,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage,
    }


def _provider_response_to_anthropic(
    pr: ProviderResponse, model: str
) -> dict[str, object]:
    """Convert a single ProviderResponse to Anthropic JSON."""
    if pr.content_blocks:
        content = pr.content_blocks
    else:
        content = [{"type": "text", "text": pr.content}]

    usage: dict[str, int] = {
        "input_tokens": pr.input_tokens,
        "output_tokens": pr.output_tokens,
    }
    if pr.cache_creation_input_tokens:
        usage["cache_creation_input_tokens"] = pr.cache_creation_input_tokens
    if pr.cache_read_input_tokens:
        usage["cache_read_input_tokens"] = pr.cache_read_input_tokens

    return _build_anthropic_response(
        content_blocks=content,
        stop_reason=pr.stop_reason,
        usage=usage,
        model=model,
    )


async def _yield_sse_event(event: dict[str, object]) -> str:
    data = json.dumps(event)
    return f"event: {event.get('type', 'message')}\ndata: {data}\n\n"


class _StreamAssembler:
    """Reassembles an Anthropic-shaped final message from SSE events.

    Upstream providers stream ``message_start`` / ``content_block_delta``
    / ``message_delta`` / ``message_stop`` frames. This collects them into
    the single message object a non-streaming request would have returned,
    so the payload log shows the same shape for both modes.
    """

    def __init__(self) -> None:
        self._message: dict[str, object] | None = None
        self._blocks: dict[int, dict[str, object]] = {}
        self._usage: dict[str, int] = {}
        self._model: str | None = None
        self._id: str | None = None
        self._stop_reason: str | None = None

    def feed_frame(self, frame: str) -> None:
        """Consume one ``event:/data:`` frame."""
        for line in frame.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[len("data:"):].strip())
            except ValueError:
                continue
            if isinstance(event, dict):
                self.feed_event(event)

    def feed_event(self, event: dict[str, object]) -> None:
        """Consume one decoded SSE event."""
        event_type = event.get("type")
        index = event.get("index")

        if event_type == "message":
            # The proxy's own terminal frame — already the final message.
            self._message = dict(event)
            return

        if event_type == "message_start":
            message = event.get("message")
            if isinstance(message, dict):
                self._id = message.get("id")
                self._model = message.get("model")
                self._merge_usage(message.get("usage"))
            return

        if event_type == "content_block_start":
            block = event.get("content_block")
            if isinstance(block, dict) and isinstance(index, int):
                self._blocks[index] = dict(block)
            return

        if event_type == "content_block_delta":
            delta = event.get("delta")
            if isinstance(delta, dict) and isinstance(index, int):
                block = self._block(index)
                for key, value in delta.items():
                    if key == "signature":
                        block["signature"] = value
                    elif key in ("text", "thinking") and isinstance(value, str):
                        block[key] = f"{block.get(key, '')}{value}"
                    elif key == "partial_json" and isinstance(value, str):
                        block["partial_json"] = (
                            f"{block.get('partial_json', '')}{value}"
                        )
            return

        if event_type == "message_delta":
            delta = event.get("delta")
            if isinstance(delta, dict) and delta.get("stop_reason"):
                self._stop_reason = str(delta["stop_reason"])
            self._merge_usage(event.get("usage"))
            return

    def _block(self, index: int) -> dict[str, object]:
        return self._blocks.setdefault(index, {"type": "text", "text": ""})

    def _merge_usage(self, usage: object) -> None:
        if not isinstance(usage, dict):
            return
        for key, value in usage.items():
            if isinstance(value, int):
                self._usage[key] = value

    def result(self, model: str) -> dict[str, object]:
        """Return the reassembled message."""
        if self._message is not None:
            return self._message
        return {
            "id": self._id,
            "type": "message",
            "role": "assistant",
            "content": [self._blocks[i] for i in sorted(self._blocks)],
            "model": self._model or model,
            "stop_reason": self._stop_reason,
            "stop_sequence": None,
            "usage": self._usage,
        }

    def to_provider_response(self, model: str) -> ProviderResponse | None:
        """Build a ProviderResponse from the streamed events.

        Returns ``None`` when the stream carried no content and no usage,
        so callers can tell "nothing arrived" from "an empty reply".
        """
        message = self.result(model)
        blocks = message.get("content")
        content_blocks = [b for b in blocks if isinstance(b, dict)] if isinstance(blocks, list) else []
        text_parts = [
            str(b.get("text", ""))
            for b in content_blocks
            if b.get("type") == "text" and b.get("text")
        ]
        usage = message.get("usage")
        usage_map = usage if isinstance(usage, dict) else {}
        if not content_blocks and not usage_map:
            return None
        return ProviderResponse(
            content="\n".join(text_parts),
            content_blocks=content_blocks,
            stop_reason=str(message.get("stop_reason") or "end_turn"),
            input_tokens=int(usage_map.get("input_tokens", 0) or 0),
            output_tokens=int(usage_map.get("output_tokens", 0) or 0),
            cache_creation_input_tokens=int(
                usage_map.get("cache_creation_input_tokens", 0) or 0
            ),
            cache_read_input_tokens=int(
                usage_map.get("cache_read_input_tokens", 0) or 0
            ),
        )


def _log_exchange(
    obs: "Observation",
    *,
    provider_name: str,
    upstream_model: str,
    request_payload: object,
    response_payload: object | None = None,
    error: str | None = None,
    status: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    """Append this exchange to the human-readable payload log.

    The response is scrubbed here, so PII echoed back by the model never
    reaches the file. The request body is expected to be scrubbed
    already — see ``_log_rejected`` for the one path where it is not.
    """
    safe_response = (
        get_scrubber().scrub(response_payload).redacted
        if response_payload is not None
        else None
    )
    get_payload_logger().log_exchange(
        request_id=obs.request_id,
        model_alias=obs.model_alias,
        upstream_model=upstream_model,
        provider=provider_name,
        stream=obs.stream,
        status=obs.status if status is None else status,
        latency_ms=obs.latency_ms,
        request_payload=request_payload,
        response_payload=safe_response,
        error=error,
        input_tokens=obs.input_tokens if input_tokens is None else input_tokens,
        output_tokens=obs.output_tokens if output_tokens is None else output_tokens,
    )


class _SSERewriter:
    """Rewrite raw upstream SSE lines into Anthropic-spec events.

    Providers may yield:

    - Plain ``data: {json}`` lines (Anthropic-shaped), and we re-frame with
      ``event: <type>``.
    - Bare JSON one-per-line (some MiniMax variants), which we promote into
      ``data:`` lines.
    - Already fully-framed ``event:`` / ``data:`` pairs, which we forward.

    Unknown event types pass through verbatim.
    """

    def __init__(self) -> None:
        self._buffer: str = ""
        self._current_event: str | None = None

    def feed(self, raw: str) -> list[str]:
        """Consume a chunk of upstream text and return any complete frames.

        Each returned string is one or more complete ``event: ...\\ndata:
        ...\\n\\n`` blocks ready to send downstream.
        """
        # Normalise line endings and append to buffer.
        self._buffer += raw.replace("\r\n", "\n")

        out: list[str] = []
        while "\n\n" in self._buffer:
            frame, self._buffer = self._buffer.split("\n\n", 1)
            framed = self._format_frame(frame)
            if framed:
                out.append(framed)
        return out

    def flush(self) -> list[str]:
        """Emit any trailing partial frame at end-of-stream."""
        if not self._buffer.strip():
            return []
        framed = self._format_frame(self._buffer)
        self._buffer = ""
        return [framed] if framed else []

    def _format_frame(self, frame: str) -> str:
        lines = [ln for ln in frame.split("\n") if ln.strip() != ""]
        if not lines:
            return ""

        event_type: str | None = None
        data_lines: list[str] = []

        for line in lines:
            if line.startswith("event:"):
                event_type = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
            else:
                # Bare JSON or unknown content. Treat as a data payload.
                data_lines.append(line.strip())

        if not data_lines:
            return ""

        data = "\n".join(data_lines)

        # If no explicit event was given but the data parses to JSON with a
        # 'type' field, use that. Otherwise default to 'message'.
        if event_type is None:
            try:
                parsed = json.loads(data)
                if isinstance(parsed, dict) and "type" in parsed:
                    event_type = str(parsed["type"])
                else:
                    event_type = "message"
            except (ValueError, TypeError):
                event_type = "message"

        return f"event: {event_type}\ndata: {data}\n\n"


def _log_rejected(
    obs: "Observation",
    request: "MessagesRequest",
    error: str,
) -> None:
    """Log a request that was rejected before reaching a provider.

    These requests never went through the outbound scrubber, so the
    body is scrubbed here.
    """
    payload = get_scrubber().scrub(
        request.model_dump(exclude_none=True)
    ).redacted
    _log_exchange(
        obs,
        provider_name="none",
        upstream_model=request.model,
        request_payload=payload,
        error=error,
    )


# ---- Endpoint ----

@router.get("/models")
async def list_models() -> dict[str, object]:
    """List models in the Anthropic ``/v1/models`` format.

    Anthropic-compatible clients (Trae's custom model setup, Anthropic
    SDKs) probe this endpoint to discover available models and to verify
    that an API key works before the first chat request.
    """
    registry = get_registry()
    return {
        "data": [
            {
                "type": "model",
                "id": alias,
                "display_name": spec.get("model", alias),
                "created_at": "2024-01-01T00:00:00Z",
            }
            for alias, spec in registry.list_models().items()
        ],
        "has_more": False,
        "first_id": next(iter(registry.list_models()), None),
        "last_id": next(reversed(list(registry.list_models())), None),
    }


@router.post("/messages", response_model=None)
async def messages(
    fastapi_request: FastAPIRequest,
    request: MessagesRequest,
):
    """Anthropic-compatible Messages endpoint."""
    observer = get_observer()
    # Honor an inbound ``X-Request-Id`` so clients can correlate logs; mint
    # one otherwise. Both paths land in the response header and the trace
    # line (issue 2).
    inbound_rid = fastapi_request.headers.get("x-request-id")
    obs = observer.begin(
        model_alias=request.model,
        stream=request.stream,
        request_id=inbound_rid,
    )

    registry = get_registry()

    # Resolve model
    resolved = registry.get(request.model)
    if resolved is None:
        observer.finish_error(obs, 400, f"unknown model: {request.model}")
        _log_rejected(obs, request, f"unknown model: {request.model}")
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model: {request.model}",
        )

    provider_name, actual_model, context_length = resolved
    provider = get_provider(provider_name)
    if provider is None:
        observer.finish_error(obs, 400, f"provider not configured: {provider_name}")
        _log_rejected(obs, request, f"provider not configured: {provider_name}")
        raise HTTPException(
            status_code=400,
            detail=f"Provider '{provider_name}' not configured (missing API key)",
        )

    # Record which provider / pool size we're routing to. If the provider
    # is a KeyRotator we also hook the per-attempt callback so the trace
    # line and rollup can attribute the request to whichever key finally
    # returned data.
    key_total = provider.key_count if isinstance(provider, KeyRotator) else None
    observer.set_provider(obs, provider_name, None, key_total)
    on_key_cb = (
        (lambda key, o=obs: observer.record_attempt(o, key))
        if isinstance(provider, KeyRotator)
        else None
    )
    if on_key_cb is not None:
        provider.set_on_key(on_key_cb)

    # Extract system prompt
    system_text: str | None = None
    if request.system:
        if isinstance(request.system, str):
            system_text = request.system
        elif isinstance(request.system, list):
            parts: list[str] = []
            for item in request.system:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
            system_text = "\n".join(parts)

    # Build messages
    messages_list: list[dict[str, object]] = []
    for msg in request.messages:
        msg_dict: dict[str, object] = {"role": msg.role}
        if isinstance(msg.content, list):
            msg_dict["content"] = [
                {
                    "type": b.type,
                    **({"text": b.text} if b.text is not None else {}),
                    **({"name": b.name} if b.name is not None else {}),
                    **({"id": b.id} if b.id is not None else {}),
                    **({"input": b.input} if b.input is not None else {}),
                    **({"tool_use_id": b.tool_use_id} if b.tool_use_id is not None else {}),
                    **({"content": b.content} if b.content is not None else {}),
                    **({"is_error": b.is_error} if b.is_error is not None else {}),
                    **({"source": b.source} if b.source is not None else {}),
                    **({"cache_control": b.cache_control} if b.cache_control is not None else {}),
                    **({"thinking": b.thinking} if b.thinking is not None else {}),
                    **({"signature": b.signature} if b.signature is not None else {}),
                }
                for b in msg.content
            ]
        else:
            msg_dict["content"] = msg.content
        messages_list.append(msg_dict)

    tools_list: list[dict[str, object]] | None = None
    if request.tools:
        tools_list = []
        for t in request.tools:
            td: dict[str, object] = {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            if t.cache_control is not None:
                td["cache_control"] = t.cache_control
            tools_list.append(td)

    tool_choice_payload: dict[str, object] | None = None
    if request.tool_choice is not None:
        tool_choice_payload = {"type": request.tool_choice.type}
        if request.tool_choice.name is not None:
            tool_choice_payload["name"] = request.tool_choice.name

    # Scrub outbound payload (PII / secrets).
    scrubber = get_scrubber()
    if system_text is not None:
        system_text = scrubber.scrub({"s": system_text}).redacted["s"]
    scrub_result = scrubber.scrub(
        {"messages": messages_list, "tools": tools_list or []}
    )
    messages_list = scrub_result.redacted["messages"]  # type: ignore[index]
    tools_list = scrub_result.redacted["tools"] or None  # type: ignore[index]
    if scrub_result.events:
        logger.info("scrubbed", extra={"event_count": len(scrub_result.events)})

    # Build the exact body that goes on the wire, for the payload log.
    try:
        request_payload = provider.build_payload(
            model=actual_model,
            system_prompt=system_text,
            messages=messages_list,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            tools=tools_list,
            stream=request.stream,
            thinking=request.thinking,
            top_p=request.top_p,
            service_tier=request.service_tier,
            tool_choice=tool_choice_payload,
        )
    except Exception:
        # Logging must never break the request.
        request_payload = {"model": actual_model, "messages": messages_list}

    # Call provider
    try:
        gen_result = provider.messages(
            model=actual_model,
            system_prompt=system_text,
            messages=messages_list,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            tools=tools_list,
            stream=request.stream,
            thinking=request.thinking,
            top_p=request.top_p,
            service_tier=request.service_tier,
            tool_choice=tool_choice_payload,
        )
    except Exception as e:
        if on_key_cb is not None:
            provider.set_on_key(None)
        observer.finish_error(obs, 502, f"provider invocation failed: {e}")
        _log_exchange(
            obs,
            provider_name=provider_name,
            upstream_model=actual_model,
            request_payload=request_payload,
            error=f"provider invocation failed: {e}",
        )
        logger.exception("provider error: %s", e)
        raise HTTPException(status_code=502, detail=f"Provider error: {e}")

    if not request.stream:
        # Non-streaming: collect the single ProviderResponse
        try:
            pr = await gen_result.__anext__()
        except StopAsyncIteration:
            if on_key_cb is not None:
                provider.set_on_key(None)
            observer.finish_error(obs, 502, "provider returned no response")
            _log_exchange(
                obs,
                provider_name=provider_name,
                upstream_model=actual_model,
                request_payload=request_payload,
                error="provider returned no response",
            )
            raise HTTPException(status_code=502, detail="Provider returned no response")
        except Exception as e:
            if on_key_cb is not None:
                provider.set_on_key(None)
            observer.finish_error(obs, 502, f"provider error: {e}")
            _log_exchange(
                obs,
                provider_name=provider_name,
                upstream_model=actual_model,
                request_payload=request_payload,
                error=f"provider error: {e}",
            )
            logger.exception("provider error: %s", e)
            raise HTTPException(status_code=502, detail=f"Provider error: {e}")

        if on_key_cb is not None:
            provider.set_on_key(None)

        if not isinstance(pr, ProviderResponse):
            observer.finish_error(obs, 500, "unexpected provider response type")
            _log_exchange(
                obs,
                provider_name=provider_name,
                upstream_model=actual_model,
                request_payload=request_payload,
                error="unexpected provider response type",
            )
            raise HTTPException(status_code=500, detail="Unexpected provider response")

        observer.finish_success(obs, 200, pr)
        anthropic_resp = _provider_response_to_anthropic(pr, request.model)
        _log_exchange(
            obs,
            provider_name=provider_name,
            upstream_model=actual_model,
            request_payload=request_payload,
            response_payload=anthropic_resp,
        )
        return JSONResponse(
            content=anthropic_resp,
            headers={
                "X-Context-Length": str(context_length),
                "X-Request-Id": obs.request_id,
                "X-Latency-Ms": f"{obs.latency_ms:.3f}",
            },
        )

    # Streaming
    final_pr: ProviderResponse | None = None
    final_response: dict[str, object] | None = None
    stream_error: str | None = None

    async def stream_wrapper() -> AsyncIterator[str]:
        nonlocal final_pr, final_response, stream_error
        rewriter = _SSERewriter()
        assembler = _StreamAssembler()
        try:
            async for item in gen_result:
                if isinstance(item, str):
                    for framed in rewriter.feed(item):
                        assembler.feed_frame(framed)
                        yield framed
                elif isinstance(item, ProviderResponse):
                    # Flush any pending upstream SSE frames first, then
                    # emit the final response as a single 'message' event.
                    for framed in rewriter.flush():
                        assembler.feed_frame(framed)
                        yield framed
                    final_pr = item
                    anthropic_resp = _provider_response_to_anthropic(item, request.model)
                    final_response = anthropic_resp
                    yield await _yield_sse_event(anthropic_resp)
            # End of stream — flush any trailing partial frame.
            for framed in rewriter.flush():
                assembler.feed_frame(framed)
                yield framed
        except Exception as e:
            stream_error = str(e)
            yield f"event: error\ndata: {json.dumps({'error': 'Internal error'})}\n\n"
        finally:
            # Always tear down the key callback + commit the trace line,
            # even if the client disconnected mid-stream.
            if on_key_cb is not None:
                provider.set_on_key(None)
            if final_pr is None and stream_error is None:
                # Upstream providers that stream raw SSE never send a
                # terminal response object — rebuild one from the events.
                final_pr = assembler.to_provider_response(request.model)
                if final_pr is not None:
                    final_response = _provider_response_to_anthropic(
                        final_pr, request.model
                    )
            if final_pr is not None:
                observer.finish_success(obs, 200, final_pr)
            elif stream_error is not None:
                observer.finish_error(obs, 500, f"stream error: {stream_error}")
            elif not obs.finished:
                observer.finish_error(obs, 500, "stream ended without provider response")

            # Log the reassembled final message. The provider's terminal
            # response wins; otherwise fall back to the streamed events.
            logged_response = final_response or assembler.result(request.model)
            usage = logged_response.get("usage")
            usage_map = usage if isinstance(usage, dict) else {}
            _log_exchange(
                obs,
                provider_name=provider_name,
                upstream_model=actual_model,
                request_payload=request_payload,
                response_payload=logged_response,
                error=(
                    None
                    if stream_error is None and final_pr is not None
                    else (stream_error or "stream ended without provider response")
                ),
                input_tokens=int(usage_map.get("input_tokens", 0) or 0),
                output_tokens=int(usage_map.get("output_tokens", 0) or 0),
            )

    return StreamingResponse(
        stream_wrapper(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Context-Length": str(context_length),
            "X-Request-Id": obs.request_id,
        },
    )
