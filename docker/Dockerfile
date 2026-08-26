FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY patchfrog ./patchfrog
COPY apps ./apps
COPY migrations ./migrations
COPY alembic.ini ./

RUN pip install --no-cache-dir . && \
    groupadd --system patchfrog && \
    useradd --system --gid patchfrog --home-dir /app --no-create-home patchfrog && \
    chown -R patchfrog:patchfrog /app

FROM base AS api
USER patchfrog
EXPOSE 8000
# Liveness only (GET /health/live) -- deliberately not /health/ready:
# a container orchestrator's own healthcheck deciding whether to
# restart the container should never depend on the database/Redis being
# reachable (a real dependency outage must never trigger a restart
# storm). Readiness (whether to *route traffic* to this instance) is a
# separate concern for the orchestrator's own readiness probe, pointed
# at /health/ready instead -- see docs/operations.md.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health/live', timeout=3).status == 200 else 1)"]
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS worker
# git: patchfrog.repository.git shells out to the git binary to clone and
# check out the commit under analysis (see RepositorySnapshotProvider) —
# required for both Phase 2 indexing and Phase 3 analysis to run for real
# PRs, not just local/CLI use against an already-checked-out directory.
#
# cppcheck, clang-tidy: external system binaries the C/C++ analyzer
# adapters shell out to (see patchfrog.analysis.analyzers.cppcheck/
# clang_tidy) — Phase 3 static analysis, must be present wherever
# analysis actually runs, not just the dev venv.
#
# Installed as root before switching to the unprivileged user.
RUN apt-get update && \
    apt-get install --no-install-recommends -y git cppcheck clang-tidy && \
    rm -rf /var/lib/apt/lists/*
USER patchfrog
# Aggregating /metrics endpoint (see patchfrog.ops.metrics) -- only
# actually bound when PROMETHEUS_MULTIPROC_DIR is set in the environment.
EXPOSE 9100
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD ["sh", "-c", "celery -A apps.worker.celery_app inspect ping -d celery@$HOSTNAME --timeout 5"]
# PROMETHEUS_MULTIPROC_DIR (if set) must exist, empty of any stale
# per-PID files from a previous container run, before the celery
# process -- and therefore patchfrog.ops.metrics's module-level
# Counter/Histogram objects -- ever imports; each one opens a file
# there the moment it's constructed. Cleared here, as a shell step
# ahead of the real process, rather than in Python: by the time any
# Python code in this container runs, the metric objects already need
# it to exist.
CMD ["sh", "-c", "[ -n \"$PROMETHEUS_MULTIPROC_DIR\" ] && rm -rf \"$PROMETHEUS_MULTIPROC_DIR\" && mkdir -p \"$PROMETHEUS_MULTIPROC_DIR\"; exec celery -A apps.worker.celery_app worker --loglevel=INFO"]
