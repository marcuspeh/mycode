"""Application entrypoint — FastAPI app with lifecycle and routers."""
from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.anthropic import router as anthropic_router
from app.api.admin import router as admin_router

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    logger.info("myproxy started")
    yield
    logger.info("myproxy stopped")


app = FastAPI(
    title="myproxy",
    description="Anthropic-compatible proxy for MiniMax and DeepSeek",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(anthropic_router)
app.include_router(admin_router)


@app.get("/health")
async def health():
    """Basic health check."""
    return {"status": "ok"}


@app.head("/")
async def root_head():
    """Root HEAD probe used by Claude Code to validate the base URL."""
    return {}
