"""Abstract provider interface and internal response model."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from pydantic import BaseModel


class ProviderResponse(BaseModel):
    """Unified internal response returned by every provider."""

    content: str = ""
    content_blocks: list[dict[str, object]] = []
    stop_reason: str = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float = 0.0


class Provider(ABC):
    """LLM provider contract. Implementations handle their own auth and HTTP."""

    def build_payload(
        self,
        *,
        model: str,
        system_prompt: str | None,
        messages: list[dict[str, object]],
        max_tokens: int,
        temperature: float,
        tools: list[dict[str, object]] | None,
        stream: bool,
        thinking: dict[str, object] | None = None,
        top_p: float | None = None,
        service_tier: str | None = None,
        tool_choice: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Build the JSON body sent to the upstream provider.

        Single source of truth for the wire format — providers send what
        this returns, and the payload logger records it verbatim.
        """
        payload: dict[str, object] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [dict(m) for m in messages],
            "stream": stream,
        }
        if system_prompt:
            payload["system"] = system_prompt
        if tools:
            payload["tools"] = tools
        if top_p is not None:
            payload["top_p"] = top_p
        if service_tier:
            payload["service_tier"] = service_tier
        if tool_choice:
            payload["tool_choice"] = tool_choice
        if thinking:
            payload["thinking"] = thinking
        return payload

    @abstractmethod
    async def messages(
        self,
        model: str,
        system_prompt: str | None,
        messages: list[dict[str, object]],
        max_tokens: int,
        temperature: float,
        tools: list[dict[str, object]] | None,
        stream: bool,
        thinking: dict[str, object] | None = None,
        top_p: float | None = None,
        service_tier: str | None = None,
        tool_choice: dict[str, object] | None = None,
    ) -> AsyncIterator[ProviderResponse | str]:
        """Yield complete ProviderResponse objects for non-streaming,
        or raw SSE strings for streaming. Each SSE string should include
        the ``data: ...\\n\\n`` framing.
        """
        ...

    @abstractmethod
    def provider_name(self) -> str:
        """Short string used for logging and metrics labels."""
        ...
