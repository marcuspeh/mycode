"""Admin endpoints: model listing, hot reload, observability."""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.observability import get_observer
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


@router.get("/stats")
async def stats():
    """Return per-request rollups for observability (issue 2).

    Shape::

        {
          "totals":     {requests, errors, input_tokens, ...},
          "by_key":     [{provider, key_suffix, requests, avg_latency_ms, ...}, ...],
          "by_model":   [{provider, model, requests, avg_latency_ms, ...}, ...],
          "recent_count": N,
        }

    Answers "did key A cost more than key B?" and "which model spiked
    latency this minute?".
    """
    return get_observer().stats()


@router.get("/recent")
async def recent(limit: int = Query(default=50, ge=1, le=500)):
    """Tail the last ``limit`` per-request trace snapshots."""
    return {"events": get_observer().recent(limit=limit)}
