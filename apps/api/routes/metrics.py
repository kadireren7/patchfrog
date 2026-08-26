"""``GET /metrics`` -- Prometheus text-exposition format, from this
process's own registry. The API process never increments any of the
review/pipeline/publication counters defined in
:mod:`patchfrog.ops.metrics` -- those all happen in worker task code --
so every one of them legitimately reads zero here forever; scrape the
worker container's own aggregating endpoint (default port 9100,
:func:`patchfrog.ops.metrics.start_worker_metrics_server`) for real
data. See that module's docstring for the full explanation, and
``docs/deployment.md``'s Metrics section for the two-target scrape
setup this implies.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

router = APIRouter()


@router.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
