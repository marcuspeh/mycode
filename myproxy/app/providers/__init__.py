"""Provider factory."""
from __future__ import annotations

from app.config.settings import get_settings
from app.providers.base import Provider
from app.providers.deepseek import DeepSeekProvider
from app.providers.minimax import MiniMaxProvider


def get_provider(name: str) -> Provider | None:
    """Return a configured provider instance by name, or None if not configured."""
    settings = get_settings()
    timeout = settings.request_timeout_seconds

    if name == "minimax" and settings.MINIMAX_API_KEY:
        return MiniMaxProvider(settings.MINIMAX_API_KEY, timeout)
    if name == "deepseek" and settings.DEEPSEEK_API_KEY:
        return DeepSeekProvider(settings.DEEPSEEK_API_KEY, timeout)
    return None
