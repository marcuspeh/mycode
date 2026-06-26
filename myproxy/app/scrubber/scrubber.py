"""Recursive payload scrubber for outbound requests."""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field

from app.config.settings import get_settings
from app.scrubber.patterns import DEFAULT_PATTERNS, Pattern

logger = logging.getLogger(__name__)


@dataclass
class ScrubResult:
    """Result of scrubbing a payload."""

    redacted: object
    events: list[dict[str, str]] = field(default_factory=list)


class Scrubber:
    """Walks a payload and replaces secrets/PII with placeholders."""

    def __init__(self, patterns: list[Pattern], enabled: bool = True) -> None:
        self._patterns = patterns
        self._enabled = enabled

    def scrub(self, payload: object) -> ScrubResult:
        """Deep-copy and scrub payload. Events log field path and type only."""
        events: list[dict[str, str]] = []
        if not self._enabled:
            return ScrubResult(redacted=copy.deepcopy(payload), events=events)
        redacted = self._scrub_value(payload, "$", events)
        return ScrubResult(redacted=redacted, events=events)

    def _scrub_value(
        self, value: object, path: str, events: list[dict[str, str]]
    ) -> object:
        if isinstance(value, str):
            return self._scrub_string(value, path, events)
        if isinstance(value, dict):
            return {k: self._scrub_value(v, f"{path}.{k}", events) for k, v in value.items()}
        if isinstance(value, list):
            return [self._scrub_value(item, f"{path}[{i}]", events) for i, item in enumerate(value)]
        return value

    def _scrub_string(
        self, text: str, path: str, events: list[dict[str, str]]
    ) -> str:
        out = text
        for pattern in self._patterns:
            new_out, count = pattern.regex.subn(pattern.placeholder, out)
            if count > 0:
                events.append({"type": pattern.name, "field": path, "count": str(count)})
                out = new_out
        return out


_scrubber: Scrubber | None = None


def get_scrubber() -> Scrubber:
    """Return the process-wide Scrubber instance."""
    global _scrubber
    if _scrubber is None:
        settings = get_settings()
        _scrubber = Scrubber(DEFAULT_PATTERNS, enabled=settings.scrub_enabled)
    return _scrubber
