"""MiniMax provider (Anthropic-compatible API)."""
from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from app.providers.base import Provider, ProviderResponse


class MiniMaxProvider(Provider):
    """MiniMax speaks an Anthropic-compatible Messages API."""

    BASE_URL = "https://api.minimax.io/anthropic/v1"

    def __init__(self, api_key: str, timeout: float = 300.0) -> None:
        self._api_key = api_key
        self._timeout = timeout

    def provider_name(self) -> str:
        return "minimax"

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

        headers = {
            "x-api-key": self._api_key,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            if stream:
                async for chunk_str in self._stream(client, headers, payload):
                    yield chunk_str
            else:
                response = await client.post(
                    f"{self.BASE_URL}/messages",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                yield self._parse_response(data)

    async def _stream(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        payload: dict[str, object],
    ) -> AsyncIterator[str]:
        async with client.stream(
            "POST",
            f"{self.BASE_URL}/messages",
            headers=headers,
            json=payload,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line:
                    yield line + "\n"

    def _parse_response(self, data: dict) -> ProviderResponse:
        content_blocks: list[dict] = data.get("content", [])
        text_parts: list[str] = []
        stop_reason = data.get("stop_reason", "end_turn")

        for block in content_blocks:
            if isinstance(block, dict):
                if block.get("type") == "text" and "text" in block:
                    text_parts.append(str(block["text"]))
                elif block.get("type") == "tool_use":
                    text_parts.append(json.dumps(block))

        usage = data.get("usage", {})
        return ProviderResponse(
            content="\n".join(text_parts),
            stop_reason=stop_reason,
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            cost_usd=0.0,  # MiniMax cost calculated by caller
        )
