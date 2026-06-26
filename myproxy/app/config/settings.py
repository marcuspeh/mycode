"""Pydantic-settings configuration for the proxy."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration sourced from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Provider API keys (optional: a provider is only enabled if its key is set).
    MINIMAX_API_KEY: str | None = None
    DEEPSEEK_API_KEY: str | None = None

    # Filesystem layout (relative to project root unless absolute).
    project_root: Path = Path(__file__).resolve().parents[2]
    config_dir: Path = project_root / "config"
    models_file: Path = config_dir / "models.yaml"

    # HTTP server.
    host: str = "0.0.0.0"
    port: int = 3566

    # Request defaults.
    request_timeout_seconds: float = 300.0

    def model_post_init(self, _ctx: object) -> None:
        """Resolve runtime paths."""
        import os

        config_dir_env = os.getenv("MYPROXY_CONFIG_DIR")
        if config_dir_env:
            self.config_dir = Path(config_dir_env)
            self.models_file = self.config_dir / "models.yaml"


# Singleton-style accessor.
_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide Settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
