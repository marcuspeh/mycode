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
from fastapi.responses import StreamingResponse
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


class Message(BaseModel):
    role: str
    content: str | list[ContentBlock]


class ToolDefinition(BaseModel):
    name: str
    description: str = ""
    input_schema: dict[str, object] = Field(default_factory=dict)


class MessagesRequest(BaseModel):
    model: str
    messages: list[Message]
    system: str | list[object] | None = None
    max_tokens: int = 4096
    temperature: float = 1.0
    stream: bool = False
    tools: list[ToolDefinition] | None = None
    metadata: dict[str, object] | None = None


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

    return _build_anthropic_response(
        content_blocks=content,
        stop_reason=pr.stop_reason,
        usage={
            "input_tokens": pr.input_tokens,
            "output_tokens": pr.output_tokens,
        },
        model=model,
    )


async def _yield_sse_event(event: dict[str, object]) -> str:
    data = json.dumps(event)
    return f"event: {event.get('type', 'message')}\ndata: {data}\n\n"


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

    provider_name, actual_model = resolved
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
                }
                for b in msg.content
            ]
        else:
            msg_dict["content"] = msg.content
        messages_list.append(msg_dict)

    tools_list: list[dict[str, object]] | None = None
    if request.tools:
        tools_list = [t.model_dump() for t in request.tools]

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
        return anthropic_resp

    # Streaming
    async def stream_wrapper() -> AsyncIterator[str]:
        try:
            async for item in gen_result:
                if isinstance(item, str):
                    yield item
                elif isinstance(item, ProviderResponse):
                    anthropic_resp = _provider_response_to_anthropic(item, request.model)
                    yield await _yield_sse_event(anthropic_resp)
        except Exception:
            yield f"event: error\ndata: {json.dumps({'error': 'Internal error'})}\n\n"

    return StreamingResponse(
        stream_wrapper(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
