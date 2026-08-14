.PHONY: install test lint format typecheck run worker up down migrate

VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

install:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check .

format:
	$(PYTHON) -m ruff format .
	$(PYTHON) -m ruff check --fix .

typecheck:
	$(PYTHON) -m mypy

run:
	$(PYTHON) -m uvicorn apps.api.main:app --reload --host 0.0.0.0 --port 8000

worker:
	$(PYTHON) -m celery -A apps.worker.celery_app worker --loglevel=INFO

up:
	docker compose up --build

down:
	docker compose down

migrate:
	$(PYTHON) -m alembic upgrade head
