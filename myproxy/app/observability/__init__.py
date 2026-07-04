"""Request observability: traces, rollups, X-Request-Id.

Every ``/v1/messages`` request flows through ``RequestObserver``,
which mints or accepts an ``X-Request-Id`` header, captures latency /
tokens / cost / error, emits one JSONL trace line, and maintains in-memory
rollups keyed by ``(provider, key_suffix)`` and ``(provider, model)``.
"""
from app.observability.observer import (
    Observation,
    RequestObserver,
    get_observer,
    reset_observer,
)

__all__ = [
    "Observation",
    "RequestObserver",
    "get_observer",
    "reset_observer",
]