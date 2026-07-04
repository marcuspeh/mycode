"""Round-robin wrapper that distributes requests across a pool of API keys.

A `KeyRotator` holds a list of API keys and, on each call to `messages()`,
picks the next key in rotation and instantiates a fresh inner provider with
that key. For streaming requests that fail mid-stream (or non-streaming
requests that raise), the rotator retries the request with the next key in
order, transparently to the caller.

The rotator is process-wide per provider; concurrent requests are serialised
on a small lock while the index is incremented, which is fast.
"""
from __future__ import annotations

import logging
import threading
from typing import AsyncIterator, Callable

import httpx

from app.providers.base import Provider, ProviderResponse

logger = logging.getLogger(__name__)


# Errors worth retrying with the next key. httpx covers connect / read /
# timeout / remote-protocol failures; we deliberately exclude JSONResponse
# errors since those mean the request reached the upstream and the upstream
# chose to reject it (we should not mask 4xx by retrying with another key).
_RETRYABLE_ERRORS: tuple[type[BaseException], ...] = (
    httpx.HTTPError,
    httpx.RemoteProtocolError,
    ConnectionError,
    TimeoutError,
)


class KeyRotator(Provider):
    """Round-robin wrapper over a pool of API keys.

    `factory(api_key)` must return a fresh `Provider` instance bound to that
    key. A new inner provider is constructed per call so the key is bound at
    request time, not at process start.
    """

    def __init__(
        self,
        provider_name: str,
        keys: list[str],
        timeout: float,
        factory: Callable[[str], Provider],
    ) -> None:
        if not keys:
            raise ValueError("KeyRotator requires at least one key")
        self._name = provider_name
        self._keys = list(keys)
        self._timeout = timeout
        self._factory = factory
        self._idx = 0
        self._lock = threading.Lock()
        # Optional callback invoked with each key we try on this request.
        # Set by the request observer so trace lines can report which key
        # actually handled the call (and how many retries happened).
        self._on_key: Callable[[str], None] | None = None

    @property
    def key_count(self) -> int:
        """Number of keys in the rotation pool (for observability)."""
        return len(self._keys)

    def set_on_key(self, callback: Callable[[str], None] | None) -> None:
        """Register (or clear) a per-attempt key callback.

        The observer uses this to attribute each request to the key that
        ultimately returned data — solving "which key did this come from?"
        from issue 2.
        """
        self._on_key = callback

    def provider_name(self) -> str:
        return self._name

    def _reserve_start_index(self) -> int:
        """Atomically read the current cursor and advance it by one."""
        with self._lock:
            start = self._idx
            self._idx += 1
            return start

    def _key_for_attempt(self, start: int, attempt: int) -> str:
        """Return the key for retry `attempt` relative to the request's
        reserved start index. Walks forward so retries pick distinct keys."""
        n = len(self._keys)
        return self._keys[(start + attempt) % n]

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
        # Reserve the starting index up front so concurrent requests don't
        # collide on the same key when the pool is small.
        start = self._reserve_start_index()

        last_error: BaseException | None = None
        for attempt in range(len(self._keys)):
            key = self._key_for_attempt(start, attempt)
            if self._on_key is not None:
                self._on_key(key)
            inner = self._factory(key)
            try:
                if stream:
                    # Buffer the inner stream so we can transparently retry
                    # on failure without emitting partial bytes to the client.
                    async for item in inner.messages(
                        model=model,
                        system_prompt=system_prompt,
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        tools=tools,
                        stream=True,
                        thinking=thinking,
                        top_p=top_p,
                        service_tier=service_tier,
                        tool_choice=tool_choice,
                    ):
                        yield item
                    return
                else:
                    async for item in inner.messages(
                        model=model,
                        system_prompt=system_prompt,
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        tools=tools,
                        stream=False,
                        thinking=thinking,
                        top_p=top_p,
                        service_tier=service_tier,
                        tool_choice=tool_choice,
                    ):
                        yield item
                    return
            except _RETRYABLE_ERRORS as e:
                last_error = e
                logger.warning(
                    "key rotation: attempt %d failed (%s), trying next key",
                    attempt + 1,
                    type(e).__name__,
                )
                # Loop to next key.
                continue

        # All keys exhausted; surface the last failure.
        assert last_error is not None
        raise last_error