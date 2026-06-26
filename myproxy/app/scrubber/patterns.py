"""Built-in regex patterns for secrets and PII."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Pattern:
    """A named regex pattern with its replacement placeholder."""

    name: str
    regex: re.Pattern[str]
    placeholder: str


def _p(name: str, regex: str, placeholder: str) -> Pattern:
    return Pattern(name, re.compile(regex), placeholder)


# Order matters: more specific patterns first.
DEFAULT_PATTERNS: list[Pattern] = [
    _p(
        "PRIVATE_KEY",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----",
        "[REDACTED_PRIVATE_KEY]",
    ),
    _p(
        "AWS_ACCESS_KEY",
        r"\bAKIA[0-9A-Z]{16}\b",
        "[REDACTED_AWS_KEY]",
    ),
    _p(
        "AWS_SECRET_KEY",
        r'(?i)aws(.{0,20})?(secret|sk).{0,20}?["\x27][A-Za-z0-9/+=]{40}["\x27]',
        "[REDACTED_AWS_SECRET]",
    ),
    _p(
        "ANTHROPIC_KEY",
        r"\bsk-ant-[A-Za-z0-9_\-]{20,}",
        "[REDACTED_ANTHROPIC_KEY]",
    ),
    _p(
        "OPENAI_KEY",
        r"\bsk-(?!ant-)[A-Za-z0-9_\-]{20,}",
        "[REDACTED_OPENAI_KEY]",
    ),
    _p(
        "GITHUB_TOKEN",
        r"\bgh[pousr]_[A-Za-z0-9]{36,}\b",
        "[REDACTED_GITHUB_TOKEN]",
    ),
    _p(
        "SLACK_TOKEN",
        r"\bxox[bpars]-[A-Za-z0-9-]{10,}\b",
        "[REDACTED_SLACK_TOKEN]",
    ),
    _p(
        "JWT",
        r"\beyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b",
        "[REDACTED_JWT]",
    ),
    _p(
        "CREDIT_CARD",
        r"\b(?:\d[ -]?){13,19}\b",
        "[REDACTED_CC]",
    ),
    _p(
        "SSN",
        r"\b\d{3}-\d{2}-\d{4}\b",
        "[REDACTED_SSN]",
    ),
    _p(
        "EMAIL",
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
        "[REDACTED_EMAIL]",
    ),
    _p(
        "GENERIC_API_KEY",
        r'(?i)(api[_\-]?key|secret|token|password)\s*[=:]\s*["\x27]?[A-Za-z0-9_\-]{16,}',
        "[REDACTED_API_KEY]",
    ),
]
