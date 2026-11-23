"""Phase 1 (ADR-011): the CLI starts with no external services configured.

Every check runs in a **fresh interpreter with an empty environment**
(only ``PATH`` and a throwaway ``HOME``, cwd in a temp dir so no ``.env``
is picked up), because the regression being guarded against is an
import-time side effect: ``apps.worker.celery_app`` builds the server
``Settings`` at module load, which used to make ``python -m patchfrog.cli
--help`` exit 1 without a database/Redis/GitHub App environment. An
in-process test could not observe that -- the suite's own conftest
already populates the server environment.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_TIMEOUT_SECONDS = 180

#: Modules with import-time side effects that require the server
#: environment (``apps.worker.celery_app`` constructs server ``Settings``
#: at module load; the task modules import it). Merely importing the
#: ``celery``/``redis``/``sqlalchemy`` *libraries* is still the case today
#: (via e.g. ``patchfrog.executable_verification.dispatch``) and is
#: harmless for startup; slimming the CLI import graph is tracked
#: separately in ``docs/PROGRESS.md``.
_SERVER_ONLY_MODULES = ("apps.worker.celery_app", "apps.worker.tasks")


def _empty_env(home: Path) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home)}


def _run_python(code: str, *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd,
        env=_empty_env(cwd),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )


_HELP_PROBE = r"""
import argparse, contextlib, io, json, sys
import patchfrog.cli as cli
from patchfrog.config.settings import get_settings

captured = {}
original = argparse.ArgumentParser.parse_args
def capture(self, *args, **kwargs):
    captured.setdefault("parser", self)
    raise SystemExit(0)
argparse.ArgumentParser.parse_args = capture
try:
    cli.main([])
except SystemExit:
    pass
argparse.ArgumentParser.parse_args = original

def walk(parser, prefix):
    paths = [prefix]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                paths.extend(walk(sub, [*prefix, name]))
    return paths

failures = []
paths = walk(captured["parser"], [])
for path in paths:
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main([*path, "--help"])
        failures.append({"path": path, "error": "no SystemExit"})
    except SystemExit as exc:
        if exc.code not in (0, None):
            failures.append({"path": path, "error": f"exit {exc.code}"})
    except Exception as exc:
        failures.append({"path": path, "error": repr(exc)})

print(json.dumps({
    "paths": len(paths),
    "failures": failures,
    "loaded": sorted(m for m in __MODULES__ if m in sys.modules),
    "server_settings_constructed": get_settings.cache_info().currsize,
}))
"""


def test_every_cli_help_works_without_any_server_environment(tmp_path: Path) -> None:
    result = _run_python(_HELP_PROBE.replace("__MODULES__", repr(_SERVER_ONLY_MODULES)), cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["failures"] == []
    # Guards against the probe silently enumerating nothing.
    assert report["paths"] >= 50
    assert report["loaded"] == []
    assert report["server_settings_constructed"] == 0


def test_cli_module_entry_point_help_exits_zero_without_environment(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "patchfrog.cli", "--help"],
        cwd=tmp_path,
        env=_empty_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_db_free_command_runs_without_any_server_environment(tmp_path: Path) -> None:
    repository = tmp_path / "app"
    repository.mkdir()
    (repository / "requirements.txt").write_text("fastapi==0.110.0\nsqlalchemy>=2.0\n")

    result = subprocess.run(
        [sys.executable, "-m", "patchfrog.cli", "dependencies", "discover", "--json", str(repository)],
        cwd=tmp_path,
        env=_empty_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    inventory = json.loads(result.stdout)
    assert inventory["repository"] == "app"
    assert "fastapi" in result.stdout


def test_db_free_code_graph_analysis_runs_without_any_server_environment(tmp_path: Path) -> None:
    """More than ``--help``: a real parse -> call graph -> blast-radius
    pipeline (``changes analyze --repo`` without ``--persist``, built on
    the DB-free ``LocalCodeGraph``) runs end to end with no database,
    Redis, GitHub App, network, or AI credential."""

    case = Path(__file__).parent.parent / "fixtures" / "upstream_changes" / "openai_sdk_method_rename"
    result = subprocess.run(
        [
            sys.executable, "-m", "patchfrog.cli", "changes", "analyze",
            "--old", str(case / "old.yaml"), "--new", str(case / "new.yaml"),
            "--hints", str(case / "hints.yaml"), "--repo", str(case / "repo"), "--json",
        ],
        cwd=tmp_path,
        env=_empty_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert set(payload) == {"event", "impact"}
    assert "persisted" not in payload
    assert payload["impact"]["affected"] == ["repo"]
    assert "assistant/reply.py::answer" in json.dumps(payload["impact"]["repositories"])


def test_core_settings_import_pulls_in_no_feature_or_server_package(tmp_path: Path) -> None:
    code = (
        "import json, sys\n"
        "from patchfrog.config.core import get_core_settings\n"
        "settings = get_core_settings()\n"
        "heavy = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'sqlalchemy', 'celery', 'redis', 'anthropic', 'openai', 'google', 'fastapi'}"
        " or m.startswith('patchfrog.review') or m.startswith('patchfrog.persistence')"
        " or m == 'patchfrog.config.settings')\n"
        "print(json.dumps({'app_env': settings.app_env, 'heavy': heavy}))\n"
    )

    result = _run_python(code, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report == {"app_env": "development", "heavy": []}
