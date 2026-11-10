"""Full request/response payload logging to a plain-text file.

The JSONL trace file answers "how many requests, how slow, how many
tokens". This module answers the other half: *what exactly was sent to
the model, and what came back*. One human-readable block per request::

    ====================================================================================================
    [2026-11-10T09:12:33.481Z] request_id=req_ab12cd34 model=claude-lb-minimax-m3 provider=minimax
      upstream_model=MiniMax-M3 stream=true status=200 latency_ms=1234.500 input_tokens=123 output_tokens=45
    --- REQUEST ---
    {
      "model": "MiniMax-M3",
      ...
    }
    --- RESPONSE ---
    {
      ...
    }
    ====================================================================================================

Bodies are scrubbed by the caller before they reach this module, so
secrets and PII are redacted before anything touches the disk.

The file is rotated by size (``myproxy-traffic.txt`` ->
``myproxy-traffic.1.txt``) so a long-running server cannot fill the disk.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("myproxy.payload")

_SEPARATOR = "=" * 100


def _now_iso() -> str:
    """ISO 8601 UTC with millisecond precision and a ``Z`` suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _dump(payload: Any) -> str:
    """Pretty-print a payload as JSON, falling back to ``repr``."""
    try:
        return json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(payload)


class PayloadLogger:
    """Appends request/response blocks to a ``.txt`` file.

    Threadsafe: every write happens under a single lock, and the file is
    rotated before the write that would push it past ``max_bytes``.
    """

    def __init__(
        self,
        path: Path | None = None,
        max_bytes: int = 50 * 1024 * 1024,
        backups: int = 1,
    ) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = None
        self._max_bytes = max_bytes
        self._backups = backups
        self._size = 0
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch(exist_ok=True)
                self._size = path.stat().st_size
                self._path = path
            except OSError as e:
                logger.warning(
                    "payload log %s not writable (%s); logging disabled", path, e
                )

    @property
    def path(self) -> Path | None:
        """The file being written to, or ``None`` when logging is off."""
        return self._path

    def log_exchange(
        self,
        *,
        request_id: str,
        model_alias: str,
        upstream_model: str,
        provider: str,
        stream: bool,
        status: int,
        latency_ms: float,
        request_payload: Any,
        response_payload: Any = None,
        error: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        """Write one request/response block."""
        if self._path is None:
            return
        block = self._format(
            request_id=request_id,
            model_alias=model_alias,
            upstream_model=upstream_model,
            provider=provider,
            stream=stream,
            status=status,
            latency_ms=latency_ms,
            request_payload=request_payload,
            response_payload=response_payload,
            error=error,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        self._append(block)

    # ---- internals ----

    def _format(
        self,
        *,
        request_id: str,
        model_alias: str,
        upstream_model: str,
        provider: str,
        stream: bool,
        status: int,
        latency_ms: float,
        request_payload: Any,
        response_payload: Any,
        error: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> str:
        header = [
            _SEPARATOR,
            f"[{_now_iso()}] request_id={request_id} model={model_alias} provider={provider}",
            f"  upstream_model={upstream_model} stream={str(stream).lower()}"
            f" status={status} latency_ms={latency_ms:.3f}",
        ]
        if input_tokens is not None or output_tokens is not None:
            header.append(
                f"  input_tokens={input_tokens or 0} output_tokens={output_tokens or 0}"
            )
        header.append("--- REQUEST ---")
        header.append(_dump(request_payload))
        if error is not None:
            header.append("--- ERROR ---")
            header.append(error)
        elif response_payload is not None:
            header.append("--- RESPONSE ---")
            header.append(_dump(response_payload))
        header.append(_SEPARATOR)
        header.append("")
        return "\n".join(header) + "\n"

    def _append(self, block: str) -> None:
        size = len(block.encode("utf-8"))
        with self._lock:
            if self._path is None:
                return
            try:
                if (
                    self._max_bytes > 0
                    and self._size > 0
                    and self._size + size > self._max_bytes
                ):
                    self._rotate()
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(block)
                self._size += size
            except OSError as e:
                # Don't try again on every request after the first failure.
                logger.warning("payload log write failed (%s); logging disabled", e)
                self._path = None

    def _rotate(self) -> None:
        """Shift ``.1`` -> ``.2`` and the live file -> ``.1``."""
        if self._path is None:
            return
        if self._backups <= 0:
            # No backups wanted — start the file over.
            self._path.write_text("", encoding="utf-8")
            self._size = 0
            return
        oldest = self._rotated(self._backups)
        if oldest.exists():
            oldest.unlink()
        for i in range(self._backups - 1, 0, -1):
            src = self._rotated(i)
            if src.exists():
                src.replace(self._rotated(i + 1))
        self._path.replace(self._rotated(1))
        self._size = 0

    def _rotated(self, index: int) -> Path:
        assert self._path is not None
        return self._path.with_suffix(f".{index}.txt")


_logger: PayloadLogger | None = None


def get_payload_logger() -> PayloadLogger:
    """Process-wide payload logger singleton.

    Resolves the target file from settings on first call so test fixtures
    can override ``Settings`` before the singleton materialises.
    """
    global _logger
    if _logger is None:
        from app.config.settings import get_settings

        settings = get_settings()
        path = settings.payload_log_file if settings.payload_log_enabled else None
        _logger = PayloadLogger(
            path=path,
            max_bytes=settings.payload_log_max_bytes,
            backups=settings.payload_log_backups,
        )
    return _logger


def reset_payload_logger() -> None:
    """Drop the singleton (used by tests that swap settings)."""
    global _logger
    _logger = None
