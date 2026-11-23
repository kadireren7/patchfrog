"""End-to-end tests of ``patchfrog scale`` through the real entry points
(Milestone 2.5): ``python -m patchfrog scale ...`` (the fast path) and, once,
``python -m patchfrog.cli scale ...``. Every test runs the CLI in a
subprocess with a minimal environment (no server configuration) against a
temporary repository and a temporary store."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from patchfrog.app.discovery import DISCOVERY_ARTIFACT
from patchfrog.app.store import LocalRunStore, RunState
from patchfrog.scalability.project import ApplicationModel

ROOT = Path(__file__).resolve().parents[2]
APP = {
    "app/__init__.py": "",
    "app/db.py": (
        "from sqlalchemy import create_engine\nfrom sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker\n"
        "engine = create_engine('sqlite:///local.db')\nSessionLocal = sessionmaker(bind=engine)\n"
        "class Base(DeclarativeBase):\n    pass\n"
        "class Item(Base):\n    __tablename__ = 'items'\n    id: Mapped[int] = mapped_column(primary_key=True)\n"
        "def get_db():\n    db = SessionLocal()\n    try:\n        yield db\n    finally:\n        db.close()\n"
    ),
    "app/main.py": (
        "from fastapi import Depends, FastAPI\nfrom app.db import get_db\napp = FastAPI()\n"
        "@app.get('/items')\ndef items(db=Depends(get_db)):\n    return []\n"
    ),
    "requirements.txt": "fastapi>=0.100\nsqlalchemy>=2.0\n",
}
TIMEOUT = 120


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def env(tmp: Path, **extra: str) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp / "home"), **extra}


def scale(*args: str, tmp: Path, cwd: Path | None = None, module: str = "patchfrog", **extra_env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", module, "scale", *args],
        cwd=cwd or tmp,
        env=env(tmp, **extra_env),
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )


def as_json(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(result.stdout)
    return data


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return write_tree(tmp_path / "repo", APP)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return tmp_path / "store"


def test_analyze_text_and_json_then_reuse_and_show(repo: Path, store: Path, tmp_path: Path) -> None:
    text = scale("analyze", str(repo), "--store", str(store), tmp=tmp_path)
    assert text.returncode == 0, text.stderr
    assert "[succeeded]" in text.stdout and "Coverage   complete" in text.stdout
    assert "1 model(s)" in text.stdout and "1 route(s) reach a session" in text.stdout
    assert "no performance findings" in text.stdout

    again = scale("analyze", str(repo), "--store", str(store), "--json", tmp=tmp_path)
    assert again.returncode == 0, again.stderr
    payload = as_json(again)
    assert payload["format"] == "patchfrog.scale.run" and payload["format_version"] == 1
    assert payload["run"]["reused"] is True and payload["run"]["state"] == "succeeded"
    assert payload["complete"] is True and payload["run"]["run_id"] in text.stdout
    model = ApplicationModel.from_dict(payload["model"])
    assert [r.path for r in model.routes] == ["/items"] and [m.name for m in model.database.models] == ["Item"]

    shown = scale("show", payload["run"]["run_id"], "--store", str(store), "--json", tmp=tmp_path)
    assert shown.returncode == 0, shown.stderr
    assert as_json(shown)["model"] == payload["model"] and as_json(shown)["complete"] is True
    details = scale("show", payload["run"]["run_id"], "--store", str(store), "--details", tmp=tmp_path)
    assert details.returncode == 0 and "GET      /items" in details.stdout and "app.db:get_db" in details.stdout


def test_dirty_working_tree_changes_the_revision_and_is_reported(repo: Path, store: Path, tmp_path: Path) -> None:
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "init", "-q"], cwd=repo, check=True, env=env(tmp_path))
    subprocess.run([*git, "add", "-A"], cwd=repo, check=True, env=env(tmp_path))
    subprocess.run([*git, "commit", "-qm", "init"], cwd=repo, check=True, env=env(tmp_path))
    clean = as_json(scale("analyze", str(repo), "--store", str(store), "--json", tmp=tmp_path))
    assert clean["revision"]["kind"] == "git" and clean["revision"]["dirty"] is False

    (repo / "app" / "main.py").write_text(APP["app/main.py"] + "@app.get('/new')\ndef new():\n    return {}\n")
    dirty = scale("analyze", str(repo), "--store", str(store), "--json", tmp=tmp_path)
    data = as_json(dirty)
    assert data["revision"]["dirty"] is True and data["revision"]["changes"]["modified"] == 1
    assert data["run"]["reused"] is False and data["run"]["run_id"] != clean["run"]["run_id"]
    assert data["revision"]["revision_id"] != clean["revision"]["revision_id"]
    assert "uncommitted changes: 1 modified" in scale("analyze", str(repo), "--store", str(store), tmp=tmp_path).stdout


def test_partial_and_strict_exit_codes(repo: Path, store: Path, tmp_path: Path) -> None:
    partial = scale("analyze", str(repo), "--store", str(store), "--max-files", "1", tmp=tmp_path)
    assert partial.returncode == 3, partial.stderr
    assert "[partial]" in partial.stdout and "NOT COMPLETE" in partial.stdout and "Partial because" in partial.stdout
    partial_json = as_json(scale("analyze", str(repo), "--store", str(store), "--max-files", "1", "--json", tmp=tmp_path))
    assert partial_json["complete"] is False and partial_json["run"]["state"] == "partial"
    assert partial_json["run"]["reused"] is False  # PARTIAL runs are never reused

    (repo / "app" / "dynamic.py").write_text("from fastapi import FastAPI\napp2 = FastAPI()\nfor r in []:\n    app2.include_router(r)\n")
    lenient = scale("analyze", str(repo), "--store", str(store), tmp=tmp_path)
    assert lenient.returncode == 0 and "NOT COMPLETE: 1 unresolved reference(s)" in lenient.stdout
    strict = scale("analyze", str(repo), "--store", str(store), "--strict", tmp=tmp_path)
    assert strict.returncode == 3


def test_input_errors(tmp_path: Path, store: Path) -> None:
    (tmp_path / "file.txt").write_text("x")
    assert scale("analyze", str(tmp_path / "missing"), "--store", str(store), tmp=tmp_path).returncode == 2
    assert scale("analyze", str(tmp_path / "file.txt"), "--store", str(store), tmp=tmp_path).returncode == 2
    bad = scale("show", "../../etc", "--store", str(store), tmp=tmp_path)
    assert bad.returncode == 2 and "invalid run id" in bad.stderr
    missing = scale("show", "0" * 32, "--store", str(store), tmp=tmp_path)
    assert missing.returncode == 5 and "not found" in missing.stderr
    usage = scale("frobnicate", tmp=tmp_path)
    assert usage.returncode == 2


def test_corrupt_unsupported_and_foreign_runs_are_rejected(repo: Path, store: Path, tmp_path: Path) -> None:
    run_id = as_json(scale("analyze", str(repo), "--store", str(store), "--json", tmp=tmp_path))["run"]["run_id"]
    local = LocalRunStore(store)
    ref = local.get_run(run_id).artifacts[DISCOVERY_ARTIFACT]
    obj = store / "objects" / ref.sha256[:2] / ref.sha256
    obj.chmod(0o600)
    obj.write_bytes(obj.read_bytes().replace(b'"items"', b'"ITEMS"'))
    corrupt = scale("show", run_id, "--store", str(store), tmp=tmp_path)
    assert corrupt.returncode == 6 and "unreadable" in corrupt.stderr

    revision = local.get_run(run_id).revision_id
    for name, doc, kind in (
        ("future", {"schema_version": 99, "revision_id": revision}, "discovery"),
        ("not-a-model", {"schema_version": 2, "revision_id": revision, "routes": "nope"}, "discovery"),
        ("other-kind", {"schema_version": 2}, "verification"),
    ):
        handle = local.begin_run(idempotency_key=hashlib.sha256(name.encode()).hexdigest(), revision_id=revision, kind=kind)
        handle.finish(RunState.SUCCEEDED, artifacts={DISCOVERY_ARTIFACT: local.put_bytes(json.dumps(doc).encode())})
        shown = scale("show", handle.run_id, "--store", str(store), tmp=tmp_path)
        assert shown.returncode == 6, (name, shown.stderr)
        if name == "future":
            assert "not supported" in shown.stderr


def test_interrupted_and_failed_runs_report_no_result(repo: Path, store: Path, tmp_path: Path) -> None:
    local = LocalRunStore(store)
    handle = local.begin_run(idempotency_key="a" * 64, revision_id="b" * 64, kind="discovery")
    handle._release()  # the owner "dies" without finishing
    local.recover()
    shown = scale("show", handle.run_id, "--store", str(store), "--json", tmp=tmp_path)
    assert shown.returncode == 4
    data = as_json(shown)
    assert data["run"]["state"] == "interrupted" and data["model"] is None and data["complete"] is False
    text = scale("show", handle.run_id, "--store", str(store), tmp=tmp_path)
    assert "NOT COMPLETE" in text.stdout

    failed = local.begin_run(idempotency_key="c" * 64, revision_id="b" * 64, kind="discovery")
    failed.finish(RunState.FAILED, reason="internal_error: RuntimeError")
    assert scale("show", failed.run_id, "--store", str(store), tmp=tmp_path).returncode == 4


def test_no_server_configuration_and_nothing_from_the_repository_runs(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    payload = f"import pathlib\npathlib.Path({str(marker)!r}).write_text('x')\n"
    repo = write_tree(tmp_path / "repo", {**APP, "app/__init__.py": payload, "conftest.py": payload, "setup.py": payload})
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env(tmp_path))
    subprocess.run(["git", "config", "core.fsmonitor", str(hook)], cwd=repo, check=True, env=env(tmp_path))
    # A repository .env must not choose where PatchFrog writes.
    (repo / ".env").write_text(f"PATCHFROG_STORE_DIR={repo / 'evil-store'}\nDATABASE_URL=postgresql://x\n")
    probe = (
        "import json, runpy, sys\n"
        "sys.argv = ['patchfrog', 'scale', 'analyze', '.', '--json']\n"
        "try:\n    runpy.run_module('patchfrog', run_name='__main__')\nexcept SystemExit as exc:\n    code = exc.code\n"
        "heavy = sorted(m for m in sys.modules if m.split('.')[0] in {'sqlalchemy', 'fastapi', 'starlette', 'celery', 'redis', "
        "'httpx', 'anthropic', 'openai', 'asyncpg', 'apps', 'app', 'conftest', 'setup'} or m.startswith(('patchfrog.persistence', "
        "'patchfrog.review', 'patchfrog.config.settings', 'patchfrog.github', 'patchfrog.cli.')) or m == 'patchfrog.cli')\n"
        "sys.stderr.write(json.dumps({'code': code, 'heavy': heavy}) + '\\n')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=repo, env=env(tmp_path, XDG_CACHE_HOME=str(tmp_path / "cache")),
        capture_output=True, text=True, timeout=TIMEOUT, check=False,
    )
    report = json.loads(result.stderr.strip().splitlines()[-1])
    assert report == {"code": 0, "heavy": []}, result.stderr
    assert not marker.exists()
    assert not (repo / "evil-store").exists()
    assert (tmp_path / "cache" / "patchfrog" / "store" / "store.json").is_file()
    assert as_json(result)["model"]["database"]["models"][0]["name"] == "Item"


def test_artifacts_are_reproducible_across_stores_and_locations(tmp_path: Path) -> None:
    a = as_json(scale("analyze", str(write_tree(tmp_path / "one", APP)), "--store", str(tmp_path / "s1"), "--json", tmp=tmp_path))
    b = as_json(scale("analyze", str(write_tree(tmp_path / "x" / "two", APP)), "--store", str(tmp_path / "s2"), "--json", tmp=tmp_path))
    assert a["model"] == b["model"] and a["revision"]["revision_id"] == b["revision"]["revision_id"]
    ref_a = LocalRunStore(tmp_path / "s1").get_run(a["run"]["run_id"]).artifacts[DISCOVERY_ARTIFACT]
    ref_b = LocalRunStore(tmp_path / "s2").get_run(b["run"]["run_id"]).artifacts[DISCOVERY_ARTIFACT]
    assert ref_a == ref_b


def test_schema_command_prints_the_committed_schema(tmp_path: Path) -> None:
    result = scale("schema", tmp=tmp_path)
    assert result.returncode == 0
    assert result.stdout == (ROOT / "docs" / "schemas" / "application-model.v2.schema.json").read_text()


def test_the_full_cli_entry_point_dispatches_scale_too(repo: Path, store: Path, tmp_path: Path) -> None:
    result = scale("analyze", str(repo), "--store", str(store), "--json", tmp=tmp_path, module="patchfrog.cli")
    assert result.returncode == 0, result.stderr
    assert as_json(result)["complete"] is True
    assert scale("--help", tmp=tmp_path, module="patchfrog.cli").returncode == 0
