"""M5.8: the ``dependencies discover`` CLI -- text and JSON forms, and
``--persist`` into the registry on a real, migrated PostgreSQL."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from patchfrog.cli import main
from patchfrog.config.settings import get_settings
from patchfrog.persistence.models.dependency import ExternalDependencyModel
from patchfrog.persistence.models.repository import RepositoryModel
from tests.support.postgres import POSTGRES_TEST_URL, postgres_engine_or_skip

MIXED = Path(__file__).resolve().parents[1] / "fixtures" / "dependencies" / "mixed_repo"


def test_cli_discover_text_and_json(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert main(["dependencies", "discover", str(MIXED)]) == 0
    text = capsys.readouterr().out
    assert "OpenAI (pypi)" in text and "Stripe (npm)" in text and "GitHub API" in text
    assert "specs: 1" in text and "no secret values read" in text

    out = tmp_path / "inventory.json"
    assert main(["dependencies", "discover", str(MIXED), "--json", "--output", str(out)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == json.loads(out.read_text())
    assert {d["key"] for d in payload["dependencies"]} >= {"openai:pypi", "stripe:npm"}


async def _registry_keys_and_cleanup(full_name: str) -> list[str]:
    engine = await postgres_engine_or_skip("external_dependencies")
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            repository = (await session.execute(
                select(RepositoryModel).where(RepositoryModel.full_name == full_name)
            )).scalar_one()
            keys = list((await session.execute(select(ExternalDependencyModel.dependency_key).where(
                ExternalDependencyModel.repository_id == repository.id
            ))).scalars().all())
            await session.delete(repository)  # cascades to the registry rows
            await session.commit()
        return keys
    finally:
        await engine.dispose()


async def _require_postgres() -> None:
    engine = await postgres_engine_or_skip("external_dependencies")
    await engine.dispose()


def test_cli_persist_records_the_inventory_idempotently(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Synchronous on purpose: the CLI owns its own event loop."""

    asyncio.run(_require_postgres())
    monkeypatch.setenv("DATABASE_URL", POSTGRES_TEST_URL)
    get_settings.cache_clear()
    full_name = f"test/m5-cli-{uuid.uuid4().hex[:8]}"
    try:
        argv = ["dependencies", "discover", str(MIXED), "--persist", "--full-name", full_name, "--json"]
        assert main(argv) == 0
        first = json.loads(capsys.readouterr().out)["registry"]
        assert first["created"] == 5
        assert main(argv) == 0
        second = json.loads(capsys.readouterr().out)["registry"]
        assert (second["created"], second["unchanged"], second["contract_snapshots_created"]) == (0, 5, 0)
        assert "stripe:npm" in asyncio.run(_registry_keys_and_cleanup(full_name))
    finally:
        get_settings.cache_clear()
