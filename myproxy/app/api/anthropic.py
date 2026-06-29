"""Anthropic-compatible /v1/messages endpoint.

Translates Anthropic API requests into internal provider calls,
and formats responses back into Anthropic-compatible JSON / SSE.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, Request as FastAPIRequest
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.providers import get_provider
from app.providers.base import ProviderResponse
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
    type: str  # spec allows only "auto" or "none"


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


# ---- Endpoint ----

@router.post("/messages", response_model=None)
async def messages(
    fastapi_request: FastAPIRequest,
    request: MessagesRequest,
):
    """Anthropic-compatible Messages endpoint."""
    registry = get_registry()

    # Resolve model
    resolved = registry.get(request.model)
    if resolved is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model: {request.model}",
        )

    provider_name, actual_model, context_length = resolved
    provider = get_provider(provider_name)
    if provider is None:
        raise HTTPException(
            status_code=400,
            detail=f"Provider '{provider_name}' not configured (missing API key)",
        )

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

    # Call provider
    t0 = time.monotonic()
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
        logger.exception("provider error: %s", e)
        raise HTTPException(status_code=502, detail=f"Provider error: {e}")

    if not request.stream:
        # Non-streaming: collect the single ProviderResponse
        pr = await gen_result.__anext__()
        latency_ms = (time.monotonic() - t0) * 1000

        if not isinstance(pr, ProviderResponse):
            raise HTTPException(status_code=500, detail="Unexpected provider response")

        anthropic_resp = _provider_response_to_anthropic(pr, request.model)
        from fastapi.responses import JSONResponse
        return JSONResponse(
            content=anthropic_resp,
            headers={"X-Context-Length": str(context_length)},
        )

    # Streaming
    async def stream_wrapper() -> AsyncIterator[str]:
        rewriter = _SSERewriter()
        try:
            async for item in gen_result:
                if isinstance(item, str):
                    for framed in rewriter.feed(item):
                        yield framed
                elif isinstance(item, ProviderResponse):
                    # Flush any pending upstream SSE frames first, then
                    # emit the final response as a single 'message' event.
                    for framed in rewriter.flush():
                        yield framed
                    anthropic_resp = _provider_response_to_anthropic(item, request.model)
                    yield await _yield_sse_event(anthropic_resp)
            # End of stream — flush any trailing partial frame.
            for framed in rewriter.flush():
                yield framed
        except Exception:
            yield f"event: error\ndata: {json.dumps({'error': 'Internal error'})}\n\n"

    return StreamingResponse(
        stream_wrapper(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Context-Length": str(context_length),
        },
    )
