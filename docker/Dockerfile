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
CMD ["celery", "-A", "apps.worker.celery_app", "worker", "--loglevel=INFO"]
