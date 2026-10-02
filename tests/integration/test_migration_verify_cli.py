"""M8.13: ``patchfrog migrations verify`` -- text and JSON forms, and
``--persist``."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import async_sessionmaker

from patchfrog.cli import main
from patchfrog.config.settings import get_settings
from patchfrog.executable_verification.sandbox import is_sandbox_available
from patchfrog.persistence.models.repository import RepositoryModel
from tests.support.postgres import POSTGRES_TEST_URL, postgres_engine_or_skip
from tests.support.upstream_cases import CASES_ROOT

DEMO_VERIFIED = CASES_ROOT / "demo_verified"

pytestmark = pytest.mark.skipif(not is_sandbox_available(), reason="verification sandbox unavailable on this host")


def _argv(*, json_output: bool = False) -> list[str]:
    argv = [
        "migrations", "verify", "--old", str(DEMO_VERIFIED / "old.yaml"), "--new", str(DEMO_VERIFIED / "new.yaml"),
        "--hints", str(DEMO_VERIFIED / "hints.yaml"), "--repo", f"demo={DEMO_VERIFIED / 'repo'}",
    ]
    if json_output:
        argv.append("--json")
    return argv


def test_migrations_verify_text(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_argv()) == 0
    text = capsys.readouterr().out
    assert "Migration verification for demo:" in text
    assert "Verification outcome:" in text
    assert "Residual risk:" in text


def test_migrations_verify_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_argv(json_output=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    verification = payload["verification"]["demo"]
    assert verification["outcome"] in {"verified", "partially_verified", "unverified", "human_required"}
    assert "bundle_fingerprint" in verification


async def _cleanup(full_names: list[str], fingerprints: list[str]) -> None:
    engine = await postgres_engine_or_skip("migration_plans")
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            for name in full_names:
                row = (
                    await session.execute(select(RepositoryModel).where(RepositoryModel.full_name == name))
                ).scalar_one_or_none()
                if row is not None:
                    await session.delete(row)
            await session.commit()
            for fingerprint in fingerprints:
                await session.execute(
                    sa_text("DELETE FROM external_change_events WHERE fingerprint = :f"), {"f": fingerprint}
                )
            await session.commit()
    finally:
        await engine.dispose()


def test_migrations_verify_persist_records_verification_run(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(_cleanup([], []))  # ensure Postgres is reachable, or skip
    monkeypatch.setenv("DATABASE_URL", POSTGRES_TEST_URL)
    get_settings.cache_clear()
    name = f"test/m8-cli-{uuid.uuid4().hex[:8]}"
    argv = [
        "migrations", "verify", "--old", str(DEMO_VERIFIED / "old.yaml"), "--new", str(DEMO_VERIFIED / "new.yaml"),
        "--hints", str(DEMO_VERIFIED / "hints.yaml"), "--provider", f"acme-{uuid.uuid4().hex[:6]}",
        "--repo", f"{name}={DEMO_VERIFIED / 'repo'}", "--persist", "--json",
    ]
    fingerprint = ""
    try:
        assert main(argv) == 0
        payload = json.loads(capsys.readouterr().out)
        fingerprint = payload["event"]["fingerprint"]
        persisted = payload["persisted"][name]
        assert persisted["patch_created"] is True
        assert persisted["verification_run_created"] is True

        # Re-running is idempotent: the same verification run is reused.
        assert main(argv) == 0
        payload2 = json.loads(capsys.readouterr().out)
        persisted2 = payload2["persisted"][name]
        assert persisted2["verification_run_id"] == persisted["verification_run_id"]
        assert persisted2["verification_run_created"] is False
    finally:
        asyncio.run(_cleanup([name], [fingerprint] if fingerprint else []))
        get_settings.cache_clear()
