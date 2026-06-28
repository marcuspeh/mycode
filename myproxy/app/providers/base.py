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
    cost_usd: float = 0.0


class Provider(ABC):
    """LLM provider contract. Implementations handle their own auth and HTTP."""

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
