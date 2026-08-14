from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness check. Deliberately has no dependencies on the DB or GitHub."""

    return {"status": "ok"}
