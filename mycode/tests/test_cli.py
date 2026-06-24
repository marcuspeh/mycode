"""Tests for mycode CLI."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from typer import Exit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli  # noqa: E402


class TestCollectExtraArgs:
    def test_no_args(self):
        old_argv = sys.argv
        try:
            sys.argv = ["mycode"]
            assert cli._collect_extra_args("any") == []
        finally:
            sys.argv = old_argv

    def test_with_model_flag_value(self):
        old_argv = sys.argv
        try:
            sys.argv = ["mycode", "--model", "minimax-3"]
            assert cli._collect_extra_args("minimax-3") == []
        finally:
            sys.argv = old_argv

    def test_with_model_equals(self):
        old_argv = sys.argv
        try:
            sys.argv = ["mycode", "--model=minimax-3"]
            assert cli._collect_extra_args("minimax-3") == []
        finally:
            sys.argv = old_argv

    def test_extra_flags_passed_through(self):
        old_argv = sys.argv
        try:
            sys.argv = [
                "mycode",
                "--model", "minimax-3",
                "--debug",
                "--some-flag=value",
                "positional-arg",
            ]
            result = cli._collect_extra_args("minimax-3")
            assert result == ["--debug", "--some-flag=value", "positional-arg"]
        finally:
            sys.argv = old_argv

    def test_base_url_and_machine_excluded(self):
        old_argv = sys.argv
        try:
            sys.argv = [
                "mycode",
                "--model", "minimax-3",
                "--base-url", "http://example.com",
                "--machine", "foo",
                "--verbose",
            ]
            result = cli._collect_extra_args("minimax-3")
            assert result == ["--verbose"]
        finally:
            sys.argv = old_argv


class TestFetchModels:
    def test_fetch_success(self):
        mock_request = httpx.Request("GET", "http://test/admin/models")
        mock_response = httpx.Response(
            200,
            request=mock_request,
            json={
                "models": {
                    "claude-minimax-3": {"provider": "minimax", "model": "MiniMax-M1"},
                    "claude-minimax-2.7": {"provider": "minimax", "model": "MiniMax-2.7"},
                }
            },
        )

        with patch("cli.httpx.get", return_value=mock_response):
            models = cli._fetch_models("http://test")
            assert len(models) == 2
            assert models[0]["provider"] == "minimax"

    def test_fetch_failure_exits(self):
        mock_request = httpx.Request("GET", "http://test/admin/models")
        mock_response = httpx.Response(500, request=mock_request, text="server error")

        with patch("cli.httpx.get", return_value=mock_response):
            with pytest.raises(Exit):
                cli._fetch_models("http://test")

    def test_fetch_connection_error_exits(self):
        with patch(
            "cli.httpx.get",
            side_effect=httpx.ConnectError("connection refused"),
        ):
            with pytest.raises(Exit):
                cli._fetch_models("http://test")