from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from apps.api.routes import github_webhooks, health, metrics
from patchfrog.config.logging import configure_logging
from patchfrog.config.settings import get_settings
from patchfrog.persistence.database import create_engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    # A single lightweight, pool_pre_ping engine, shared for the process
    # lifetime -- the one deliberate exception to the API process
    # otherwise never touching the database directly (see
    # apps.api.dependencies' module docstring): GET /health/ready needs a
    # real connection to check readiness, and creating a fresh connection
    # pool on every readiness poll would be wasteful and could exhaust
    # connections under a tight orchestrator poll interval.
    app.state.db_engine = create_engine(settings.database_url)
    yield
    await app.state.db_engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="PatchFrog",
        description="GitHub-native code review platform",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(github_webhooks.router)
    return app


app = create_app()
