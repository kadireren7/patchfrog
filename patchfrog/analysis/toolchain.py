"""Effective analysis toolchain identity.

Distinct from :class:`~patchfrog.analysis.config.AnalysisConfig`, which
captures configuration *intent* (what a run was asked to do):
:class:`ToolchainSnapshot` captures what actually ran -- the analyzers
selected for this run and what got discovered about them right now (name
+ version), a content hash of any ruleset PatchFrog bundles (so editing
``patchfrog-rules.yml`` invalidates prior canonical runs even though
nothing about the *config* changed), and PatchFrog's own analysis engine
version (bumped when the normalization/dedup/fingerprint algorithm itself
changes materially).

Two runs can have identical configuration intent yet a different
effective toolchain -- e.g. the worker image upgrades ruff between two
otherwise-identical requests -- and canonical-run reuse must treat those
as distinct identities, since analyzer behavior/findings can change
without the repository or ``.patchfrog.yml`` changing at all. See
:meth:`patchfrog.analysis.service.StaticAnalysisService._run`, which
combines this fingerprint with :meth:`AnalysisConfig.fingerprint` to form
the full run identity.

Discovery here is exactly as deterministic as each adapter's own
``discover()`` already is (a single ``--version`` invocation, never
downloading or negotiating anything) -- this module only asks every
*selected* analyzer once, concurrently, up front, so the resulting
fingerprint is known before committing to (or reusing) a canonical run
identity.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from patchfrog.analysis.analyzers.base import Analyzer, AnalyzerAvailability

#: Bumped whenever the analysis engine's own normalization, deduplication,
#: or fingerprinting algorithm changes materially enough that a prior
#: canonical run's findings can no longer be trusted as equivalent to what
#: re-running now would produce, even with identical config and identical
#: analyzer versions.
ANALYSIS_ENGINE_VERSION = 1


@dataclass(frozen=True, slots=True)
class AnalyzerToolchainEntry:
    name: str
    availability: AnalyzerAvailability
    version: str | None


@dataclass(frozen=True, slots=True)
class ToolchainSnapshot:
    """The effective, discovered analysis toolchain for one run."""

    analyzers: tuple[AnalyzerToolchainEntry, ...]
    ruleset_hashes: dict[str, str]
    engine_version: int = ANALYSIS_ENGINE_VERSION

    def fingerprint(self) -> str:
        payload = {
            "engine_version": self.engine_version,
            "analyzers": [
                {"name": e.name, "availability": e.availability.value, "version": e.version}
                for e in sorted(self.analyzers, key=lambda e: e.name)
            ],
            "ruleset_hashes": dict(sorted(self.ruleset_hashes.items())),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


async def discover_toolchain(selected: Mapping[str, Analyzer]) -> ToolchainSnapshot:
    """Discover the effective toolchain for exactly the analyzers
    ``select_analyzers`` chose to consider for this run.

    Cheap -- each adapter's ``discover()`` is a single ``--version``
    call -- and run concurrently, up front, so the run identity is known
    before deciding whether to reuse a prior canonical run or execute a
    fresh one.
    """

    names = sorted(selected)
    results = await asyncio.gather(*(selected[name].discover() for name in names))
    entries = tuple(
        AnalyzerToolchainEntry(name=name, availability=result.availability, version=result.version)
        for name, result in zip(names, results, strict=True)
    )

    ruleset_hashes: dict[str, str] = {}
    if "semgrep" in selected:
        from patchfrog.analysis.analyzers.semgrep import BUNDLED_RULES_PATH

        ruleset_hashes["semgrep_rules"] = _hash_file(BUNDLED_RULES_PATH)

    return ToolchainSnapshot(analyzers=entries, ruleset_hashes=ruleset_hashes)


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
