"""Application discovery (Milestone 2.3): engine rules, limits, identities,
serialization, and the JobRunner/store integration. The fixture corpus is
in test_discovery_corpus.py; this file covers behaviour the corpus can't
isolate (budgets, cancellation, revision binding, safety)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Mapping
from pathlib import Path

import pytest

from patchfrog.app.discovery import (
    DISCOVERY_ARTIFACT,
    DISCOVERY_RULESET_VERSION,
    DiscoveryResult,
    discovery_spec,
    load_discovery,
    run_discovery,
)
from patchfrog.app.jobs import CancellationToken, JobBudget, JobRunner
from patchfrog.app.revision import (
    RevisionEntry,
    RevisionEntryChanged,
    capture_revision,
    read_revision_file,
)
from patchfrog.app.store import LocalRunStore, RunState
from patchfrog.scalability.project import ApplicationModel, DiscoveryLimits, discover_application
from patchfrog.scalability.project.domain import (
    LimitationCode,
    UnresolvedKind,
    VersionStatus,
)
from patchfrog.scalability.project.frameworks import detect_frameworks

ENGINE = "0.1.0-beta"


class DictReader:
    def __init__(self, files: Mapping[str, str | bytes]) -> None:
        self.files = {k: v.encode() if isinstance(v, str) else v for k, v in files.items()}
        self.reads: list[str] = []
        self._skipped: dict[str, str] = {}

    @property
    def skipped(self) -> Mapping[str, str]:
        return self._skipped

    def read(self, path: str) -> bytes | None:
        self.reads.append(path)
        return self.files.get(path)


def discover(files: Mapping[str, str | bytes], limits: DiscoveryLimits | None = None) -> ApplicationModel:
    reader = DictReader(files)
    return discover_application(revision_id="r" * 64, paths=sorted(files), reader=reader, limits=limits or DiscoveryLimits())


def write_tree(root: Path, files: Mapping[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


APP = """
from fastapi import APIRouter, FastAPI
app = FastAPI()
router = APIRouter(prefix="/r")
@router.get("/a")
def a(): ...
app.include_router(router, prefix="/api")
"""


# --- framework detection ------------------------------------------------------------------


def test_version_rules_never_invent_an_installed_version() -> None:
    declared = detect_frameworks({"requirements.txt": "fastapi==0.110.0\n"}, {}, {})
    assert declared[0].version_status is VersionStatus.DECLARED_UNRESOLVED and declared[0].resolved_version is None
    assert declared[0].declarations[0].spec == "==0.110.0"

    locked = detect_frameworks(
        {"requirements.txt": "fastapi>=0.100\n"},
        {"poetry.lock": '[[package]]\nname = "fastapi"\nversion = "0.111.0"\n', "uv.lock": '[[package]]\nname = "FastAPI"\nversion = "0.111.0"\n'},
        {},
    )
    assert locked[0].version_status is VersionStatus.RESOLVED and locked[0].resolved_version == "0.111.0"
    assert len(locked[0].resolutions) == 2

    conflict = detect_frameworks(
        {}, {"poetry.lock": '[[package]]\nname = "fastapi"\nversion = "0.1"\n', "uv.lock": '[[package]]\nname = "fastapi"\nversion = "0.2"\n'}, {}
    )
    assert conflict[0].version_status is VersionStatus.CONFLICTING and conflict[0].resolved_version is None

    imported = detect_frameworks({}, {}, {"fastapi": 3})
    assert imported[0].version_status is VersionStatus.IMPORT_ONLY and imported[0].importing_files == 3
    assert detect_frameworks({}, {}, {}) == ()


def test_malformed_manifests_and_url_specs_are_safe() -> None:
    evidence = detect_frameworks(
        {
            "pyproject.toml": "[project\nbroken",
            "requirements.txt": "fastapi @ git+https://user:secret@example.invalid/fastapi.git\n",
        },
        {"poetry.lock": "not toml ["},
        {},
    )
    (fastapi,) = evidence
    assert fastapi.declarations[0].spec is not None and "secret" not in fastapi.declarations[0].spec
    assert fastapi.version_status is VersionStatus.DECLARED_UNRESOLVED


def test_only_python_manifests_are_read_and_setup_py_is_never_interpreted() -> None:
    reader = DictReader(
        {
            "package.json": '{"dependencies": {"fastapi": "1.0.0"}}',
            "setup.py": "raise SystemExit('never')\nsetup(install_requires=['fastapi==9'])\n",
            "requirements-dev.in": "fastapi\n",
        }
    )
    model = discover_application(revision_id="r" * 64, paths=sorted(reader.files), reader=reader, limits=DiscoveryLimits())
    (fastapi,) = model.frameworks
    assert [d.manifest_path for d in fastapi.declarations] == ["requirements-dev.in"]
    assert "package.json" not in reader.reads


# --- route semantics not covered by the corpus -------------------------------------------------


def test_identity_does_not_depend_on_line_numbers_and_serialization_is_canonical() -> None:
    first = discover({"main.py": APP})
    shifted = discover({"main.py": "\n\n# moved\n" + APP})
    assert [r.route_id for r in first.routes] == [r.route_id for r in shifted.routes]
    assert first.routes[0].registration.line != shifted.routes[0].registration.line
    assert first.to_json_bytes() == discover({"main.py": APP}).to_json_bytes()
    assert ApplicationModel.from_dict(json.loads(first.to_json_bytes())) == first


def test_newer_schema_is_rejected() -> None:
    data = json.loads(discover({"main.py": APP}).to_json_bytes())
    data["schema_version"] = 99
    with pytest.raises(ValueError):
        ApplicationModel.from_dict(data)


def test_include_cycle_is_reported_not_looped() -> None:
    source = """
