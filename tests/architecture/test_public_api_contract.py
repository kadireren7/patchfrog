"""Public Engine API v0 contract (ADR-017, docs/architecture/PUBLIC_API.md).

Verifies the documented, stable engine interfaces in
``tests/architecture/public_api.py``. Deliberately independent of any
private consumer: the exact import surface of the hosted product is
verified by that product's own CI against a pinned engine revision
(PUBLIC_API.md section 4), never recorded in this public repository.

The imports run in **one** fresh interpreter with dummy server settings
(no database connection, no Redis, no GitHub, no provider credentials).
Network sockets and process spawning are *disabled and recorded*, not
mocked away: a public module that tries either at import time fails the
test, because that is an architectural problem to fix, not to hide.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.architecture.public_api import DEPRECATED_ALIASES, PUBLIC_API_V0, PUBLIC_API_VERSION
from tests.architecture.rules import CORE_ROOTS

_PROBE = r"""
import importlib, json, socket, subprocess, sys, threading, time

attempts = []
def _blocked(what):
    def guard(*args, **kwargs):
        attempts.append(f"{what}{args[1:2] if what == 'socket.connect' else args[:1]}")
        raise OSError(f"{what} disabled by the public API contract test")
    return guard
socket.socket.connect = _blocked("socket.connect")
socket.socket.connect_ex = _blocked("socket.connect")
socket.create_connection = _blocked("socket.create_connection")
socket.getaddrinfo = _blocked("socket.getaddrinfo")
subprocess.Popen.__init__ = _blocked("subprocess.Popen")

entries = json.loads(sys.argv[1])
aliases = json.loads(sys.argv[2])
threads_before = threading.active_count()
failures = []
resolved = {}
from patchfrog.config.settings import get_settings
# Library modules first: none of them may build server Settings at import
# time. Only the apps.* server entry points (imported afterwards) do, by design.
entries.sort(key=lambda entry: entry[0].startswith("apps."))
settings_built_by_library = None
for module_name, name in entries:
    if settings_built_by_library is None and module_name.startswith("apps."):
        settings_built_by_library = get_settings.cache_info().currsize
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        failures.append({"module": module_name, "name": name, "error": repr(exc)[:300]})
        continue
    if not hasattr(module, name):
        failures.append({"module": module_name, "name": name, "error": "name not defined in module"})
        continue
    resolved[(module_name, name)] = getattr(module, name)
alias_mismatches = [
    f"{alias[0]}.{alias[1]} is not {target[0]}.{target[1]}"
    for alias, target in aliases
    if resolved.get(tuple(alias)) is not resolved.get(tuple(target)) or tuple(alias) not in resolved
]
print(json.dumps({
    "checked": len(entries),
    "failures": failures,
    "alias_mismatches": alias_mismatches,
    "side_effect_attempts": attempts,
    "settings_built_by_library": settings_built_by_library,
    "new_threads": threading.active_count() - threads_before,
}))
"""


def test_public_api_list_is_well_formed() -> None:
    keys = [(module, name) for module, name, _area, _tier in PUBLIC_API_V0]

    assert len(keys) == len(set(keys)), "duplicate public API entries"
    assert {tier for *_rest, tier in PUBLIC_API_V0} <= {"stable", "provisional", "deprecated"}
    deprecated = {(m, n) for m, n, _a, tier in PUBLIC_API_V0 if tier == "deprecated"}
    assert deprecated == set(DEPRECATED_ALIASES), "every deprecated entry needs exactly one canonical target"
    for target in DEPRECATED_ALIASES.values():
        assert target in keys, f"canonical target {target} must itself be public"


def test_core_entries_are_covered_by_r1_core_roots() -> None:
    # Ties the public promise to the boundary rules: a "core" interface must
    # live under an approved core root whose closure R1 keeps pure.
    for module, name, area, _tier in PUBLIC_API_V0:
        if area != "core":
            continue
        assert any(module == root or module.startswith(root + ".") for root in CORE_ROOTS), (
            f"{module}.{name} is declared core but {module} is not under an R1 core root"
        )


def test_public_api_imports_without_side_effects(tmp_path: Path, test_private_key: str) -> None:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
        # Dummy server settings: syntactically valid, never contacted.
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "REDIS_URL": "redis://127.0.0.1:9/0",
        "GITHUB_APP_ID": "1",
        "GITHUB_PRIVATE_KEY": test_private_key,
        "GITHUB_WEBHOOK_SECRET": "contract-test-not-a-secret",
        "VERIFIER_STAGING_ROOT": str(tmp_path / "staging"),
    }
    entries = json.dumps([[module, name] for module, name, _area, _tier in PUBLIC_API_V0])
    aliases = json.dumps([[list(alias), list(target)] for alias, target in DEPRECATED_ALIASES.items()])

    result = subprocess.run(
        [sys.executable, "-c", _PROBE, entries, aliases],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert result.returncode == 0, result.stderr[-3000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["checked"] == len(PUBLIC_API_V0)
    assert report["failures"] == [], json.dumps(report["failures"], indent=1)
    assert report["alias_mismatches"] == [], report["alias_mismatches"]
    assert report["settings_built_by_library"] == 0, "a public library module built server Settings at import time"
    assert report["side_effect_attempts"] == [], (
        f"public engine modules attempted network/process activity at import time: {report['side_effect_attempts']}"
    )


def test_public_api_document_matches_the_contract() -> None:
    doc = (Path(__file__).resolve().parents[2] / "docs" / "architecture" / "PUBLIC_API.md").read_text()
    rows = set()
    for line in doc.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 4 and cells[0].startswith("`") and cells[1].startswith("`"):
            rows.add((cells[0].strip("`"), cells[1].strip("`"), cells[2], cells[3]))

    assert rows == set(PUBLIC_API_V0), "docs/architecture/PUBLIC_API.md section 3 is out of sync with public_api.py"
    assert f"**{PUBLIC_API_VERSION}**" in doc
