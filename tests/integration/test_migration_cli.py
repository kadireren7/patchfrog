"""M7.8: ``migrations plan`` / ``migrations generate`` / ``migrations
demo`` -- text and JSON forms, ``--write`` / ``--output-dir``, and
``--persist`` on a real, migrated PostgreSQL."""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from patchfrog.cli import main
from patchfrog.config.settings import get_settings
from patchfrog.persistence.models.repository import RepositoryModel
from tests.support.postgres import POSTGRES_TEST_URL, postgres_engine_or_skip
from tests.support.upstream_cases import CASES_ROOT

DEMO = CASES_ROOT / "demo"
REQUIRED_PARAM = CASES_ROOT / "required_parameter_added"


def test_migrations_demo_end_to_end(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["migrations", "demo"]) == 0
    text = capsys.readouterr().out
    assert "risk: BREAKING" in text
    assert "PLANNED" in text and "residual risk: high" in text
    assert "responses.create" in text and "chat.create" in text
    assert "Patch (deterministic): candidate" in text
    assert "gate OK  syntax" in text
    assert "-    result = client.chat.create" in text
    assert "+    result = client.responses.create" in text

    assert main(["migrations", "demo", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["plan"]["status"] == "planned"
    assert payload["patch"]["is_candidate"] is True
    assert payload["patch"]["origin"] == "deterministic"
    assert {"app/ai/chat.py", "workers/summary.py", "requirements.txt"} == set(payload["patch"]["modified_files"])


def test_migrations_plan_text_and_json(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["migrations", "plan", "--old", str(REQUIRED_PARAM / "old.yaml"), "--new",
            str(REQUIRED_PARAM / "new.yaml"), "--hints", str(REQUIRED_PARAM / "hints.yaml"), "--repo",
            f"repo={REQUIRED_PARAM / 'repo'}"]
    assert main(argv) == 0
    text = capsys.readouterr().out
    assert "Migration plan for repo: PLANNED" in text
    assert "add_required_parameter @ payments/charges.py::create_charge" in text

    assert main([*argv, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    plan = payload["plans"]["repo"]
    assert plan["status"] == "planned"
    steps = {(s["strategy"], s["target"]["location"]) for s in plan["steps"]}
    assert ("add_required_parameter", "payments/charges.py::create_charge") in steps
    assert ("add_required_parameter", "payments/refunds.py::refund") in steps
    (refund_step,) = [s for s in plan["steps"] if s["target"]["location"] == "payments/refunds.py::refund"]
    assert refund_step["eligibility"] == "human_required" and refund_step["operation"] is None


def test_migrations_generate_never_writes_by_default(capsys: pytest.CaptureFixture[str], tmp_path: object) -> None:
    from pathlib import Path

    root = Path(str(tmp_path)) / "repo"
    shutil.copytree(DEMO / "repo", root)
    before = (root / "app/ai/chat.py").read_text()
    argv = ["migrations", "generate", "--old", str(DEMO / "old.yaml"), "--new", str(DEMO / "new.yaml"), "--hints",
            str(DEMO / "hints.yaml"), "--repo", f"demo={root}"]
    assert main(argv) == 0
    capsys.readouterr()
    assert (root / "app/ai/chat.py").read_text() == before  # unchanged: --write was not given


def test_migrations_generate_write_and_output_dir(capsys: pytest.CaptureFixture[str], tmp_path: object) -> None:
    from pathlib import Path

    root = Path(str(tmp_path)) / "repo"
    shutil.copytree(DEMO / "repo", root)
    out_dir = Path(str(tmp_path)) / "patches"
    argv = ["migrations", "generate", "--old", str(DEMO / "old.yaml"), "--new", str(DEMO / "new.yaml"), "--hints",
            str(DEMO / "hints.yaml"), "--repo", f"demo={root}", "--write", "--output-dir", str(out_dir)]
    assert main(argv) == 0
    capsys.readouterr()
    assert "client.responses.create" in (root / "app/ai/chat.py").read_text()
    assert (out_dir / "demo.patch").is_file()
    assert "-    result = client.chat.create" in (out_dir / "demo.patch").read_text()

    # Re-running against the already-migrated checkout is a no-op (no
    # write happens a second time, no crash, still exit 0).
    after_first_write = (root / "app/ai/chat.py").read_text()
    assert main(argv) == 0
    capsys.readouterr()
    assert (root / "app/ai/chat.py").read_text() == after_first_write


@pytest.mark.parametrize(
    "argv",
    [
        ["migrations", "plan", "--old", str(DEMO / "old.yaml"), "--new", str(DEMO / "new.yaml")],  # no --repo
        ["migrations", "generate", "--to-version", "2.0.0", "--repo", str(DEMO / "repo")],  # no --package
    ],
)
def test_bad_inputs_exit_2_without_a_traceback(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "Traceback" not in err


# -- Postgres ----------------------------------------------------------------------------


async def _require_postgres() -> None:
    engine = await postgres_engine_or_skip("migration_plans")
    await engine.dispose()


async def _cleanup(full_names: list[str], fingerprints: list[str]) -> None:
    from sqlalchemy import text as sa_text

    engine = await postgres_engine_or_skip("migration_plans")
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            for name in full_names:
                row = (await session.execute(select(RepositoryModel).where(RepositoryModel.full_name == name))
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


def test_migrations_generate_persist_is_idempotent(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(_require_postgres())
    monkeypatch.setenv("DATABASE_URL", POSTGRES_TEST_URL)
    get_settings.cache_clear()
    name = f"test/m7-cli-{uuid.uuid4().hex[:8]}"
    argv = ["migrations", "generate", "--old", str(DEMO / "old.yaml"), "--new", str(DEMO / "new.yaml"), "--hints",
            str(DEMO / "hints.yaml"), "--provider", f"acme-{uuid.uuid4().hex[:6]}", "--repo", f"{name}={DEMO / 'repo'}",
            "--persist", "--json"]
    fingerprint = ""
    try:
        assert main(argv) == 0
        first = json.loads(capsys.readouterr().out)
        fingerprint = first["event"]["fingerprint"]
        persisted = first["persisted"][name]
        assert persisted["event_created"] is True and persisted["plan_created"] is True
        assert persisted["patch_created"] is True

        assert main(argv) == 0
        second = json.loads(capsys.readouterr().out)
        persisted2 = second["persisted"][name]
        assert persisted2["event_created"] is False
        assert persisted2["plan_id"] == persisted["plan_id"]
        assert persisted2["plan_created"] is False
        assert persisted2["patch_id"] == persisted["patch_id"]
        assert persisted2["patch_created"] is False
    finally:
        asyncio.run(_cleanup([name], [fingerprint] if fingerprint else []))
        get_settings.cache_clear()


def test_migrations_plan_persist_without_generate(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(_require_postgres())
    monkeypatch.setenv("DATABASE_URL", POSTGRES_TEST_URL)
    get_settings.cache_clear()
    name = f"test/m7-plan-{uuid.uuid4().hex[:8]}"
    argv = ["migrations", "plan", "--old", str(REQUIRED_PARAM / "old.yaml"), "--new",
            str(REQUIRED_PARAM / "new.yaml"), "--hints", str(REQUIRED_PARAM / "hints.yaml"),
            "--provider", f"paylane-{uuid.uuid4().hex[:6]}", "--repo", f"{name}={REQUIRED_PARAM / 'repo'}",
            "--persist", "--json"]
    fingerprint = ""
    try:
        assert main(argv) == 0
        payload = json.loads(capsys.readouterr().out)
        fingerprint = payload["event"]["fingerprint"]
        persisted = payload["persisted"][name]
        assert persisted["plan_created"] is True
        assert "patch_id" not in persisted  # `plan` never generates or persists a patch
    finally:
        asyncio.run(_cleanup([name], [fingerprint] if fingerprint else []))
        get_settings.cache_clear()
