"""Process-wide request observability.

Each ``/v1/messages`` call gets one ``Observation`` whose lifecycle is:

    begin() → set_provider() → [record_attempt() …] → finish_success/finish_error()

On ``finish_*`` we:

  * record latency (monotonic → ms)
  * bump the in-memory rollups (totals + per-key + per-model)
  * emit a single JSONL line via the ``myproxy.trace`` logger
  * optionally append the same line to ``Settings.trace_file``

The trace line + the rollups together answer "which model, which key,
which minute?" from issue 2.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.providers.base import ProviderResponse

logger = logging.getLogger("myproxy.trace")


def _now_iso() -> str:
    """ISO 8601 UTC with millisecond precision and a ``Z`` suffix."""
    return (
        datetime.now(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    )


def _new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:24]}"


def _key_suffix(key: str | None) -> str | None:
    """Return only the last 4 chars of a key so we never log full secrets.

    ``None`` / empty input passes through; keys shorter than 4 chars are
    returned as-is (test fixtures use ``"a"``, ``"b"`` etc.).
    """
    if not key:
        return None
    if len(key) <= 4:
        return key
    return f"...{key[-4:]}"


@dataclass
class Observation:
    """Mutable record of a single in-flight request.

    Only ``finish_*`` should set the terminal fields (latency, status,
    tokens, cost, error). ``record_attempt`` is called by ``KeyRotator``
    once per key tried so the trace line shows the retry count.
    """

    request_id: str
    started_monotonic: float
    started_at: str  # ISO 8601 UTC
    model_alias: str
    stream: bool

    provider: str | None = None
    key_suffix: str | None = None
    key_total: int | None = None
    attempts: int = 0

    status: int = 0
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    stop_reason: str | None = None
    error: str | None = None
    finished: bool = False


class RequestObserver:
    """Tracks every request through the ``/v1/messages`` endpoint.

    Threadsafe: all rollup mutation is guarded by a single lock. The lock
    is only held during in-memory updates; the JSONL write happens after
    release so a slow disk cannot stall concurrent requests.
    """

    def __init__(
        self,
        trace_file: Path | None = None,
        recent_limit: int = 500,
    ) -> None:
        self._lock = threading.Lock()
        self._recent: list[dict[str, Any]] = []
        self._recent_limit = recent_limit
        self._rollup_keys: dict[tuple[str, str | None], dict[str, Any]] = {}
        self._rollup_models: dict[tuple[str, str], dict[str, Any]] = {}
        self._totals: dict[str, Any] = {
            "requests": 0,
            "errors": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "cost_usd": 0.0,
            "latency_ms_total": 0.0,
        }
        self._trace_file: Path | None = None
        if trace_file is not None:
            try:
                trace_file.parent.mkdir(parents=True, exist_ok=True)
                trace_file.touch(exist_ok=True)
                self._trace_file = trace_file
            except OSError as e:
                logger.warning(
                    "trace file %s not writable (%s); falling back to logger-only",
                    trace_file,
                    e,
                )

    # ---- lifecycle ----

    def begin(
        self,
        model_alias: str,
        stream: bool,
        request_id: str | None = None,
    ) -> Observation:
        """Open an observation.

        ``request_id`` is the inbound ``X-Request-Id`` header if the client
        supplied one; otherwise a fresh ``req_<hex>`` is minted.
        """
        rid = (
            request_id.strip()
            if request_id and request_id.strip()
            else _new_request_id()
        )
        return Observation(
            request_id=rid,
            started_monotonic=time.monotonic(),
            started_at=_now_iso(),
            model_alias=model_alias,
            stream=stream,
        )

    def set_provider(
        self,
        obs: Observation,
        provider: str,
        key: str | None,
        key_total: int | None,
    ) -> None:
        """Record which provider / pool size we're routing to.

        The actual key used is filled in by ``record_attempt`` as soon as
        ``KeyRotator`` picks one.
        """
        obs.provider = provider
        obs.key_suffix = _key_suffix(key)
        obs.key_total = key_total

    def record_attempt(self, obs: Observation, key: str | None) -> None:
        """Called by ``KeyRotator`` for each key it tries.

        Retries are visible in the trace line via ``attempts``. The last
        call wins for ``key_suffix`` so the rollup attributes the request
        to whichever key actually returned the data.
        """
        obs.attempts += 1
        if key is not None:
            obs.key_suffix = _key_suffix(key)

    def finish_success(
        self,
        obs: Observation,
        status: int,
        response: "ProviderResponse",
    ) -> None:
        obs.latency_ms = (time.monotonic() - obs.started_monotonic) * 1000.0
        obs.status = status
        obs.input_tokens = response.input_tokens
        obs.output_tokens = response.output_tokens
        obs.cache_creation_tokens = response.cache_creation_input_tokens
        obs.cache_read_tokens = response.cache_read_input_tokens
        obs.cost_usd = response.cost_usd
        obs.stop_reason = response.stop_reason
        obs.finished = True
        self._commit(obs)

    def finish_error(self, obs: Observation, status: int, error: str) -> None:
        obs.latency_ms = (time.monotonic() - obs.started_monotonic) * 1000.0
        obs.status = status
        obs.error = error
        obs.finished = True
        self._commit(obs)

    # ---- rollups & queries ----

    def _commit(self, obs: Observation) -> None:
        with self._lock:
            self._totals["requests"] += 1
            self._totals["latency_ms_total"] += obs.latency_ms
            self._totals["input_tokens"] += obs.input_tokens
            self._totals["output_tokens"] += obs.output_tokens
            self._totals["cache_creation_tokens"] += obs.cache_creation_tokens
            self._totals["cache_read_tokens"] += obs.cache_read_tokens
            self._totals["cost_usd"] += obs.cost_usd
            if obs.error or obs.status >= 400:
                self._totals["errors"] += 1

            provider = obs.provider or "unknown"

            key_key = (provider, obs.key_suffix)
            key_row = self._rollup_keys.setdefault(
                key_key,
                {
                    "provider": provider,
                    "key_suffix": obs.key_suffix,
                    "requests": 0,
                    "errors": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 0,
                    "cost_usd": 0.0,
                    "latency_ms_total": 0.0,
                },
            )
            key_row["requests"] += 1
            key_row["latency_ms_total"] += obs.latency_ms
            key_row["input_tokens"] += obs.input_tokens
            key_row["output_tokens"] += obs.output_tokens
            key_row["cache_creation_tokens"] += obs.cache_creation_tokens
            key_row["cache_read_tokens"] += obs.cache_read_tokens
            key_row["cost_usd"] += obs.cost_usd
            if obs.error or obs.status >= 400:
                key_row["errors"] += 1

            model_key = (provider, obs.model_alias)
            model_row = self._rollup_models.setdefault(
                model_key,
                {
                    "provider": provider,
                    "model": obs.model_alias,
                    "requests": 0,
                    "errors": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 0,
                    "cost_usd": 0.0,
                    "latency_ms_total": 0.0,
                },
            )
            model_row["requests"] += 1
            model_row["latency_ms_total"] += obs.latency_ms
            model_row["input_tokens"] += obs.input_tokens
            model_row["output_tokens"] += obs.output_tokens
            model_row["cache_creation_tokens"] += obs.cache_creation_tokens
            model_row["cache_read_tokens"] += obs.cache_read_tokens
            model_row["cost_usd"] += obs.cost_usd
            if obs.error or obs.status >= 400:
                model_row["errors"] += 1

            record = self._snapshot(obs)
            self._recent.append(record)
            if len(self._recent) > self._recent_limit:
                # Drop the oldest in chunks; bounded by recent_limit.
                del self._recent[: len(self._recent) - self._recent_limit]

        # Outside the lock — slow disk must not block concurrent requests.
        self._emit_trace(record)

    @staticmethod
    def _snapshot(obs: Observation) -> dict[str, Any]:
        return {
            "request_id": obs.request_id,
            "ts": obs.started_at,
            "model": obs.model_alias,
            "provider": obs.provider,
            "key": obs.key_suffix,
            "key_total": obs.key_total,
            "attempts": obs.attempts,
            "stream": obs.stream,
            "status": obs.status,
            "latency_ms": round(obs.latency_ms, 3),
            "input_tokens": obs.input_tokens,
            "output_tokens": obs.output_tokens,
            "cache_creation_tokens": obs.cache_creation_tokens,
            "cache_read_tokens": obs.cache_read_tokens,
            "cost_usd": obs.cost_usd,
            "stop_reason": obs.stop_reason,
            "error": obs.error,
        }

    def _emit_trace(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, separators=(",", ":"))
        # Logger first — uvicorn / docker / log aggregators all see it.
        logger.info("trace %s", line)
        if self._trace_file is not None:
            try:
                with self._trace_file.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError as e:
                # Don't try again on every request after the first failure.
                logger.warning("trace file write failed (%s); disabling", e)
                self._trace_file = None

    def stats(self) -> dict[str, Any]:
        """Return totals + per-key + per-model rollups for ``/admin/stats``."""
        with self._lock:
            totals = dict(self._totals)
            totals["avg_latency_ms"] = (
                round(totals["latency_ms_total"] / totals["requests"], 3)
                if totals["requests"]
                else 0.0
            )
            totals.pop("latency_ms_total", None)
            return {
                "totals": totals,
                "by_key": [self._avg(r) for r in self._rollup_keys.values()],
                "by_model": [self._avg(r) for r in self._rollup_models.values()],
                "recent_count": len(self._recent),
            }

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the last ``limit`` trace snapshots (oldest first dropped)."""
        with self._lock:
            return list(self._recent[-limit:])

    @staticmethod
    def _avg(row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        n = row.get("requests") or 0
        out["avg_latency_ms"] = (
            round((row.get("latency_ms_total") or 0.0) / n, 3) if n else 0.0
        )
        # ``latency_ms_total`` is the running sum; callers want the average.
        out.pop("latency_ms_total", None)
        return out


_observer: RequestObserver | None = None


def get_observer() -> RequestObserver:
    """Process-wide observer singleton.

    Resolves the trace file from settings on first call so test fixtures
    can override ``Settings`` before the singleton materialises.
    """
    global _observer
    if _observer is None:
        from app.config.settings import get_settings

        settings = get_settings()
        path: Path | None = None
        if settings.trace_enabled:
            path = settings.trace_file
        _observer = RequestObserver(trace_file=path)
    return _observer


def reset_observer() -> None:
    """Drop the singleton (used by tests that swap settings)."""
    global _observer
    _observer = None