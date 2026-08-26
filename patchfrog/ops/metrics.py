"""Prometheus-compatible operational metrics.

Every counter/histogram here is deliberately low-cardinality: labels are
bounded, closed sets (status, error category, provider/model name, pipeline
stage) -- never a repository name, installation, PR number, username, or
any piece of finding/source text (spec section 16: "Avoid high-cardinality
labels" / "Do not leak repo names, usernames, source code, finding
text"). Exposed at ``GET /metrics`` (see :mod:`apps.api.routes.metrics`)
in the standard Prometheus text exposition format via the official
``prometheus_client`` library -- never a hand-rolled exporter.

Every counter defined here is incremented exclusively from worker-side
task code (``apps/worker/tasks/*.py``, :mod:`patchfrog.feedback.sync`) --
the API process itself never increments any of them, it only serves
``GET /metrics``. Found via dogfooding the local production-like stack
(the API's own ``/metrics`` reported an unchanged ``0`` immediately after
a real review had actually started in the worker container): the API and
worker are separate OS processes -- separate containers, even -- and
``prometheus_client``'s default registry lives in one process's memory,
never shared across processes. A plain ``generate_latest()`` on the API
side can therefore never see a worker-incremented counter, no matter how
much traffic flows through the worker, in *any* real multi-process
deployment of this codebase -- not just this one's Docker Compose setup.
The fix is ``prometheus_client``'s documented "multiprocess mode": set
``PROMETHEUS_MULTIPROC_DIR`` to a real directory before the worker starts
(see the worker service in ``docker-compose.yml`` and
``docs/deployment.md``), which makes every ``Counter``/``Histogram``
write to per-process files there instead of process memory, transparent
to the metric definitions below. :func:`start_worker_metrics_server`
then serves an aggregating ``/metrics`` HTTP endpoint (default port
9100) directly from the worker container, reading across every forked
worker subprocess's files -- Prometheus is meant to scrape *both*
endpoints (API ``:8000/metrics`` and worker ``:9100/metrics``) as
separate targets; the API's own endpoint legitimately stays empty for
every metric on this page, since it never increments any of them.
"""

from __future__ import annotations

import os

import structlog
from prometheus_client import Counter, Histogram

logger = structlog.get_logger(__name__)

reviews_started_total = Counter("patchfrog_reviews_started_total", "Review runs started")
reviews_completed_total = Counter(
    "patchfrog_reviews_completed_total", "Review runs that reached a terminal status", ["status"]
)
reviews_failed_total = Counter(
    "patchfrog_reviews_failed_total", "Review runs that failed, by error category", ["error_category"]
)
reviews_skipped_total = Counter(
    "patchfrog_reviews_skipped_total", "Reviews skipped before running", ["reason"]
)

review_duration_seconds = Histogram(
    "patchfrog_review_duration_seconds", "Wall-clock duration of one AI review run"
)
queue_wait_seconds = Histogram(
    "patchfrog_queue_wait_seconds", "Time between a task being enqueued and starting execution", ["task"]
)

repository_index_duration_seconds = Histogram(
    "patchfrog_repository_index_duration_seconds", "Wall-clock duration of one repository indexing run"
)
static_analysis_duration_seconds = Histogram(
    "patchfrog_static_analysis_duration_seconds", "Wall-clock duration of one static analysis run"
)
context_duration_seconds = Histogram(
    "patchfrog_context_duration_seconds", "Wall-clock duration of one context-bundle build"
)
ai_review_duration_seconds = Histogram(
    "patchfrog_ai_review_duration_seconds", "Wall-clock duration of the AI review stage specifically"
)
publication_duration_seconds = Histogram(
    "patchfrog_publication_duration_seconds", "Wall-clock duration of one publish attempt"
)

provider_calls_total = Counter(
    "patchfrog_provider_calls_total", "LLM provider calls made", ["provider", "model", "role"]
)
provider_errors_total = Counter(
    "patchfrog_provider_errors_total", "LLM provider calls that failed", ["provider", "model"]
)
provider_input_tokens_total = Counter(
    "patchfrog_provider_input_tokens_total", "LLM provider input tokens consumed", ["provider", "model"]
)
provider_output_tokens_total = Counter(
    "patchfrog_provider_output_tokens_total", "LLM provider output tokens produced", ["provider", "model"]
)

findings_generated_total = Counter(
    "patchfrog_findings_generated_total", "Findings proposed by the AI reviewer, before validation/critic"
)
findings_published_total = Counter(
    "patchfrog_findings_published_total", "Findings actually written to GitHub as inline comments"
)
findings_suppressed_total = Counter(
    "patchfrog_findings_suppressed_total", "Findings suppressed as duplicates or below threshold", ["reason"]
)

feedback_events_total = Counter(
    "patchfrog_feedback_events_total", "Raw feedback events ingested", ["event_type"]
)


def start_worker_metrics_server(port: int) -> bool:
    """Serve an aggregating ``/metrics`` HTTP endpoint from the worker
    container -- see the module docstring for why this exists at all.

    A no-op (returns ``False``) unless ``PROMETHEUS_MULTIPROC_DIR`` is
    set: a developer running ``celery worker`` locally without it configured
    should get ordinary single-process metrics behavior (or none), never a
    crash.

    Deliberately does *not* create or clear ``PROMETHEUS_MULTIPROC_DIR``
    itself: every ``Counter``/``Histogram`` in this module already writes
    there the moment it's constructed, at *import* time -- which happens
    before this function (called from ``celery.signals.worker_init``)
    ever runs, and happens again independently in every forked worker
    subprocess. The directory must exist, already empty of stale files
    from a previous container run, before the Python process starts at
    all -- see the worker target's ``CMD`` in ``docker/Dockerfile``,
    which clears and recreates it as a shell step ahead of ``exec
    celery ...``. Found the hard way: an earlier version of this function
    tried to create the directory here and crashed the worker at import
    time with ``FileNotFoundError`` -- by the time this ran, the first
    ``Counter(...)`` at the top of this module had already tried (and
    failed) to open a file inside a directory that didn't exist yet.

    Call once, from the Celery *parent* process before it forks worker
    subprocesses (``celery.signals.worker_init``) -- the aggregating
    HTTP server then lives in the long-running parent and can see every
    child's file as it's written, for the life of the container.
    """

    multiproc_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if not multiproc_dir:
        return False

    # Imported lazily: only meaningful (and only importable without extra
    # optional deps in some prometheus_client versions) once multiprocess
    # mode is actually in play.
    from prometheus_client import CollectorRegistry, multiprocess, start_http_server

    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
    start_http_server(port, registry=registry)
    logger.info("worker_metrics_server_started", port=port, multiproc_dir=multiproc_dir)
    return True


def mark_worker_process_dead(pid: int) -> None:
    """Clean up one forked worker subprocess's metric files on exit --
    call from ``celery.signals.worker_process_shutdown``. A no-op when
    multiprocess mode isn't configured, matching
    :func:`start_worker_metrics_server`."""

    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        return
    from prometheus_client import multiprocess

    multiprocess.mark_process_dead(pid)  # type: ignore[no-untyped-call]
