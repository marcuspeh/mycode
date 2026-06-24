"""Admin endpoints: model listing and hot reload."""
from __future__ import annotations

from fastapi import APIRouter

from app.registry.model_registry import get_registry

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/models")
async def list_models():
    """Return the current model registry contents."""
    registry = get_registry()
    return {"models": registry.list_models()}


@router.post("/reload")
async def reload_models():
    """Force reload models.yaml from disk."""
    registry = get_registry()
    registry.reload()
    return {"status": "ok", "models": registry.list_models()}
