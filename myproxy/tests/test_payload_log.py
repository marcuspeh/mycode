"""Tests for the human-readable request/response payload log.

Covers the ``PayloadLogger`` writer (append, rotation, unwritable path)
and the wire-up through ``/v1/messages`` for both streaming and
non-streaming requests.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app
from app.observability.payload_log import (
    PayloadLogger,
    get_payload_logger,
    reset_payload_logger,
)
from app.providers.base import ProviderResponse

client = TestClient(app)


def _make_async_gen(items: list):
    async def _gen():
        for item in items:
            yield item

    return _gen()


# ---- Helpers ----

@pytest.fixture
def traffic_file(tmp_path: Path, monkeypatch) -> Path:
    """Point the payload log at a temp file for the duration of a test."""
    from app.config import settings as settings_module

    path = tmp_path / "myproxy-traffic.txt"
    monkeypatch.setenv("PAYLOAD_LOG_FILE", str(path))
    monkeypatch.setenv("PAYLOAD_LOG_ENABLED", "true")
    settings_module._settings = None
    reset_payload_logger()
    yield path
    settings_module._settings = None
    reset_payload_logger()


# ---- PayloadLogger unit tests ----

class TestPayloadLogger:
    def test_writes_request_and_response_blocks(self, tmp_path: Path):
        log = PayloadLogger(path=tmp_path / "traffic.txt")
        log.log_exchange(
            request_id="req_1",
            model_alias="claude-lb-minimax-m3",
            upstream_model="MiniMax-M3",
            provider="minimax",
            stream=False,
            status=200,
            latency_ms=12.5,
            request_payload={"model": "MiniMax-M3", "messages": []},
            response_payload={"content": [{"type": "text", "text": "hi"}]},
            input_tokens=3,
            output_tokens=4,
        )
        text = (tmp_path / "traffic.txt").read_text()
        assert "request_id=req_1" in text
        assert "model=claude-lb-minimax-m3" in text
        assert "upstream_model=MiniMax-M3" in text
        assert "provider=minimax" in text
        assert "--- REQUEST ---" in text
        assert "--- RESPONSE ---" in text
        assert "input_tokens=3" in text
        assert "output_tokens=4" in text
        # Bodies are valid JSON blocks.
        assert json.loads(text.split("--- REQUEST ---")[1].split("--- RESPONSE ---")[0]) == {
            "model": "MiniMax-M3",
            "messages": [],
        }

    def test_error_block_replaces_response(self, tmp_path: Path):
        log = PayloadLogger(path=tmp_path / "traffic.txt")
        log.log_exchange(
            request_id="req_2",
            model_alias="m",
            upstream_model="m",
            provider="minimax",
            stream=False,
            status=502,
            latency_ms=1.0,
            request_payload={"model": "m"},
            error="provider error: boom",
        )
        text = (tmp_path / "traffic.txt").read_text()
        assert "--- ERROR ---" in text
        assert "provider error: boom" in text
        assert "--- RESPONSE ---" not in text

    def test_appends_rather_than_truncates(self, tmp_path: Path):
        path = tmp_path / "traffic.txt"
        log = PayloadLogger(path=path)
        for i in range(3):
            log.log_exchange(
                request_id=f"req_{i}",
                model_alias="m",
                upstream_model="m",
                provider="minimax",
                stream=False,
                status=200,
                latency_ms=1.0,
                request_payload={"i": i},
            )
        text = path.read_text()
        assert text.count("--- REQUEST ---") == 3
        assert "req_0" in text and "req_2" in text

    def test_rotates_when_file_exceeds_max_bytes(self, tmp_path: Path):
        path = tmp_path / "traffic.txt"
        log = PayloadLogger(path=path, max_bytes=200, backups=1)
        for i in range(10):
            log.log_exchange(
                request_id=f"req_{i}",
                model_alias="m",
                upstream_model="m",
                provider="minimax",
                stream=False,
                status=200,
                latency_ms=1.0,
                request_payload={"i": i},
            )
        rotated = tmp_path / "traffic.1.txt"
        assert rotated.exists()
        assert path.exists()
        # The live file holds only the most recent entries.
        assert path.stat().st_size <= 400

    def test_zero_backups_truncates_instead_of_rotating(self, tmp_path: Path):
        path = tmp_path / "traffic.txt"
        log = PayloadLogger(path=path, max_bytes=200, backups=0)
        for i in range(10):
            log.log_exchange(
                request_id=f"req_{i}",
                model_alias="m",
                upstream_model="m",
                provider="minimax",
                stream=False,
                status=200,
                latency_ms=1.0,
                request_payload={"i": i},
            )
        assert not (tmp_path / "traffic.1.txt").exists()
        assert path.stat().st_size <= 400

    def test_unwritable_path_disables_logging(self, tmp_path: Path):
        blocker = tmp_path / "blocker"
        blocker.write_text("not a dir")
        log = PayloadLogger(path=blocker / "sub" / "traffic.txt")
        assert log.path is None
        # Must not raise.
        log.log_exchange(
            request_id="req_3",
            model_alias="m",
            upstream_model="m",
            provider="minimax",
            stream=False,
            status=200,
            latency_ms=1.0,
            request_payload={"m": 1},
        )

    def test_disabled_logger_writes_nothing(self, tmp_path: Path):
        path = tmp_path / "traffic.txt"
        log = PayloadLogger(path=None)
        log.log_exchange(
            request_id="req_4",
            model_alias="m",
            upstream_model="m",
            provider="minimax",
            stream=False,
            status=200,
            latency_ms=1.0,
            request_payload={"m": 1},
        )
        assert not path.exists()


# ---- Settings ----

class TestPayloadLogSettings:
    def test_defaults_to_project_logs_dir(self):
        from app.config.settings import Settings

        s = Settings()
        assert s.payload_log_enabled is True
        assert s.payload_log_file is not None
        assert s.payload_log_file.name == "myproxy-traffic.txt"
        assert "logs" in s.payload_log_file.parts

    def test_disabled_passes_none_to_logger(self, monkeypatch):
        from app.config import settings as settings_module

        monkeypatch.setenv("PAYLOAD_LOG_ENABLED", "false")
        settings_module._settings = None
        reset_payload_logger()
        assert get_payload_logger().path is None
        settings_module._settings = None
        reset_payload_logger()


# ---- /v1/messages wire-up ----

class TestMessagesEndpointPayloadLog:
    @patch("app.api.anthropic.get_provider")
    @patch("app.api.anthropic.get_registry")
    def test_non_streaming_request_logged(
        self, mock_registry, mock_get_provider, traffic_file: Path,
    ):
        mock_registry.return_value.get.return_value = ("minimax", "MiniMax-M3", 1000000)
        mock_provider = MagicMock()
        mock_provider.build_payload.side_effect = lambda **kw: {
            "model": kw["model"],
            "messages": kw["messages"],
            "max_tokens": kw["max_tokens"],
        }
        mock_provider.messages.return_value = _make_async_gen([
            ProviderResponse(
                content="hello there",
                input_tokens=11,
                output_tokens=5,
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

        text = traffic_file.read_text()
        assert "--- REQUEST ---" in text
        assert "--- RESPONSE ---" in text
        assert "hello there" in text
        assert "MiniMax-M3" in text
        assert "status=200" in text

    @patch("app.api.anthropic.get_provider")
    @patch("app.api.anthropic.get_registry")
    def test_streaming_request_logged_as_reassembled_message(
        self, mock_registry, mock_get_provider, traffic_file: Path,
    ):
        mock_registry.return_value.get.return_value = ("minimax", "MiniMax-M3", 1000000)
        mock_provider = MagicMock()
        mock_provider.build_payload.side_effect = lambda **kw: {
            "model": kw["model"],
            "messages": kw["messages"],
        }
        mock_provider.messages.return_value = _make_async_gen([
            'event: message_start\ndata: {"type":"message_start","message":'
            '{"id":"msg_1","model":"MiniMax-M3","usage":{"input_tokens":9}}}\n\n',
            'event: content_block_start\ndata: {"type":"content_block_start",'
            '"index":0,"content_block":{"type":"text","text":""}}\n\n',
            'event: content_block_delta\ndata: {"type":"content_block_delta",'
            '"index":0,"delta":{"type":"text_delta","text":"streamed reply"}}\n\n',
            'event: message_delta\ndata: {"type":"message_delta",'
            '"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":4}}\n\n',
            'event: message_stop\ndata: {"type":"message_stop"}\n\n',
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

        text = traffic_file.read_text()
        assert "--- RESPONSE ---" in text
        # The reassembled message carries the streamed text, not raw frames.
        assert "streamed reply" in text
        assert "content_block_delta" not in text
        assert "input_tokens=9" in text
        assert "output_tokens=4" in text
        # A stream that ends without a terminal ProviderResponse is still
        # a successful request — the events are reassembled into one.
        assert "status=200" in text
        assert "--- ERROR ---" not in text

    @patch("app.api.anthropic.get_provider")
    @patch("app.api.anthropic.get_registry")
    def test_secrets_are_scrubbed_in_log(
        self, mock_registry, mock_get_provider, traffic_file: Path,
    ):
        mock_registry.return_value.get.return_value = ("minimax", "MiniMax-M3", 1000000)
        mock_provider = MagicMock()
        mock_provider.build_payload.side_effect = lambda **kw: {
            "model": kw["model"],
            "messages": kw["messages"],
        }
        mock_provider.messages.return_value = _make_async_gen([
            ProviderResponse(content="ok", input_tokens=1, output_tokens=1),
        ])
        mock_get_provider.return_value = mock_provider

        client.post(
            "/v1/messages",
            json={
                "model": "claude-minimax-3",
                "messages": [
                    {"role": "user", "content": "mail me at bob@example.com"}
                ],
                "max_tokens": 10,
            },
        )

        text = traffic_file.read_text()
        assert "bob@example.com" not in text
        assert "[REDACTED_EMAIL]" in text

    @patch("app.api.anthropic.get_provider")
    @patch("app.api.anthropic.get_registry")
    def test_provider_failure_logged_with_error(
        self, mock_registry, mock_get_provider, traffic_file: Path,
    ):
        mock_registry.return_value.get.return_value = ("minimax", "MiniMax-M3", 1000000)

        async def boom(**_kwargs):
            raise RuntimeError("connect refused")
            yield  # pragma: no cover

        mock_provider = MagicMock()
        mock_provider.build_payload.side_effect = lambda **kw: {"model": kw["model"]}
        mock_provider.messages.side_effect = boom
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

        text = traffic_file.read_text()
        assert "--- ERROR ---" in text
        assert "connect refused" in text

    @patch("app.api.anthropic.get_registry")
    def test_unknown_model_logged_as_rejected(
        self, mock_registry, traffic_file: Path,
    ):
        mock_registry.return_value.get.return_value = None

        response = client.post(
            "/v1/messages",
            json={
                "model": "nope",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 10,
            },
        )
        assert response.status_code == 400

        text = traffic_file.read_text()
        assert "--- ERROR ---" in text
        assert "unknown model: nope" in text
        assert "status=400" in text
