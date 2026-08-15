FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
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
# Static analysis engine (Phase 3): cppcheck and clang-tidy are external
# system binaries the C/C++ adapters shell out to (see
# patchfrog.analysis.analyzers.cppcheck/clang_tidy) — they must be present
# wherever analysis actually runs, i.e. the worker image, not just the dev
# venv. Installed as root before switching to the unprivileged user.
RUN apt-get update && \
    apt-get install --no-install-recommends -y cppcheck clang-tidy && \
    rm -rf /var/lib/apt/lists/*
USER patchfrog
CMD ["celery", "-A", "apps.worker.celery_app", "worker", "--loglevel=INFO"]
