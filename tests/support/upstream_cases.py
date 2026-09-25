"""Loader for the M6/M7 end-to-end fixture corpus
(``tests/fixtures/upstream_changes/<case>/case.yaml``).

A case names its change inputs (a contract pair + optional hints, or a
package version change with release metadata) and its consumer
repositories; everything else -- the diff, consumers, blast radius,
migration plan, patch -- is produced by the real engine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from patchfrog.dependencies.discovery import discover_dependencies
from patchfrog.dependencies.domain import Ecosystem
from patchfrog.upstream.domain import DependencyTarget, ExternalChangeEvent
from patchfrog.upstream.events import (
    build_contract_change,
    build_version_change,
    load_contract_file,
    release_from_metadata,
)
from patchfrog.upstream.hints import EMPTY_HINTS, ChangeHints, load_hints
from patchfrog.upstream.workspace import current_version

CASES_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "upstream_changes"


@dataclass(frozen=True)
class UpstreamCase:
    name: str
    directory: Path
    event: ExternalChangeEvent
    hints: ChangeHints
    repositories: dict[str, Path]
    expect: dict[str, Any]


def load_case(directory: Path) -> UpstreamCase:
    spec = yaml.safe_load((directory / "case.yaml").read_text())
    change = spec["change"]
    hints = load_hints((directory / change["hints"]).read_text()) if change.get("hints") else EMPTY_HINTS
    repositories = {name: directory / rel for name, rel in spec["repositories"].items()}
    if "old" in change:
        event = build_contract_change(
            load_contract_file(directory / change["old"]), load_contract_file(directory / change["new"]), hints=hints
        )
    else:
        target = DependencyTarget(ecosystem=Ecosystem(change["ecosystem"]), package_name=change["package"])
        release = release_from_metadata(json.loads((directory / change["to_version_from_release"]).read_text()))
        first_repo = next(iter(repositories.values()))
        from_version = current_version(discover_dependencies(first_repo), target)
        event = build_version_change(target, from_version, release.version, hints=hints, release=release)
    return UpstreamCase(
        name=directory.name, directory=directory, event=event, hints=hints, repositories=repositories,
        expect=spec["expect"],
    )


def all_case_dirs() -> list[Path]:
    return sorted(p for p in CASES_ROOT.iterdir() if (p / "case.yaml").is_file())
