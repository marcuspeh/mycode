"""Pydantic-settings configuration for the proxy."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_csv(value: str | None) -> list[str]:
    """Split a comma-separated env var into a list, dropping blanks."""
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


class Settings(BaseSettings):
    """Runtime configuration sourced from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Provider API keys. Comma-separated; rotation order follows env order.
    # A provider is enabled if at least one key is configured.
    MINIMAX_API_KEYS: str | None = None
    DEEPSEEK_API_KEYS: str | None = None

    # Filesystem layout (relative to project root unless absolute).
    project_root: Path = Path(__file__).resolve().parents[2]
    config_dir: Path = project_root / "config"
    models_file: Path = config_dir / "models.yaml"

    # HTTP server.
    host: str = "0.0.0.0"
    port: int = 3566

    # Request defaults.
    request_timeout_seconds: float = 300.0

    # Scrubber.
    scrub_enabled: bool = True

    # Observability
    trace_enabled: bool = True
    trace_file: Path | None = None

    def model_post_init(self, _ctx: object) -> None:
        """Resolve runtime paths."""
        import os

        config_dir_env = os.getenv("MYPROXY_CONFIG_DIR")
        if config_dir_env:
            self.config_dir = Path(config_dir_env)
            self.models_file = self.config_dir / "models.yaml"

        if self.trace_file is None:
            self.trace_file = self.project_root / "logs" / "myproxy.jsonl"

    @property
    def minimax_keys(self) -> list[str]:
        """List of MiniMax API keys (parsed from MINIMAX_API_KEYS)."""
        return _parse_csv(self.MINIMAX_API_KEYS)

    @property
    def deepseek_keys(self) -> list[str]:
        """List of DeepSeek API keys (parsed from DEEPSEEK_API_KEYS)."""
        return _parse_csv(self.DEEPSEEK_API_KEYS)


# Singleton-style accessor.
_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide Settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
