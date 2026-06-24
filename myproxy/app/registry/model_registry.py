"""Hot-reloading model registry backed by models.yaml."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class ModelRegistry:
    """Maps external model names to (provider, model) tuples.

    Watches models.yaml for changes and hot-reloads automatically.
    """

    def __init__(self, models_file: Path) -> None:
        self._models_file = models_file
        self._models: dict[str, dict[str, str]] = {}
        self._mtime: float = 0.0
        self.reload()

    def _file_has_changed(self) -> bool:
        try:
            current = self._models_file.stat().st_mtime
        except FileNotFoundError:
            return False
        return current != self._mtime

    def reload(self) -> None:
        """Reload the model registry from disk."""
        try:
            with open(self._models_file) as f:
                data: dict[str, Any] = yaml.safe_load(f.read()) or {}
        except FileNotFoundError:
            logger.warning("models.yaml not found at %s", self._models_file)
            data = {}

        self._models = data.get("models", {})
        self._mtime = self._models_file.stat().st_mtime if self._models_file.exists() else 0.0
        logger.info("Loaded %d models from %s", len(self._models), self._models_file)

    def _maybe_reload(self) -> None:
        if self._file_has_changed():
            self.reload()

    def get(self, model_name: str) -> tuple[str, str] | None:
        """Return (provider_name, actual_model) or None."""
        self._maybe_reload()
        entry = self._models.get(model_name)
        if entry is None:
            return None
        return entry["provider"], entry["model"]

    def list_models(self) -> dict[str, dict[str, str]]:
        """Return full model mapping for admin API."""
        self._maybe_reload()
        return dict(self._models)


# Process-wide singleton.
_registry: ModelRegistry | None = None


def get_registry() -> ModelRegistry:
    """Return the process-wide ModelRegistry singleton."""
    global _registry
    if _registry is None:
        from app.config.settings import get_settings

        _registry = ModelRegistry(get_settings().models_file)
    return _registry
