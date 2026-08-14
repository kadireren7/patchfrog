from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from apps.api.routes import github_webhooks, health
from patchfrog.config.logging import configure_logging
from patchfrog.config.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="PatchFrog",
        description="GitHub-native code review platform (Phase 1: ingestion foundation)",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(github_webhooks.router)
    return app


app = create_app()
