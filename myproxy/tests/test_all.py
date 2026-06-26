"""Tests for myproxy."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app
from app.registry.model_registry import ModelRegistry

client = TestClient(app)


# ---- Helpers ----

@pytest.fixture
def temp_models_file():
    """Create a temporary models.yaml."""
    content = """
models:
  claude-minimax-3:
    provider: minimax
    model: MiniMax-M1
  claude-deepseek3:
    provider: deepseek
    model: deepseek-chat
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False
    ) as f:
        f.write(content)
        path = Path(f.name)

    yield path
    path.unlink(missing_ok=True)


def _make_async_gen(items: list):
    """Create a proper async generator from a list of items for mocking."""
    async def _gen():
        for item in items:
            yield item
    return _gen()


# ---- Model Registry Tests ----

class TestModelRegistry:
    def test_loads_models(self, temp_models_file: Path):
        registry = ModelRegistry(temp_models_file)
        assert registry.get("claude-minimax-3") == ("minimax", "MiniMax-M1")
        assert registry.get("claude-deepseek3") == ("deepseek", "deepseek-chat")

    def test_unknown_model_returns_none(self, temp_models_file: Path):
        registry = ModelRegistry(temp_models_file)
        assert registry.get("nonexistent") is None

    def test_list_models(self, temp_models_file: Path):
        registry = ModelRegistry(temp_models_file)
        models = registry.list_models()
        assert "claude-minimax-3" in models
        assert "claude-deepseek3" in models

    def test_hot_reload(self, temp_models_file: Path):
        registry = ModelRegistry(temp_models_file)
        assert "claude-deepseek3" in registry.list_models()

        new_content = """
models:
  claude-new-model:
    provider: minimax
    model: MiniMax-2.7
"""
        temp_models_file.write_text(new_content)

        assert registry.get("claude-new-model") == ("minimax", "MiniMax-2.7")
        assert registry.get("claude-deepseek3") is None

    def test_missing_file(self):
        registry = ModelRegistry(Path("/nonexistent/models.yaml"))
        assert registry.list_models() == {}
        assert registry.get("any") is None


# ---- API Endpoint Tests ----

class TestHealthEndpoint:
    def test_health_returns_ok(self):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestAdminEndpoints:
    def test_list_models_returns_dict(self):
        response = client.get("/admin/models")
        assert response.status_code == 200
        data = response.json()
        assert "models" in data

    def test_reload_models(self):
        response = client.post("/admin/reload")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestMessagesEndpoint:
    def test_unknown_model_returns_400(self):
        response = client.post(
            "/v1/messages",
            json={
                "model": "unknown-model-xyz",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 100,
            },
        )
        assert response.status_code == 400

    @patch("app.api.anthropic.get_provider")
    @patch("app.api.anthropic.get_registry")
    def test_missing_provider_key_returns_400(self, mock_registry, mock_get_provider):
        mock_registry.return_value.get.return_value = ("deepseek", "deepseek-chat")
        mock_get_provider.return_value = None

        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-deepseek3",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 100,
            },
        )
        assert response.status_code == 400

    @patch("app.api.anthropic.get_provider")
    @patch("app.api.anthropic.get_registry")
    def test_successful_non_streaming_request(
        self, mock_registry, mock_get_provider
    ):
        from app.providers.base import ProviderResponse

        mock_registry.return_value.get.return_value = ("minimax", "MiniMax-M1")
        mock_provider = MagicMock()
        mock_provider.messages.return_value = _make_async_gen([
            ProviderResponse(
                content="Hello from MiniMax",
                stop_reason="end_turn",
                input_tokens=10,
                output_tokens=5,
                cost_usd=0.001,
            )
        ])
        mock_get_provider.return_value = mock_provider

        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-minimax-3",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 100,
                "stream": False,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["role"] == "assistant"
        assert data["type"] == "message"
        assert data["stop_reason"] == "end_turn"
        assert data["usage"]["input_tokens"] == 10
        assert data["usage"]["output_tokens"] == 5

    @patch("app.api.anthropic.get_provider")
    @patch("app.api.anthropic.get_registry")
    def test_streaming_request(self, mock_registry, mock_get_provider):
        from app.providers.base import ProviderResponse

        mock_registry.return_value.get.return_value = ("minimax", "MiniMax-M1")
        mock_provider = MagicMock()
        mock_provider.messages.return_value = _make_async_gen([
            "data: {\"type\":\"content_block_delta\"}\n\n",
            ProviderResponse(
                content="Hello from stream",
                stop_reason="end_turn",
                input_tokens=10,
                output_tokens=5,
                cost_usd=0.001,
            ),
        ])
        mock_get_provider.return_value = mock_provider

        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-minimax-3",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 100,
                "stream": True,
            },
        )
        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")


# ---- Provider Response Parsing Tests ----

class TestMiniMaxProvider:
    def test_parse_response(self):
        from app.providers.minimax import MiniMaxProvider

        provider = MiniMaxProvider("fake-key")
        data = {
            "content": [
                {"type": "text", "text": "Hello world"},
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "read_file",
                    "input": {"path": "/tmp/test"},
                },
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
        result = provider._parse_response(data)
        assert "Hello world" in result.content
        assert "tool_use" in result.content
        assert result.stop_reason == "end_turn"
        assert result.input_tokens == 100
        assert result.output_tokens == 50


class TestDeepSeekProvider:
    def test_parse_response(self):
        from app.providers.deepseek import DeepSeekProvider

        provider = DeepSeekProvider("fake-key")
        data = {
            "content": [
                {"type": "text", "text": "Hello from DeepSeek"},
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "read_file",
                    "input": {"path": "/tmp/test"},
                },
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 20, "output_tokens": 30},
        }
        result = provider._parse_response(data)
        assert "Hello from DeepSeek" in result.content
        assert "tool_use" in result.content
        assert result.stop_reason == "end_turn"
        assert result.input_tokens == 20
        assert result.output_tokens == 30
