"""Provider factory."""
from __future__ import annotations

from app.config.settings import get_settings
from app.providers.base import Provider
from app.providers.deepseek import DeepSeekProvider
from app.providers.key_rotator import KeyRotator
from app.providers.minimax import MiniMaxProvider


def get_provider(name: str) -> Provider | None:
    """Return a configured provider instance by name, or None if not configured.

    When more than one API key is configured for a provider, the result is
    wrapped in a `KeyRotator` that round-robins across keys per request.
    """
    settings = get_settings()
    timeout = settings.request_timeout_seconds

    if name == "minimax":
        keys = settings.minimax_keys
        if not keys:
            return None
        if len(keys) == 1:
            return MiniMaxProvider(keys[0], timeout)
        return KeyRotator(
            provider_name="minimax",
            keys=keys,
            timeout=timeout,
            factory=lambda k: MiniMaxProvider(k, timeout),
        )

    if name == "deepseek":
        keys = settings.deepseek_keys
        if not keys:
            return None
        if len(keys) == 1:
            return DeepSeekProvider(keys[0], timeout)
        return KeyRotator(
            provider_name="deepseek",
            keys=keys,
            timeout=timeout,
            factory=lambda k: DeepSeekProvider(k, timeout),
        )

    return None
