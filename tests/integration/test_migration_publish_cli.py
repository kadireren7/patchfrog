"""M9.10: ``patchfrog migrations publish --dry-run`` -- text and JSON
forms, and the CLI's own safety gates (real publication and multi-repo
fan-out are both rejected, never silently narrowed)."""

from __future__ import annotations

import json

import pytest

from patchfrog.cli import main
from tests.support.upstream_cases import CASES_ROOT

DEMO_VERIFIED = CASES_ROOT / "demo_verified"


def _argv(*, json_output: bool = False, dry_run: bool = True, repository: str | None = "acme-org/widgets") -> list[str]:
    argv = [
        "migrations", "publish", "--old", str(DEMO_VERIFIED / "old.yaml"), "--new", str(DEMO_VERIFIED / "new.yaml"),
        "--hints", str(DEMO_VERIFIED / "hints.yaml"), "--repo", f"demo={DEMO_VERIFIED / 'repo'}",
    ]
    if repository is not None:
        argv += ["--repository", repository]
    if dry_run:
        argv.append("--dry-run")
    if json_output:
        argv.append("--json")
    return argv


def test_publish_dry_run_never_touches_github_and_prints_a_plan(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_argv()) == 0
    text = capsys.readouterr().out
    assert "Eligibility:" in text
    assert "Proposed branch: patchfrog/migrate/acme-ai/" in text
    assert "PR title:" in text
    assert "PR body:" in text


def test_publish_dry_run_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_argv(json_output=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["eligibility"] in {"auto_open", "open_with_operator_approval", "plan_only", "not_eligible"}
    assert payload["linkage"]["repository"] == "acme-org/widgets"
    assert payload["branch_name"].startswith("patchfrog/migrate/acme-ai/")
    assert "identity_key" in payload["linkage"]


def test_publish_without_dry_run_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_argv(dry_run=False)) == 2
    err = capsys.readouterr().err
    assert "only --dry-run is supported" in err


def test_publish_without_repository_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_argv(repository=None)) == 2
    err = capsys.readouterr().err
    assert "--repository" in err


def test_publish_rejects_more_than_one_repo(capsys: pytest.CaptureFixture[str]) -> None:
    argv = [*_argv(), "--repo", f"second={DEMO_VERIFIED / 'repo'}"]
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert "exactly one --repo" in err
