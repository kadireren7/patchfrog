"""Milestone 1.3 (MIGRATION_PLAN M4): ``patchfrog.codegraph`` is a neutral,
local-first package.

Acceptance criterion (Phase 1, ROADMAP): importing ``patchfrog.codegraph``
loads no ``sqlalchemy``, ``celery``, ``anthropic``, ``openai`` or
``google.genai``, checked through ``sys.modules`` in a **fresh interpreter
with an empty environment**, because the suite's own conftest fills in a
server environment that an in-process check could not rule out. Static
closure purity is separately enforced by ``tests/architecture`` (R1).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import patchfrog.codegraph as codegraph
import patchfrog.upstream.code_graph as legacy_code_graph
from patchfrog.codegraph import build_local_code_graph

_FORBIDDEN_TOP_LEVEL = ("sqlalchemy", "asyncpg", "alembic", "celery", "redis", "anthropic", "openai", "fastapi", "semgrep", "apps")
_FORBIDDEN_PREFIXES = (
    "google.genai",
    "patchfrog.persistence",
    "patchfrog.review",
    "patchfrog.config.settings",
    "patchfrog.github",
    "patchfrog.upstream",
)


def _write_repo(root: Path) -> Path:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "a.py").write_text("def helper():\n    return 1\n")
    (root / "pkg" / "b.py").write_text("from pkg.a import helper\n\n\ndef caller():\n    return helper()\n")
    return root


def test_codegraph_builds_without_any_service_environment_and_loads_no_server_or_llm_modules(tmp_path: Path) -> None:
    repo = _write_repo(tmp_path / "repo")
    code = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from patchfrog.codegraph import build_local_code_graph\n"
        f"graph = build_local_code_graph(Path({str(repo)!r}))\n"
        "callers = {f'{k[0]}::{k[1]}': sorted(f'{c[0]}::{c[1]}' for c in v) for k, v in graph.callers.items()}\n"
        f"top = {_FORBIDDEN_TOP_LEVEL!r}\n"
        f"prefixes = {_FORBIDDEN_PREFIXES!r}\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] in top or m.startswith(prefixes))\n"
        "print(json.dumps({'callers': callers, 'languages': sorted(graph.languages), 'loaded': loaded}))\n"
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
    report = json.loads(result.stdout)
    assert report["loaded"] == []
    assert report["languages"] == ["python"]
    assert report["callers"] == {"pkg/a.py::helper": ["pkg/b.py::caller"]}


def test_code_graph_output_is_reproducible_for_the_same_tree(tmp_path: Path) -> None:
    repo = _write_repo(tmp_path / "repo")

    first, second = build_local_code_graph(repo), build_local_code_graph(repo)

    assert first.edges == second.edges
    assert first.graph_files == second.graph_files
    assert {k: sorted(v) for k, v in first.callers.items()} == {k: sorted(v) for k, v in second.callers.items()}


def test_legacy_upstream_path_reexports_the_same_objects() -> None:
    for name in codegraph.__all__:
        assert getattr(legacy_code_graph, name) is getattr(codegraph, name), name
    assert set(legacy_code_graph.__all__) == set(codegraph.__all__)
