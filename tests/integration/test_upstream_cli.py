"""M6.8: ``changes diff`` / ``changes analyze`` -- text and JSON forms,
the three input kinds, and ``--persist`` / ``--from-registry`` /
``--registry`` on a real, migrated PostgreSQL."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from patchfrog.cli import main
from patchfrog.config.settings import get_settings
from patchfrog.persistence.models.repository import RepositoryModel
from patchfrog.persistence.models.upstream import ExternalChangeEventModel
from tests.support.postgres import POSTGRES_TEST_URL, postgres_engine_or_skip
from tests.support.upstream_cases import CASES_ROOT

DEMO = CASES_ROOT / "demo"
GITHUB = CASES_ROOT / "github_endpoint_replacement"
MAJOR = CASES_ROOT / "package_major_bump_insufficient_evidence"
ORDERS = CASES_ROOT / "response_schema_change"
MULTI = CASES_ROOT / "multi_repo_dependency_usage"


def test_changes_diff_text_and_json(capsys: pytest.CaptureFixture[str], tmp_path: object) -> None:
    argv = ["changes", "diff", str(GITHUB / "old.yaml"), str(GITHUB / "new.yaml"), "--hints", str(GITHUB / "hints.yaml")]
    assert main(argv) == 0
    text = capsys.readouterr().out
    assert "risk: BREAKING" in text and "moved to GET /repos/{owner}/{repo}/stats/summary" in text
    assert main([*argv, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["classification"]["risk"] == "breaking"
    (item,) = [i for i in payload["diff"] if i["compatibility"] == "breaking"]
    assert item["kind"] == "endpoint_path_changed" and item["replacement"] == "GET /repos/{owner}/{repo}/stats/summary"


def test_changes_analyze_contract_pair_text(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["changes", "analyze", "--old", str(DEMO / "old.yaml"), "--new", str(DEMO / "new.yaml"),
            "--hints", str(DEMO / "hints.yaml"), "--repo", f"demo={DEMO / 'repo'}"]
    assert main(argv) == 0
    text = capsys.readouterr().out
    assert "Repository demo: AFFECTED" in text
    assert "app/ai/chat.py::generate_reply" in text and "workers/summary.py::summarize" in text
    assert "Blast radius: 2 direct, 3 transitive, 0 potential, 2 related test file(s)" in text
    assert "app/ai/embed.py::embed" not in text.split("Unaffected usages ignored")[0]


def test_changes_analyze_version_change_uses_the_repository_version(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["changes", "analyze", "--package", "acme-http", "--ecosystem", "pypi", "--release",
            str(MAJOR / "release.json"), "--repo", str(MAJOR / "repo"), "--json"]
    assert main(argv) == 0
    payload = json.loads(capsys.readouterr().out)
    event = payload["event"]
    assert (event["old"]["version"], event["new"]["version"]) == ("2.4.1", "3.0.0")
    assert event["source"] == "github_release" and event["release"]["tag"] == "v3.0.0"
    assert event["release"]["notes_sha256"] and "body" not in json.dumps(event["release"])
    assert event["classification"]["risk"] == "review_required"
    assert payload["impact"]["uncertain"] == ["repo"]


def test_changes_analyze_multiple_repositories(capsys: pytest.CaptureFixture[str]) -> None:
    repos = [f"{name}={MULTI / 'org' / name}" for name in ("checkout-service", "crm-service", "docs-site",
                                                             "billing-worker")]
    argv = ["changes", "analyze", "--old", str(MULTI / "old.yaml"), "--new", str(MULTI / "new.yaml"), "--hints",
            str(MULTI / "hints.yaml"), "--json"]
    for repo in repos:
        argv += ["--repo", repo]
    assert main(argv) == 0
    impact = json.loads(capsys.readouterr().out)["impact"]
    assert impact["affected"] == ["checkout-service"]
    assert impact["uncertain"] == ["billing-worker"]
    assert sorted(impact["unaffected"]) == ["crm-service", "docs-site"]


@pytest.mark.parametrize(
    "argv",
    [
        ["changes", "analyze", "--old", str(DEMO / "old.yaml"), "--repo", str(DEMO / "repo")],
        ["changes", "analyze", "--old", str(DEMO / "old.yaml"), "--new", str(GITHUB / "new.yaml"), "--repo",
         str(DEMO / "repo")],
        ["changes", "analyze", "--to-version", "2.0.0", "--repo", str(DEMO / "repo")],
        ["changes", "analyze", "--old", str(DEMO / "old.yaml"), "--new", str(DEMO / "new.yaml")],
        ["changes", "diff", str(DEMO / "repo" / "requirements.txt"), str(DEMO / "new.yaml")],
    ],
)
def test_bad_inputs_exit_2_without_a_traceback(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "Traceback" not in err


# -- Postgres ----------------------------------------------------------------------------


async def _require_postgres() -> None:
    engine = await postgres_engine_or_skip("external_change_events")
    await engine.dispose()


async def _cleanup(full_names: list[str], fingerprints: list[str]) -> None:
    engine = await postgres_engine_or_skip("external_change_events")
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            for name in full_names:
                row = (await session.execute(select(RepositoryModel).where(RepositoryModel.full_name == name))
                       ).scalar_one_or_none()
                if row is not None:
                    await session.delete(row)
            for fingerprint in fingerprints:
                event = (await session.execute(select(ExternalChangeEventModel).where(
                    ExternalChangeEventModel.fingerprint == fingerprint))).scalar_one_or_none()
                if event is not None:
                    await session.delete(event)
            await session.commit()
    finally:
        await engine.dispose()


def test_persist_is_idempotent(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_require_postgres())
    monkeypatch.setenv("DATABASE_URL", POSTGRES_TEST_URL)
    get_settings.cache_clear()
    name = f"test/m6-cli-{uuid.uuid4().hex[:8]}"
    # A unique provider identity keeps this run's event fingerprint private
    # to this test in the shared database.
    argv = ["changes", "analyze", "--old", str(DEMO / "old.yaml"), "--new", str(DEMO / "new.yaml"),
            "--hints", str(DEMO / "hints.yaml"), "--provider", f"acme-{uuid.uuid4().hex[:6]}",
            "--repo", f"{name}={DEMO / 'repo'}", "--persist", "--json"]
    fingerprint = ""
    try:
        assert main(argv) == 0
        first = json.loads(capsys.readouterr().out)
        fingerprint = first["event"]["fingerprint"]
        assert first["persisted"]["event_created"] is True
        assert first["persisted"]["impacts_created"] == {name: 1}
        assert main(argv) == 0
        second = json.loads(capsys.readouterr().out)
        assert second["event"]["fingerprint"] == fingerprint
        assert second["persisted"]["event_created"] is False
        assert second["persisted"]["impacts_created"] == {name: 0}
    finally:
        asyncio.run(_cleanup([name], [fingerprint] if fingerprint else []))
        get_settings.cache_clear()


def test_registry_snapshot_diff_and_registry_wide_impact(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(_require_postgres())
    monkeypatch.setenv("DATABASE_URL", POSTGRES_TEST_URL)
    get_settings.cache_clear()
    name = f"test/m6-orders-{uuid.uuid4().hex[:8]}"
    fingerprint = ""
    try:
        assert main(["dependencies", "discover", str(ORDERS / "repo"), "--persist", "--full-name", name]) == 0
        capsys.readouterr()
        argv = ["changes", "analyze", "--from-registry", "--full-name", name,
                "--dependency", "openapi:contracts/orders.openapi.yaml", "--new", str(ORDERS / "new.yaml"),
                "--registry", "--json"]
        assert main(argv) == 0
        payload = json.loads(capsys.readouterr().out)
        fingerprint = payload["event"]["fingerprint"]
        assert payload["event"]["source"] == "registry_snapshot"
        assert payload["event"]["classification"]["risk"] == "breaking"
        mine = [r for r in payload["impact"]["repositories"] if r["repository"] == name]
        assert mine and mine[0]["status"] == "affected"
        assert mine[0]["consumers"][0]["affected"][0]["symbol"] == "order_total"
    finally:
        asyncio.run(_cleanup([name], [fingerprint] if fingerprint else []))
        get_settings.cache_clear()
