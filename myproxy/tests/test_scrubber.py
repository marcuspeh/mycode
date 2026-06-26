"""Tests for the PII/secret scrubber."""
from __future__ import annotations

from app.scrubber.patterns import DEFAULT_PATTERNS
from app.scrubber.scrubber import Scrubber


def make_scrubber() -> Scrubber:
    return Scrubber(DEFAULT_PATTERNS, enabled=True)


class TestScrubber:
    def test_aws_access_key_redacted(self):
        s = make_scrubber()
        result = s.scrub({"text": "key is AKIAIOSFODNN7EXAMPLE"})
        assert "[REDACTED_AWS_KEY]" in result.redacted["text"]
        assert "AKIAIOSFODNN7EXAMPLE" not in result.redacted["text"]
        assert result.events[0]["type"] == "AWS_ACCESS_KEY"

    def test_jwt_redacted(self):
        s = make_scrubber()
        token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature"
        result = s.scrub({"text": f"token={token}"})
        assert "[REDACTED_JWT]" in result.redacted["text"]
        assert token not in result.redacted["text"]

    def test_credit_card_redacted(self):
        s = make_scrubber()
        result = s.scrub({"text": "card: 4111 1111 1111 1111"})
        assert "[REDACTED_CC]" in result.redacted["text"]
        assert "4111" not in result.redacted["text"]

    def test_email_redacted(self):
        s = make_scrubber()
        result = s.scrub({"text": "contact alice@example.com"})
        assert "[REDACTED_EMAIL]" in result.redacted["text"]
        assert "alice@example.com" not in result.redacted["text"]

    def test_anthropic_key_redacted(self):
        s = make_scrubber()
        result = s.scrub({"text": "key=sk-ant-api03-abcdefghijklmnopqrstuvwxyz"})
        assert "[REDACTED_ANTHROPIC_KEY]" in result.redacted["text"]

    def test_openai_key_redacted(self):
        s = make_scrubber()
        result = s.scrub({"text": "key=sk-proj-abcdefghijklmnopqrstuvwxyz"})
        assert "[REDACTED_OPENAI_KEY]" in result.redacted["text"]

    def test_github_token_redacted(self):
        s = make_scrubber()
        result = s.scrub({"text": "ghp_abcdefghijklmnopqrstuvwxyz1234567890abcd"})
        assert "[REDACTED_GITHUB_TOKEN]" in result.redacted["text"]

    def test_recursive_into_nested_messages(self):
        s = make_scrubber()
        payload = {
            "messages": [
                {"role": "user", "content": "my key is AKIAIOSFODNN7EXAMPLE"},
                {"role": "assistant", "content": "ok"},
            ]
        }
        result = s.scrub(payload)
        assert "[REDACTED_AWS_KEY]" in result.redacted["messages"][0]["content"]
        assert "AKIAIOSFODNN7EXAMPLE" not in str(result.redacted)

    def test_recursive_into_tool_use_inputs(self):
        s = make_scrubber()
        payload = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "input": {"path": "/home/AKIAIOSFODNN7EXAMPLE/secrets.txt"},
                        }
                    ],
                }
            ]
        }
        result = s.scrub(payload)
        tool_input = result.redacted["messages"][0]["content"][0]["input"]["path"]
        assert "[REDACTED_AWS_KEY]" in tool_input

    def test_disabled_returns_unchanged(self):
        s = Scrubber(DEFAULT_PATTERNS, enabled=False)
        result = s.scrub({"text": "AKIAIOSFODNN7EXAMPLE"})
        assert "AKIAIOSFODNN7EXAMPLE" in result.redacted["text"]
        assert result.events == []

    def test_no_secrets_no_events(self):
        s = make_scrubber()
        result = s.scrub({"text": "hello world, no secrets here"})
        assert result.events == []

    def test_events_log_field_path_not_value(self):
        s = make_scrubber()
        result = s.scrub({"messages": [{"content": "AKIAIOSFODNN7EXAMPLE"}]})
        assert result.events[0]["field"] == "$.messages[0].content"
        assert "AKIA" not in str(result.events)

    def test_private_key_redacted(self):
        s = make_scrubber()
        key = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEpAIBAAKCAQEAxxxxxxx\n"
            "-----END RSA PRIVATE KEY-----"
        )
        result = s.scrub({"text": key})
        assert "[REDACTED_PRIVATE_KEY]" in result.redacted["text"]

    def test_generic_api_key_redacted(self):
        s = make_scrubber()
        result = s.scrub({"text": "api_key=abcdefghijklmnop12345678"})
        assert "[REDACTED_API_KEY]" in result.redacted["text"]
