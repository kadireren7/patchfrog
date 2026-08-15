from __future__ import annotations

from pathlib import Path

import pytest

from patchfrog.analysis.analyzers.base import AnalyzerAvailability, AnalyzerDiscoveryResult
from patchfrog.analysis.domain import (
    AnalysisContext,
    AnalyzerCapabilities,
    AnalyzerExecutionStatus,
    AnalyzerResult,
)
from patchfrog.analysis.toolchain import (
    AnalyzerToolchainEntry,
    ToolchainSnapshot,
    discover_toolchain,
)
from patchfrog.domain.code import Language


def _snapshot(
    *, version: str = "1.0.0", ruleset_hash: str = "aaa", engine_version: int = 1
) -> ToolchainSnapshot:
    return ToolchainSnapshot(
        analyzers=(
            AnalyzerToolchainEntry(name="ruff", availability=AnalyzerAvailability.AVAILABLE, version=version),
        ),
        ruleset_hashes={"semgrep_rules": ruleset_hash},
        engine_version=engine_version,
    )


def test_identical_snapshots_have_identical_fingerprints() -> None:
    assert _snapshot().fingerprint() == _snapshot().fingerprint()


def test_changing_analyzer_version_changes_fingerprint() -> None:
    assert _snapshot(version="1.0.0").fingerprint() != _snapshot(version="1.1.0").fingerprint()


def test_changing_ruleset_hash_changes_fingerprint() -> None:
    assert _snapshot(ruleset_hash="aaa").fingerprint() != _snapshot(ruleset_hash="bbb").fingerprint()


def test_changing_engine_version_changes_fingerprint() -> None:
    assert _snapshot(engine_version=1).fingerprint() != _snapshot(engine_version=2).fingerprint()


def test_analyzer_ordering_does_not_affect_fingerprint() -> None:
    a = ToolchainSnapshot(
        analyzers=(
            AnalyzerToolchainEntry(name="ruff", availability=AnalyzerAvailability.AVAILABLE, version="1.0"),
            AnalyzerToolchainEntry(name="semgrep", availability=AnalyzerAvailability.AVAILABLE, version="2.0"),
        ),
        ruleset_hashes={},
    )
    b = ToolchainSnapshot(
        analyzers=(
            AnalyzerToolchainEntry(name="semgrep", availability=AnalyzerAvailability.AVAILABLE, version="2.0"),
            AnalyzerToolchainEntry(name="ruff", availability=AnalyzerAvailability.AVAILABLE, version="1.0"),
        ),
        ruleset_hashes={},
    )
    assert a.fingerprint() == b.fingerprint()


def test_changing_availability_changes_fingerprint() -> None:
    available = ToolchainSnapshot(
        analyzers=(
            AnalyzerToolchainEntry(name="cppcheck", availability=AnalyzerAvailability.AVAILABLE, version="2.17"),
        ),
        ruleset_hashes={},
    )
    unavailable = ToolchainSnapshot(
        analyzers=(
            AnalyzerToolchainEntry(name="cppcheck", availability=AnalyzerAvailability.UNAVAILABLE, version=None),
        ),
        ruleset_hashes={},
    )
    assert available.fingerprint() != unavailable.fingerprint()


class _StubAnalyzer:
    """A minimal analyzer whose discovered version is controllable, so
    ``discover_toolchain`` can be exercised without a real subprocess."""

    def __init__(self, *, name: str, version: str) -> None:
        self._name = name
        self._version = version
        self.capabilities = AnalyzerCapabilities(
            name=name, languages=frozenset({Language.PYTHON}), requires_compile_database=False,
            supports_file_scope=True, supports_repository_scope=True, supports_changed_files_only=True,
            supports_structured_output=True,
        )

    async def discover(self) -> AnalyzerDiscoveryResult:
        return AnalyzerDiscoveryResult(availability=AnalyzerAvailability.AVAILABLE, version=self._version)

    async def analyze(self, context: AnalysisContext) -> AnalyzerResult:
        return AnalyzerResult(
            analyzer=self._name, version=self._version, status=AnalyzerExecutionStatus.SUCCEEDED, duration_ms=0.0
        )


async def test_discover_toolchain_reflects_each_selected_analyzers_discovered_version() -> None:
    selected = {"stub": _StubAnalyzer(name="stub", version="1.2.3")}

    snapshot = await discover_toolchain(selected)

    assert snapshot.analyzers == (
        AnalyzerToolchainEntry(name="stub", availability=AnalyzerAvailability.AVAILABLE, version="1.2.3"),
    )


async def test_discover_toolchain_result_changes_when_discovered_version_changes() -> None:
    fp1 = (await discover_toolchain({"stub": _StubAnalyzer(name="stub", version="1.0.0")})).fingerprint()
    fp2 = (await discover_toolchain({"stub": _StubAnalyzer(name="stub", version="2.0.0")})).fingerprint()

    assert fp1 != fp2


async def test_discover_toolchain_only_hashes_ruleset_when_semgrep_selected() -> None:
    without_semgrep = await discover_toolchain({"stub": _StubAnalyzer(name="stub", version="1.0")})
    assert without_semgrep.ruleset_hashes == {}


async def test_discover_toolchain_ruleset_hash_changes_with_bundled_rules_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real regression this exists for: PatchFrog editing its own
    bundled semgrep ruleset must invalidate any prior canonical run, even
    though nothing about the repository or .patchfrog.yml changed."""

    import patchfrog.analysis.toolchain as toolchain_module
    from patchfrog.analysis.analyzers.semgrep import SemgrepAnalyzer

    rules_v1 = tmp_path / "rules_v1.yml"
    rules_v1.write_text("rules: []\n")
    rules_v2 = tmp_path / "rules_v2.yml"
    rules_v2.write_text("rules:\n  - id: extra-rule\n")

    monkeypatch.setattr(
        "patchfrog.analysis.analyzers.semgrep.BUNDLED_RULES_PATH", rules_v1, raising=True
    )
    snapshot_v1 = await toolchain_module.discover_toolchain({"semgrep": SemgrepAnalyzer()})

    monkeypatch.setattr(
        "patchfrog.analysis.analyzers.semgrep.BUNDLED_RULES_PATH", rules_v2, raising=True
    )
    snapshot_v2 = await toolchain_module.discover_toolchain({"semgrep": SemgrepAnalyzer()})

    assert snapshot_v1.ruleset_hashes["semgrep_rules"] != snapshot_v2.ruleset_hashes["semgrep_rules"]
    assert snapshot_v1.fingerprint() != snapshot_v2.fingerprint()