from fastapi import APIRouter, FastAPI
app = FastAPI()
a = APIRouter(prefix="/a")
b = APIRouter(prefix="/b")
@b.get("/x")
def x(): ...
a.include_router(b)
b.include_router(a)
app.include_router(a)
"""
    model = discover({"main.py": source})
    assert [(r.path, r.include_chain) for r in model.routes] == [
        ("/a/b/x", ("app:main:app", "router:main:a", "router:main:b"))
    ]
    assert [x.code for x in model.limitations] == [LimitationCode.INCLUDE_CYCLE]
    assert not model.coverage.complete


def test_router_included_twice_gives_two_routes_with_distinct_ids() -> None:
    source = APP + 'app.include_router(router, prefix="/v2")\n'
    model = discover({"main.py": source})
    assert sorted(r.path for r in model.routes) == ["/api/r/a", "/v2/r/a"]
    assert len({r.route_id for r in model.routes}) == 2


def test_star_import_and_namespace_package_resolution() -> None:
    model = discover(
        {
            "pkg/routes/users.py": "from fastapi import APIRouter\nrouter = APIRouter()\n@router.get('/u')\nasync def u(): ...\n",
            "pkg/routes/all.py": "from .users import *\n",
            "pkg/main.py": "from fastapi import FastAPI\nfrom pkg.routes.all import router\napp = FastAPI()\napp.include_router(router)\n",
        }
    )
    assert [(r.app_id, r.path) for r in model.routes] == [("app:pkg.main:app", "/u")]
    assert model.coverage.complete


def test_syntax_bomb_and_undecodable_source_become_parse_errors() -> None:
    bomb = "x = " + "(" * 5000 + ")" * 5000 + "\nimport fastapi\n"
    model = discover({"bomb.py": bomb, "nul.py": b"import fastapi\x00\n", "main.py": APP})
    codes = sorted((x.code.value, x.location.path if x.location else "") for x in model.limitations)
    assert codes == [("parse_error", "bomb.py"), ("parse_error", "nul.py")]
    assert len(model.routes) == 1 and not model.coverage.complete


def test_files_without_registrations_are_not_parsed_unless_referenced() -> None:
    reader = DictReader(
        {
            "main.py": APP,
            "util.py": "def helper():\n    return {}.get('x')\n",
            "consts.py": "PREFIX = '/c'\n",
            "c.py": "from fastapi import APIRouter\nfrom consts import PREFIX\nr = APIRouter(prefix=PREFIX)\n@r.get('/x')\ndef x(): ...\n",
        }
    )
    model = discover_application(revision_id="r" * 64, paths=sorted(reader.files), reader=reader, limits=DiscoveryLimits())
    assert model.coverage.python_files == 4 and model.coverage.scanned_files == 4
    assert model.coverage.parsed_files == 3  # util.py has no registration token and isn't referenced
    assert ("router:c:r" in {r.router_id for r in model.routers}) and any(r.path == "/c/x" for r in model.routes)


# --- limits -------------------------------------------------------------------------------------------


def test_python_file_limit_is_explicit() -> None:
    files = {f"m{i:02}.py": "x = 1\n" for i in range(5)}
    model = discover(files, DiscoveryLimits(max_python_files=3))
    assert model.coverage.python_files == 3 and model.coverage.limit_reached
    assert any("max_python_files: 2 of 5" in x.detail for x in model.limitations)


def test_include_depth_route_count_and_unresolved_count_limits() -> None:
    chain = ["from fastapi import APIRouter, FastAPI", "app = FastAPI()"]
    chain += [f"r{i} = APIRouter(prefix='/{i}')" for i in range(6)]
    chain += ["@r5.get('/end')", "def end(): ...", "app.include_router(r0)"]
    chain += [f"r{i}.include_router(r{i + 1})" for i in range(5)]
    deep = discover({"main.py": "\n".join(chain) + "\n"}, DiscoveryLimits(max_include_depth=3))
    assert deep.routes == () and deep.coverage.limit_reached

    many = "from fastapi import FastAPI\napp = FastAPI()\n" + "".join(f"@app.get('/p{i}')\ndef h{i}(): ...\n" for i in range(20))
    capped = discover({"main.py": many}, DiscoveryLimits(max_routes=5))
    assert len(capped.routes) == 5 and capped.coverage.limit_reached

    noisy = "from fastapi import FastAPI\napp = FastAPI()\n" + "".join(f"app.include_router(x{i})\n" for i in range(20))
    trimmed = discover({"main.py": noisy}, DiscoveryLimits(max_unresolved=4))
    assert len(trimmed.unresolved) == 4
    assert any("16 further unresolved" in x.detail for x in trimmed.limitations)


def test_resolution_depth_limit_on_alias_chains() -> None:
    lines = ["from fastapi import FastAPI", "a0 = FastAPI()"] + [f"a{i} = a{i - 1}" for i in range(1, 40)]
    lines += ["@a39.get('/deep')", "def deep(): ..."]
    model = discover({"main.py": "\n".join(lines) + "\n"}, DiscoveryLimits(max_resolution_depth=10))
    assert model.routes == () and model.coverage.limit_reached
    assert [u.kind for u in model.unresolved] == [UnresolvedKind.ROUTER_RECEIVER]


def test_scope_depth_limit() -> None:
    body, indent = [], ""
    for i in range(6):
        body.append(f"{indent}def f{i}():")
        indent += "    "
    body.append(f"{indent}pass")
    model = discover({"main.py": "import fastapi\n" + "\n".join(body) + "\n"}, DiscoveryLimits(max_scope_depth=3))
    assert model.coverage.limit_reached
    assert any("max_scope_depth" in x.detail for x in model.limitations)


def test_invalid_limits_are_rejected() -> None:
    with pytest.raises(ValueError):
        DiscoveryLimits(max_routes=0)


# --- JobRunner integration -----------------------------------------------------------------------------


def test_discovery_runs_as_an_idempotent_job_bound_to_the_revision(tmp_path: Path) -> None:
    repo = write_tree(tmp_path / "repo", {"main.py": APP, "requirements.txt": "fastapi\n"})
    store = LocalRunStore(tmp_path / "store")
    first = run_discovery(store, repo, engine_version=ENGINE)
    assert first.job.state is RunState.SUCCEEDED and first.model is not None
    assert first.model.revision_id == first.job.revision.revision_id == first.job.record.revision_id
    assert first.job.record.kind == "discovery"
    assert first.job.record.metadata["ruleset_version"] == DISCOVERY_RULESET_VERSION

    again = run_discovery(store, repo, engine_version=ENGINE)
    assert again.job.reused and again.job.record.run_id == first.job.record.run_id
    assert again.model == first.model

    other_limits = run_discovery(store, repo, engine_version=ENGINE, limits=DiscoveryLimits(max_routes=7))
    assert not other_limits.job.reused  # limits shape the result, so they are part of the key

    (repo / "main.py").write_text(APP + "@app.get('/new')\ndef new(): ...\n")
    changed = run_discovery(store, repo, engine_version=ENGINE)
    assert not changed.job.reused and changed.model is not None
    assert changed.model.revision_id != first.model.revision_id and len(changed.model.routes) == 2


def test_artifact_is_immutable_and_reproducible_across_locations(tmp_path: Path) -> None:
    files = {"app/main.py": APP, "app/__init__.py": ""}
    a = run_discovery(LocalRunStore(tmp_path / "s1"), write_tree(tmp_path / "one", files), engine_version=ENGINE)
    b = run_discovery(LocalRunStore(tmp_path / "s2"), write_tree(tmp_path / "elsewhere" / "two", files), engine_version=ENGINE)
    ref_a, ref_b = a.job.artifacts[DISCOVERY_ARTIFACT], b.job.artifacts[DISCOVERY_ARTIFACT]
    assert ref_a == ref_b  # same bytes, independent of the absolute root
    object_path = tmp_path / "s1" / "objects" / ref_a.sha256[:2] / ref_a.sha256
    assert not os.access(object_path, os.W_OK)


def test_cancellation_discards_results(tmp_path: Path) -> None:
    repo = write_tree(tmp_path / "repo", {f"m{i}.py": APP.replace("/a", f"/a{i}") for i in range(30)})
    store = LocalRunStore(tmp_path / "store")
    token = CancellationToken()
    calls = 0
    original = JobRunner.run

    def run_and_cancel(self: JobRunner, spec, job, *, token=None):  # type: ignore[no-untyped-def]
        def wrapped(ctx):  # type: ignore[no-untyped-def]
            nonlocal calls
            original_read = ctx.read_file

            def read(path: str, *, max_bytes: int) -> bytes:
                nonlocal calls
                calls += 1
                if calls == 5:
                    assert token is not None
                    token.cancel()
                data: bytes = original_read(path, max_bytes=max_bytes)
                return data

            ctx.read_file = read
            job(ctx)

        return original(self, spec, wrapped, token=token)

    JobRunner.run = run_and_cancel  # type: ignore[method-assign]
    try:
        result = run_discovery(store, repo, engine_version=ENGINE, token=token)
    finally:
        JobRunner.run = original  # type: ignore[method-assign]
    assert result.job.state is RunState.CANCELLED and result.model is None
    assert DISCOVERY_ARTIFACT not in result.job.record.artifacts
    assert calls == 5  # stopped at the next checkpoint, not after all 30 files


def test_wall_time_budget_gives_partial_without_a_result(tmp_path: Path) -> None:
    repo = write_tree(tmp_path / "repo", {"main.py": APP})
    ticks = iter(range(0, 10_000, 50))
    runner = JobRunner(LocalRunStore(tmp_path / "store"), clock=lambda: float(next(ticks)))
    result = run_discovery(runner._store, repo, engine_version=ENGINE, budget=JobBudget(max_seconds=120), runner=runner)
    assert result.job.state is RunState.PARTIAL and result.model is None
    assert result.job.record.reason == "budget_exhausted: max_seconds"


def test_byte_budgets_skip_files_and_make_the_run_partial(tmp_path: Path) -> None:
    repo = write_tree(
        tmp_path / "repo",
        {"main.py": APP, "big.py": "import fastapi\n" + "#" * 5000 + "\n", "requirements.txt": "fastapi\n"},
    )
    result = run_discovery(LocalRunStore(tmp_path / "store"), repo, engine_version=ENGINE, limits=DiscoveryLimits(max_file_bytes=2000))
    assert result.job.state is RunState.PARTIAL and result.model is not None
    assert ("big.py", "max_file_bytes") in {(x.location.path, x.detail) for x in result.model.limitations if x.location}
    assert "file skipped (max_file_bytes): big.py" in result.job.report.partial_reasons
    assert len(result.model.routes) == 1

    tight = run_discovery(LocalRunStore(tmp_path / "store2"), repo, engine_version=ENGINE, limits=DiscoveryLimits(max_total_bytes=300))
    assert tight.job.state is RunState.PARTIAL and tight.model is not None
    assert any(x.detail == "max_total_bytes" for x in tight.model.limitations)


def test_file_changed_after_capture_is_never_analysed(tmp_path: Path) -> None:
    repo = write_tree(tmp_path / "repo", {"main.py": APP})
    store = LocalRunStore(tmp_path / "store")

    def capture_then_modify(*args, **kwargs):  # type: ignore[no-untyped-def]
        revision = capture_revision(*args, **kwargs)
        (repo / "main.py").write_text(APP.replace("/a", "/injected"))
        return revision

    result = run_discovery(store, repo, engine_version=ENGINE, runner=JobRunner(store, capture=capture_then_modify))
    assert result.job.state is RunState.PARTIAL and result.model is not None
    assert result.model.routes == ()
    assert any(x.detail == "changed_since_capture" for x in result.model.limitations)


def test_load_discovery_rejects_foreign_revision(tmp_path: Path) -> None:
    repo = write_tree(tmp_path / "repo", {"main.py": APP})
    store = LocalRunStore(tmp_path / "store")
    result = run_discovery(store, repo, engine_version=ENGINE)
    from dataclasses import replace

    with pytest.raises(ValueError):
        load_discovery(store, replace(result.job.record, revision_id="f" * 64))


def test_discovery_spec_is_deterministic(tmp_path: Path) -> None:
    a = discovery_spec(tmp_path, engine_version=ENGINE)
    b = discovery_spec(tmp_path, engine_version=ENGINE)
    assert a == b and a.ruleset_version == DISCOVERY_RULESET_VERSION


# --- safety ----------------------------------------------------------------------------------------------


def test_analysed_code_is_never_imported_or_executed(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    payload = f"import pathlib\npathlib.Path({str(marker)!r}).write_text('x')\nraise SystemExit(3)\n"
    repo = write_tree(
        tmp_path / "repo",
        {
            "evil_mod.py": payload + "from fastapi import FastAPI\napp = FastAPI()\n",
            "main.py": "from evil_mod import app\n@app.get('/x')\ndef x(): ...\n",
            "setup.py": payload,
            "conftest.py": payload,
        },
    )
    before = set(sys.modules)
    result = run_discovery(LocalRunStore(tmp_path / "store"), repo, engine_version=ENGINE)
    assert not marker.exists()
    assert not {m for m in set(sys.modules) - before if m in {"evil_mod", "main", "setup", "conftest"}}
    assert result.model is not None and [r.path for r in result.model.routes] == ["/x"]


def test_symlinks_are_never_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret_routes.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n@app.get('/leak')\ndef leak(): ...\n")
    repo = write_tree(tmp_path / "repo", {"main.py": APP})
    (repo / "linked.py").symlink_to(outside / "secret_routes.py")
    (repo / "linkdir").symlink_to(outside, target_is_directory=True)
    result = run_discovery(LocalRunStore(tmp_path / "store"), repo, engine_version=ENGINE)
    assert result.model is not None
    assert [r.path for r in result.model.routes] == ["/api/r/a"]
    assert all("secret" not in r.location.path for r in result.model.routers)


def test_read_revision_file_refuses_mismatches_and_symlink_swaps(tmp_path: Path) -> None:
    repo = write_tree(tmp_path / "repo", {"a.py": "x = 1\n"})
    revision = capture_revision(repo)
    (entry,) = revision.entries
    assert read_revision_file(repo, entry, max_bytes=100) == b"x = 1\n"
    with pytest.raises(RevisionEntryChanged):
        read_revision_file(repo, entry, max_bytes=3)
    with pytest.raises(RevisionEntryChanged):
        read_revision_file(repo, RevisionEntry("a.py", entry.size, "0" * 64), max_bytes=100)
    (tmp_path / "other.py").write_text("x = 1\n")
    (repo / "a.py").unlink()
    (repo / "a.py").symlink_to(tmp_path / "other.py")  # identical bytes, but outside the root
    with pytest.raises(RevisionEntryChanged):
        read_revision_file(repo, entry, max_bytes=100)
    with pytest.raises(RevisionEntryChanged):
        read_revision_file(repo, RevisionEntry("missing.py", 1, "0" * 64), max_bytes=100)


def test_concurrent_identical_discoveries_analyse_once(tmp_path: Path) -> None:
    repo = write_tree(tmp_path / "repo", {"main.py": APP})
    store = LocalRunStore(tmp_path / "store")
    results: list[DiscoveryResult] = []

    def worker() -> None:
        results.append(run_discovery(store, repo, engine_version=ENGINE))

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(results) == 3 and len({r.job.record.run_id for r in results}) == 1
    assert sum(1 for r in results if not r.job.reused) == 1


def test_discovery_works_in_an_empty_environment_without_server_modules(tmp_path: Path) -> None:
    repo = write_tree(tmp_path / "repo", {"main.py": APP})
    code = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from patchfrog.app.discovery import run_discovery\n"
        "from patchfrog.app.store import LocalRunStore\n"
        f"r = run_discovery(LocalRunStore(Path({str(tmp_path / 'store')!r})), Path({str(repo)!r}), engine_version='0.1.0-beta')\n"
        "heavy = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'sqlalchemy', 'celery', 'redis', 'anthropic', 'openai', 'fastapi', 'starlette', 'asyncpg', 'apps', 'httpx'} "
        "or m.startswith(('google.genai', 'patchfrog.persistence', 'patchfrog.review', "
        "'patchfrog.config.settings', 'patchfrog.github')))\n"
        "print(json.dumps({'state': r.job.state.value, 'routes': len(r.model.routes), 'heavy': heavy}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"state": "succeeded", "routes": 1, "heavy": []}


def test_layered_300_file_fixture_is_discovered_exactly(tmp_path: Path) -> None:
    """The roadmap's synthetic layered app (routers, services, repositories),
    checked for correctness only; timing is recorded by
    scripts/bench_discovery.py, never asserted."""

    files: dict[str, str] = {"app/__init__.py": "", "app/api/__init__.py": "", "app/services/__init__.py": "", "app/repositories/__init__.py": ""}
    includes = []
    for i in range(100):
        files[f"app/repositories/repo_{i}.py"] = f"class Repo{i}:\n    def all(self):\n        return []\n"
        files[f"app/services/service_{i}.py"] = (
            f"from app.repositories.repo_{i} import Repo{i}\n\ndef get_service_{i}():\n    return Repo{i}()\n"
        )
        files[f"app/api/resource_{i}.py"] = (
            "from fastapi import APIRouter, Depends\n"
            f"from app.services.service_{i} import get_service_{i}\n"
            f"router = APIRouter(prefix='/r{i}')\n"
            f"@router.get('/')\nasync def list_{i}(svc=Depends(get_service_{i})):\n    return svc.all()\n"
            f"@router.post('/')\ndef create_{i}(svc=Depends(get_service_{i})):\n    return None\n"
        )
        includes.append(f"from app.api.resource_{i} import router as r{i}\napp.include_router(r{i}, prefix='/api')\n")
    files["app/main.py"] = "from fastapi import FastAPI\napp = FastAPI()\n" + "".join(includes)
    repo = write_tree(tmp_path / "repo", files)
    result = run_discovery(LocalRunStore(tmp_path / "store"), repo, engine_version=ENGINE)
    model = result.model
    assert model is not None and result.job.state is RunState.SUCCEEDED
    assert model.coverage.python_files == 305 and model.coverage.complete
    assert len(model.routes) == 200 and len(model.dependency_functions) == 100
    assert {r.path for r in model.routes} == {f"/api/r{i}/" for i in range(100)}
