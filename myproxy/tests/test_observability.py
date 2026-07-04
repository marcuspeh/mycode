"""Tests for the request observability module (issue 2).

Covers the ``RequestObserver`` directly, the ``KeyRotator`` key callback,
and the wire-up through ``/v1/messages`` and ``/admin/stats``.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app
from app.observability import (
    Observation,
    RequestObserver,
    get_observer,
    reset_observer,
)
from app.providers.base import ProviderResponse
from app.providers.key_rotator import KeyRotator

client = TestClient(app)


# ---- Helpers ----

def _make_async_gen(items: list):
    """Create a proper async generator from a list of items for mocking."""
    async def _gen():
        for item in items:
            yield item
    return _gen()


@pytest.fixture(autouse=True)
def _fresh_observer():
    """Each test starts with a clean observer singleton."""
    reset_observer()
    yield
    reset_observer()


# ---- RequestObserver unit tests ----

class TestRequestObserverUnit:
    def test_begin_mints_request_id_when_none_supplied(self):
        obs = RequestObserver()
        o = obs.begin(model_alias="m", stream=False)
        assert o.request_id.startswith("req_")
        assert len(o.request_id) > len("req_")

    def test_begin_honors_inbound_request_id(self):
        obs = RequestObserver()
        o = obs.begin(model_alias="m", stream=False, request_id="client-abc-123")
        assert o.request_id == "client-abc-123"

    def test_begin_mints_when_inbound_is_blank(self):
        obs = RequestObserver()
        o = obs.begin(model_alias="m", stream=False, request_id="   ")
        assert o.request_id.startswith("req_")

    def test_finish_success_records_tokens_and_cost(self):
        obs = RequestObserver()
        o = obs.begin(model_alias="m", stream=False)
        obs.set_provider(o, provider="minimax", key="sk-aa11", key_total=3)
        obs.finish_success(
            o,
            status=200,
            response=ProviderResponse(
                content="ok",
                stop_reason="end_turn",
                input_tokens=100,
                output_tokens=50,
                cache_creation_input_tokens=10,
                cache_read_input_tokens=5,
                cost_usd=0.0123,
            ),
        )
        assert o.latency_ms > 0
        assert o.status == 200
        assert o.input_tokens == 100
        assert o.output_tokens == 50
        assert o.cache_creation_tokens == 10
        assert o.cache_read_tokens == 5
        assert o.cost_usd == 0.0123
        assert o.stop_reason == "end_turn"
        assert o.error is None

    def test_finish_error_records_status_and_message(self):
        obs = RequestObserver()
        o = obs.begin(model_alias="m", stream=False)
        obs.set_provider(o, provider="minimax", key="sk-aa11", key_total=1)
        obs.finish_error(o, status=502, error="provider timeout")
        assert o.status == 502
        assert o.error == "provider timeout"
        assert o.latency_ms > 0

    def test_record_attempt_tracks_retry_count(self):
        obs = RequestObserver()
        o = obs.begin(model_alias="m", stream=False)
        obs.set_provider(o, provider="minimax", key=None, key_total=3)
        obs.record_attempt(o, "sk-aaaa")
        obs.record_attempt(o, "sk-bbbb")
        obs.record_attempt(o, "sk-cccc")
        # Final key wins for suffix.
        assert o.key_suffix == "...cccc"
        assert o.attempts == 3

    def test_key_suffix_only_last_four(self):
        obs = RequestObserver()
        o = obs.begin(model_alias="m", stream=False)
        obs.set_provider(o, provider="minimax", key="sk-ant-1234567890abcdef", key_total=1)
        assert o.key_suffix == "...cdef"

    def test_key_suffix_preserves_short_keys(self):
        obs = RequestObserver()
        o = obs.begin(model_alias="m", stream=False)
        obs.set_provider(o, provider="minimax", key="ab", key_total=1)
        assert o.key_suffix == "ab"


class TestRequestObserverRollups:
    def test_totals_increment_on_success(self):
        obs = RequestObserver()
        for i in range(3):
            o = obs.begin(model_alias="m1", stream=False)
            obs.set_provider(o, provider="minimax", key="sk-aaaa", key_total=2)
            obs.finish_success(
                o,
                status=200,
                response=ProviderResponse(
                    content="x", input_tokens=10, output_tokens=5, cost_usd=0.01,
                ),
            )
        stats = obs.stats()
        assert stats["totals"]["requests"] == 3
        assert stats["totals"]["errors"] == 0
        assert stats["totals"]["input_tokens"] == 30
        assert stats["totals"]["output_tokens"] == 15
        assert abs(stats["totals"]["cost_usd"] - 0.03) < 1e-9
        assert stats["totals"]["avg_latency_ms"] > 0

    def test_errors_counted(self):
        obs = RequestObserver()
        o = obs.begin(model_alias="m", stream=False)
        obs.set_provider(o, provider="minimax", key="sk-aaaa", key_total=1)
        obs.finish_error(o, status=502, error="boom")
        stats = obs.stats()
        assert stats["totals"]["requests"] == 1
        assert stats["totals"]["errors"] == 1

    def test_by_key_rollup_separates_keys(self):
        """Issue 2: 'did key A cost more than key B?' must be answerable."""
        obs = RequestObserver()

        # Key A: 2 successful requests at $0.01 each
        for _ in range(2):
            o = obs.begin(model_alias="m", stream=False)
            obs.set_provider(o, provider="minimax", key="sk-aaaa", key_total=2)
            obs.finish_success(
                o, status=200,
                response=ProviderResponse(content="x", cost_usd=0.01),
            )

        # Key B: 1 successful request at $0.05 + 1 error
        o = obs.begin(model_alias="m", stream=False)
        obs.set_provider(o, provider="minimax", key="sk-bbbb", key_total=2)
        obs.finish_success(
            o, status=200,
            response=ProviderResponse(content="x", cost_usd=0.05),
        )
        o = obs.begin(model_alias="m", stream=False)
        obs.set_provider(o, provider="minimax", key="sk-bbbb", key_total=2)
        obs.finish_error(o, status=502, error="upstream")

        stats = obs.stats()
        by_key = {row["key_suffix"]: row for row in stats["by_key"]}
        assert by_key["...aaaa"]["requests"] == 2
        assert by_key["...aaaa"]["errors"] == 0
        assert abs(by_key["...aaaa"]["cost_usd"] - 0.02) < 1e-9
        assert by_key["...bbbb"]["requests"] == 2
        assert by_key["...bbbb"]["errors"] == 1
        assert abs(by_key["...bbbb"]["cost_usd"] - 0.05) < 1e-9

    def test_by_model_rollup(self):
        obs = RequestObserver()
        for model in ("m1", "m2", "m1"):
            o = obs.begin(model_alias=model, stream=False)
            obs.set_provider(o, provider="minimax", key="sk-aaaa", key_total=1)
            obs.finish_success(
                o, status=200,
                response=ProviderResponse(content="x", input_tokens=10),
            )
        stats = obs.stats()
        by_model = {row["model"]: row for row in stats["by_model"]}
        assert by_model["m1"]["requests"] == 2
        assert by_model["m2"]["requests"] == 1

    def test_avg_latency_ms_computed(self):
        obs = RequestObserver()
        for _ in range(2):
            o = obs.begin(model_alias="m", stream=False)
            obs.set_provider(o, provider="minimax", key="k", key_total=1)
            obs.finish_success(
                o, status=200, response=ProviderResponse(content="x"),
            )
        stats = obs.stats()
        assert stats["totals"]["avg_latency_ms"] > 0
        assert all("avg_latency_ms" in row for row in stats["by_key"])
        assert all("avg_latency_ms" in row for row in stats["by_model"])

    def test_recent_bounded(self):
        obs = RequestObserver(recent_limit=5)
        for _ in range(10):
            o = obs.begin(model_alias="m", stream=False)
            obs.set_provider(o, provider="minimax", key="k", key_total=1)
            obs.finish_success(
                o, status=200, response=ProviderResponse(content="x"),
            )
        # Only the last 5 are retained.
        assert len(obs.recent(limit=100)) == 5


class TestRequestObserverTraceFile:
    def test_jsonl_line_written_per_request(self, tmp_path: Path):
        trace_file = tmp_path / "trace.jsonl"
        obs = RequestObserver(trace_file=trace_file)
        o = obs.begin(model_alias="m", stream=False)
        obs.set_provider(o, provider="minimax", key="sk-1234abcd", key_total=2)
        obs.finish_success(
            o, status=200,
            response=ProviderResponse(
                content="x", input_tokens=10, output_tokens=5, cost_usd=0.001,
            ),
        )
        line = trace_file.read_text().strip()
        record = json.loads(line)
        assert record["model"] == "m"
        assert record["provider"] == "minimax"
        assert record["key"] == "...abcd"
        assert record["key_total"] == 2
        assert record["status"] == 200
        assert record["input_tokens"] == 10
        assert record["output_tokens"] == 5
        assert record["cost_usd"] == 0.001
        assert record["latency_ms"] > 0
        assert record["request_id"].startswith("req_")
        assert "ts" in record

    def test_trace_file_unwritable_falls_back_to_logger(self, tmp_path: Path):
        """A bad path disables file writes but must not crash requests."""
        # Pointing at a file inside a non-existent directory that we can't
        # create (use a path through an existing file used as a dir).
        blocker = tmp_path / "blocker"
        blocker.write_text("not a dir")
        bad = blocker / "subdir" / "trace.jsonl"
        obs = RequestObserver(trace_file=bad)
        o = obs.begin(model_alias="m", stream=False)
        obs.set_provider(o, provider="minimax", key="k", key_total=1)
        # Must not raise.
        obs.finish_success(
            o, status=200, response=ProviderResponse(content="x"),
        )


# ---- KeyRotator key callback ----

class TestKeyRotatorKeyCallback:
    def test_callback_fires_with_each_attempt(self):
        seen: list[str] = []

        class FakeInner:
            def __init__(self, key: str) -> None:
                self._key = key

            def provider_name(self) -> str:
                return "fake"

            async def messages(self, **_kwargs):
                yield ProviderResponse(content="ok", stop_reason="end_turn")

        rotator = KeyRotator(
            "fake", ["aaaa", "bbbb", "cccc"], 1.0, lambda k: FakeInner(k),
        )
        rotator.set_on_key(seen.append)

        import asyncio

        async def run():
            async for _ in rotator.messages(
                model="m", system_prompt=None, messages=[],
                max_tokens=1, temperature=1.0, tools=None, stream=False,
            ):
                pass

        asyncio.run(run())
        # Rotator picks key at index 0 for the only attempt (no retry needed).
        assert seen == ["aaaa"]

    def test_callback_fires_per_retry(self):
        seen: list[str] = []

        class Flaky:
            def __init__(self, key: str) -> None:
                self._key = key
                self.calls = 0

            def provider_name(self) -> str:
                return "flaky"

            async def messages(self, **_kwargs):
                self.calls += 1
                if self._key == "bad":
                    raise httpx.ConnectError("nope", request=None)
                yield ProviderResponse(content="ok", stop_reason="end_turn")

        import httpx

        rotator = KeyRotator(
            "flaky", ["bad", "good"], 1.0, lambda k: Flaky(k),
        )
        rotator.set_on_key(seen.append)

        import asyncio

        async def run():
            async for _ in rotator.messages(
                model="m", system_prompt=None, messages=[],
                max_tokens=1, temperature=1.0, tools=None, stream=False,
            ):
                pass

        asyncio.run(run())
        assert seen == ["bad", "good"]

    def test_clear_callback(self):
        seen: list[str] = []

        class P:
            def __init__(self, key: str) -> None:
                self._key = key

            def provider_name(self) -> str:
                return "p"

            async def messages(self, **_kwargs):
                yield ProviderResponse(content="ok", stop_reason="end_turn")

        rotator = KeyRotator("p", ["x", "y"], 1.0, lambda k: P(k))
        rotator.set_on_key(seen.append)
        rotator.set_on_key(None)

        import asyncio

        async def run():
            async for _ in rotator.messages(
                model="m", system_prompt=None, messages=[],
                max_tokens=1, temperature=1.0, tools=None, stream=False,
            ):
                pass

        asyncio.run(run())
        assert seen == []

    def test_key_count_property(self):
        rotator = KeyRotator(
            "p", ["a", "b", "c"], 1.0, lambda k: MagicMock(provider_name=lambda: "p"),
        )
        assert rotator.key_count == 3


# ---- /v1/messages wire-up ----

class TestMessagesEndpointObservability:
    @patch("app.api.anthropic.get_provider")
    @patch("app.api.anthropic.get_registry")
    def test_x_request_id_returned_in_headers(
        self, mock_registry, mock_get_provider,
    ):
        mock_registry.return_value.get.return_value = ("minimax", "MiniMax-M1", 1000000)
        mock_provider = MagicMock()
        mock_provider.messages.return_value = _make_async_gen([
            ProviderResponse(content="ok", input_tokens=1, output_tokens=1),
        ])
        mock_get_provider.return_value = mock_provider

        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-minimax-3",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 10,
            },
        )
        assert response.status_code == 200
        rid = response.headers.get("x-request-id")
        assert rid is not None
        assert rid.startswith("req_")

    @patch("app.api.anthropic.get_provider")
    @patch("app.api.anthropic.get_registry")
    def test_inbound_x_request_id_echoed(
        self, mock_registry, mock_get_provider,
    ):
        mock_registry.return_value.get.return_value = ("minimax", "MiniMax-M1", 1000000)
        mock_provider = MagicMock()
        mock_provider.messages.return_value = _make_async_gen([
            ProviderResponse(content="ok", input_tokens=1, output_tokens=1),
        ])
        mock_get_provider.return_value = mock_provider

        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-minimax-3",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 10,
            },
            headers={"X-Request-Id": "client-trace-99"},
        )
        assert response.headers["x-request-id"] == "client-trace-99"

    @patch("app.api.anthropic.get_provider")
    @patch("app.api.anthropic.get_registry")
    def test_x_latency_ms_returned_for_non_streaming(
        self, mock_registry, mock_get_provider,
    ):
        mock_registry.return_value.get.return_value = ("minimax", "MiniMax-M1", 1000000)
        mock_provider = MagicMock()
        mock_provider.messages.return_value = _make_async_gen([
            ProviderResponse(content="ok", input_tokens=1, output_tokens=1),
        ])
        mock_get_provider.return_value = mock_provider

        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-minimax-3",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 10,
            },
        )
        latency = response.headers.get("x-latency-ms")
        assert latency is not None
        assert float(latency) >= 0

    @patch("app.api.anthropic.get_provider")
    @patch("app.api.anthropic.get_registry")
    def test_unknown_model_recorded_as_error(
        self, mock_registry, mock_get_provider,
    ):
        mock_registry.return_value.get.return_value = None
        response = client.post(
            "/v1/messages",
            json={
                "model": "unknown-model",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 10,
            },
        )
        assert response.status_code == 400
        stats = get_observer().stats()
        assert stats["totals"]["errors"] == 1
        recent = get_observer().recent()
        assert recent[-1]["status"] == 400
        assert "unknown-model" in recent[-1]["error"]

    @patch("app.api.anthropic.get_provider")
    @patch("app.api.anthropic.get_registry")
    def test_successful_request_populates_rollup(
        self, mock_registry, mock_get_provider,
    ):
        mock_registry.return_value.get.return_value = ("minimax", "MiniMax-M1", 1000000)
        mock_provider = MagicMock()
        mock_provider.messages.return_value = _make_async_gen([
            ProviderResponse(
                content="ok",
                input_tokens=42,
                output_tokens=7,
                cost_usd=0.005,
                stop_reason="end_turn",
            ),
        ])
        mock_get_provider.return_value = mock_provider

        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-minimax-3",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 10,
            },
        )
        assert response.status_code == 200

        stats = get_observer().stats()
        assert stats["totals"]["requests"] == 1
        assert stats["totals"]["input_tokens"] == 42
        assert stats["totals"]["output_tokens"] == 7
        assert stats["totals"]["errors"] == 0
        assert stats["totals"]["cost_usd"] == 0.005
        # by_model contains the alias the client asked for, not the resolved one.
        models = {row["model"] for row in stats["by_model"]}
        assert "claude-minimax-3" in models

        # The trace line has everything an operator needs.
        recent = get_observer().recent()
        assert len(recent) == 1
        line = recent[0]
        assert line["provider"] == "minimax"
        assert line["model"] == "claude-minimax-3"
        assert line["input_tokens"] == 42
        assert line["output_tokens"] == 7
        assert line["cost_usd"] == 0.005
        assert line["status"] == 200
        assert line["latency_ms"] >= 0
        assert "ts" in line
        assert line["request_id"] == response.headers["x-request-id"]

    @patch("app.api.anthropic.get_provider")
    @patch("app.api.anthropic.get_registry")
    def test_streaming_request_records_final_response(
        self, mock_registry, mock_get_provider,
    ):
        mock_registry.return_value.get.return_value = ("minimax", "MiniMax-M1", 1000000)
        mock_provider = MagicMock()
        mock_provider.messages.return_value = _make_async_gen([
            "data: {\"type\":\"content_block_delta\"}\n\n",
            ProviderResponse(
                content="done",
                input_tokens=20,
                output_tokens=8,
                cost_usd=0.002,
                stop_reason="end_turn",
            ),
        ])
        mock_get_provider.return_value = mock_provider

        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-minimax-3",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 10,
                "stream": True,
            },
        )
        assert response.status_code == 200
        # Streaming response carries X-Request-Id (latency is recorded but
        # only known after the stream ends).
        assert response.headers["x-request-id"].startswith("req_")

        stats = get_observer().stats()
        assert stats["totals"]["requests"] == 1
        assert stats["totals"]["input_tokens"] == 20
        assert stats["totals"]["output_tokens"] == 8
        assert stats["totals"]["cost_usd"] == 0.002
        # Success trace line, not error.
        line = get_observer().recent()[-1]
        assert line["status"] == 200
        assert line["stream"] is True

    @patch("app.api.anthropic.get_provider")
    @patch("app.api.anthropic.get_registry")
    def test_streaming_failure_recorded(
        self, mock_registry, mock_get_provider,
    ):
        mock_registry.return_value.get.return_value = ("minimax", "MiniMax-M1", 1000000)

        async def boom(**_kwargs):
            yield "data: {\"type\":\"ping\"}\n\n"
            raise RuntimeError("upstream kaboom")

        mock_provider = MagicMock()
        mock_provider.messages.side_effect = boom
        mock_get_provider.return_value = mock_provider

        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-minimax-3",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 10,
                "stream": True,
            },
        )
        # The endpoint always returns 200 on streaming; the SSE body
        # carries an error frame, but the trace line records the failure.
        assert response.status_code == 200
        stats = get_observer().stats()
        assert stats["totals"]["errors"] == 1
        line = get_observer().recent()[-1]
        assert line["status"] == 500
        assert "upstream kaboom" in line["error"]

    @patch("app.api.anthropic.get_provider")
    @patch("app.api.anthropic.get_registry")
    def test_provider_invocation_failure_recorded(
        self, mock_registry, mock_get_provider,
    ):
        """Failure when calling provider.messages itself (before iteration)."""
        mock_registry.return_value.get.return_value = ("minimax", "MiniMax-M1", 1000000)
        mock_provider = MagicMock()
        mock_provider.messages.side_effect = RuntimeError("connect refused")
        mock_get_provider.return_value = mock_provider

        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-minimax-3",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 10,
            },
        )
        assert response.status_code == 502
        stats = get_observer().stats()
        assert stats["totals"]["errors"] == 1
        line = get_observer().recent()[-1]
        assert line["status"] == 502
        assert "connect refused" in line["error"]

    @patch("app.api.anthropic.get_provider")
    @patch("app.api.anthropic.get_registry")
    def test_key_rotator_attributes_request_to_successful_key(
        self, mock_registry, mock_get_provider,
    ):
        """End-to-end: when KeyRotator retries, the trace line records
        the key that actually served the response."""
        import asyncio
        import httpx

        from app.providers.base import ProviderResponse

        # Use a custom rotator that fails on the first key and succeeds on
        # the second. Keys are 8+ chars so the ``...XXXX`` suffix format
        # is exercised.
        class FlakyInner:
            def __init__(self, key: str) -> None:
                self._key = key

            def provider_name(self) -> str:
                return "minimax"

            async def messages(self, **_kwargs):
                if self._key == "key-bad-001":
                    raise httpx.ConnectError("nope", request=None)
                yield ProviderResponse(content="ok", cost_usd=0.001)

        rotator = KeyRotator(
            "minimax",
            ["key-bad-001", "key-good-002"],
            1.0,
            lambda k: FlakyInner(k),
        )
        mock_registry.return_value.get.return_value = ("minimax", "MiniMax-M1", 1000000)
        mock_get_provider.return_value = rotator

        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-minimax-3",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 10,
            },
        )
        assert response.status_code == 200
        line = get_observer().recent()[-1]
        # The successful key's suffix wins (record_attempt overwrites).
        # ``...-002`` because ``key[-4:]`` is ``-002`` for ``"key-good-002"``.
        assert line["key"] == "...-002"
        assert line["key_total"] == 2
        assert line["attempts"] == 2


# ---- /admin/stats + /admin/recent ----

class TestAdminObservabilityEndpoints:
    @patch("app.api.anthropic.get_provider")
    @patch("app.api.anthropic.get_registry")
    def test_admin_stats_returns_rollup(
        self, mock_registry, mock_get_provider,
    ):
        mock_registry.return_value.get.return_value = ("minimax", "MiniMax-M1", 1000000)
        mock_provider = MagicMock()
        mock_provider.messages.return_value = _make_async_gen([
            ProviderResponse(
                content="ok", input_tokens=5, output_tokens=2, cost_usd=0.0001,
            ),
        ])
        mock_get_provider.return_value = mock_provider

        # Make one request so there's something to roll up.
        client.post(
            "/v1/messages",
            json={
                "model": "claude-minimax-3",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 10,
            },
        )

        response = client.get("/admin/stats")
        assert response.status_code == 200
        data = response.json()
        assert "totals" in data
        assert "by_key" in data
        assert "by_model" in data
        assert data["totals"]["requests"] == 1
        assert data["totals"]["input_tokens"] == 5
        assert data["totals"]["cost_usd"] == 0.0001
        # by_model keyed by the alias the client used.
        models = {row["model"] for row in data["by_model"]}
        assert "claude-minimax-3" in models

    @patch("app.api.anthropic.get_provider")
    @patch("app.api.anthropic.get_registry")
    def test_admin_recent_returns_tail(
        self, mock_registry, mock_get_provider,
    ):
        mock_registry.return_value.get.return_value = ("minimax", "MiniMax-M1", 1000000)
        mock_provider = MagicMock()
        mock_provider.messages.return_value = _make_async_gen([
            ProviderResponse(content=f"reply-{i}", input_tokens=1, output_tokens=1)
            for i in range(3)
        ])
        mock_get_provider.return_value = mock_provider

        for i in range(3):
            client.post(
                "/v1/messages",
                json={
                    "model": "claude-minimax-3",
                    "messages": [{"role": "user", "content": f"hi {i}"}],
                    "max_tokens": 10,
                },
            )

        response = client.get("/admin/recent?limit=10")
        assert response.status_code == 200
        events = response.json()["events"]
        assert len(events) == 3
        # Each event carries the operator's "which model, which key, which
        # minute" fields.
        for ev in events:
            assert "ts" in ev
            assert "model" in ev
            assert "provider" in ev
            assert "status" in ev
            assert "latency_ms" in ev

    def test_admin_recent_limit_validation(self):
        # limit must be 1..500.
        assert client.get("/admin/recent?limit=0").status_code == 422
        assert client.get("/admin/recent?limit=501").status_code == 422
        assert client.get("/admin/recent?limit=1").status_code == 200


# ---- Settings: trace knobs ----

class TestTraceSettings:
    def test_trace_file_defaults_to_project_logs_dir(self):
        from app.config.settings import Settings

        s = Settings()
        assert s.trace_enabled is True
        assert s.trace_file is not None
        assert s.trace_file.name == "myproxy.jsonl"
        assert "logs" in s.trace_file.parts

    def test_trace_disabled_passes_none_to_observer(self, monkeypatch):
        from app.config import settings as settings_module

        # Pydantic-settings reads ``trace_enabled`` from ``TRACE_ENABLED``
        # (no env_prefix is configured on Settings).
        monkeypatch.setenv("TRACE_ENABLED", "false")
        settings_module._settings = None
        s = settings_module.get_settings()
        assert s.trace_enabled is False
        # Observer should be created with no file target.
        from app.observability import reset_observer
        reset_observer()
        obs = get_observer()
        assert obs._trace_file is None
        settings_module._settings = None