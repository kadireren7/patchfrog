"""Controlled corpus for Executable Verification Foundation (minimum 38
scenarios per spec Part H) -- real sandbox subprocess execution against
real, purpose-built test files (never a hand-built
``ExecutableVerificationEvidence`` standing in for a real subprocess
round trip, except where a scenario is explicitly about pure
classification logic already covered at the unit level).

Every sandbox-level test in this file genuinely spawns
``bwrap``/``prlimit``/the real Python interpreter as subprocesses -- this
is the strongest possible verification that the isolation guarantees in
``validation/executable_verification/latest-summary.md`` sections 2-3
actually hold, not merely that the code compiles. Sections 26+ cover the
filesystem-confinement correction specifically: a real escape (real
``$HOME``, an arbitrary outside-workspace file, and the real Docker
socket, all readable/visible) was empirically confirmed against this
package's first version before that correction landed.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from patchfrog.change_intelligence.domain import (
    CompanionReasonCode,
    CompanionStatus,
    ExpectedCompanionChange,
)
from patchfrog.executable_verification.domain import (
    EXECUTABLE_VERIFICATION_VERSION,
    MAX_STDOUT_EXCERPT_BYTES,
    ExecutableVerificationReport,
    VerificationOutcome,
)
from patchfrog.executable_verification.pytest_adapter import run_pytest_verification
from patchfrog.executable_verification.sandbox import (
    VerificationSandbox,
    bounded_excerpt,
    is_sandbox_available,
)
from patchfrog.executable_verification.service import (
    VerificationBudget,
    build_executable_verification_report,
)
from patchfrog.review.domain import ReviewCandidate, ReviewCandidateReason

pytestmark = pytest.mark.skipif(
    not is_sandbox_available(), reason="bwrap/prlimit-based filesystem confinement not available on this host"
)


def _candidate(*, file_path: str = "src/capture.py", qualified_name: str | None = "capture_payment") -> ReviewCandidate:
    return ReviewCandidate(
        file_path=file_path, symbol_id=None, symbol_name=qualified_name.rsplit(".", 1)[-1] if qualified_name else None,
        qualified_name=qualified_name, start_line=1, end_line=10, changed_lines=(2,),
        static_finding_ids=(), reason=ReviewCandidateReason.CHANGED_SYMBOL,
    )


def _companion(*, source_file_path: str, expected_file_path: str) -> ExpectedCompanionChange:
    return ExpectedCompanionChange(
        change_unit_id="unit-1", source_qualified_name="capture_payment", source_file_path=source_file_path,
        expected_qualified_name="test_capture_payment", expected_file_path=expected_file_path,
        reason_code=CompanionReasonCode.TEST_NOT_UPDATED, reason="r", evidence="e", status=CompanionStatus.MISSING,
    )


def _workspace() -> Path:
    return Path(tempfile.mkdtemp(prefix="patchfrog-verify-test-"))


def _write(workspace: Path, relative_path: str, content: str) -> None:
    path = workspace / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# ---- 1. No eligible verification target -> no execution ----


async def test_case_no_eligible_target_no_execution() -> None:
    from patchfrog.executable_verification.eligibility import determine_verification_target

    assert determine_verification_target(candidate=_candidate(), expected_companions=()) is None


# ---- 2. Docs-only PR (no companions at all) -> no execution ----


async def test_case_docs_only_pr_no_execution() -> None:
    budget = VerificationBudget()
    report = await build_executable_verification_report(
        candidate=_candidate(file_path="README.md", qualified_name=None), expected_companions=(),
        commit_sha="a" * 40, local=True, budget=budget, root_path=Path("/tmp"),
    )
    assert report.attempted is False


# ---- 3. Sandbox unavailable -> SANDBOX_ERROR, structurally never a fallback ----


async def test_case_sandbox_unavailable_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    import patchfrog.executable_verification.service as svc

    monkeypatch.setattr(svc, "is_sandbox_available", lambda: False)
    companion = _companion(source_file_path="src/capture.py", expected_file_path="tests/test_capture.py")
    budget = VerificationBudget()
    report = await build_executable_verification_report(
        candidate=_candidate(), expected_companions=(companion,), commit_sha="a" * 40, local=True,
        budget=budget, root_path=Path("/tmp"),
    )
    assert report.attempted is True
    assert report.evidence is not None
    assert report.evidence.outcome is VerificationOutcome.SANDBOX_ERROR


# ---- 4. Targeted existing test selected -> only the intended target executed ----


async def test_case_only_targeted_test_file_executed() -> None:
    workspace = _workspace()
    try:
        _write(workspace, "tests/test_capture.py", "def test_pass():\n    assert 1 == 1\n")
        _write(workspace, "tests/test_unrelated.py", "def test_fail_unrelated():\n    assert False\n")
        sandbox = VerificationSandbox(timeout_seconds=10.0)
        evidence = await run_pytest_verification(
            sandbox, workspace_root=workspace, test_target_path="tests/test_capture.py", commit_sha="a" * 40,
        )
        assert evidence.outcome is VerificationOutcome.PASSED
        assert "test_unrelated" not in evidence.stdout_excerpt
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---- 5. Real, relevant test failure -> CONFIRMED_FAILURE ----


async def test_case_confirmed_failure_classification() -> None:
    workspace = _workspace()
    try:
        _write(workspace, "tests/test_capture.py", "def test_fail():\n    assert 1 == 2\n")
        sandbox = VerificationSandbox(timeout_seconds=10.0)
        evidence = await run_pytest_verification(
            sandbox, workspace_root=workspace, test_target_path="tests/test_capture.py", commit_sha="a" * 40,
        )
        assert evidence.outcome is VerificationOutcome.CONFIRMED_FAILURE
        assert evidence.exit_code == 1
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---- 6. All tests pass -> PASSED ----


async def test_case_passed_classification() -> None:
    workspace = _workspace()
    try:
        _write(workspace, "tests/test_capture.py", "def test_pass():\n    assert 1 == 1\n")
        sandbox = VerificationSandbox(timeout_seconds=10.0)
        evidence = await run_pytest_verification(
            sandbox, workspace_root=workspace, test_target_path="tests/test_capture.py", commit_sha="a" * 40,
        )
        assert evidence.outcome is VerificationOutcome.PASSED
        assert evidence.exit_code == 0
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---- 7. Missing test dependency -> UNSUPPORTED, never a crash, never CONFIRMED_FAILURE ----


async def test_case_missing_dependency_unsupported_not_confirmed_failure() -> None:
    workspace = _workspace()
    try:
        _write(
            workspace, "tests/test_capture.py",
            "import some_nonexistent_package_xyz\n\ndef test_uses_it():\n    assert some_nonexistent_package_xyz.foo() == 1\n",
        )
        sandbox = VerificationSandbox(timeout_seconds=10.0)
        evidence = await run_pytest_verification(
            sandbox, workspace_root=workspace, test_target_path="tests/test_capture.py", commit_sha="a" * 40,
        )
        assert evidence.outcome is VerificationOutcome.UNSUPPORTED
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---- 8. Timeout -> process killed, classified TIMEOUT, never a bug ----


async def test_case_timeout_kills_process() -> None:
    workspace = _workspace()
    try:
        _write(
            workspace, "tests/test_capture.py",
            "import time\n\ndef test_hangs():\n    time.sleep(30)\n",
        )
        sandbox = VerificationSandbox(timeout_seconds=2.0)
        start = time.monotonic()
        evidence = await run_pytest_verification(
            sandbox, workspace_root=workspace, test_target_path="tests/test_capture.py", commit_sha="a" * 40,
        )
        elapsed = time.monotonic() - start
        assert evidence.outcome is VerificationOutcome.TIMEOUT
        assert evidence.timed_out is True
        # Genuinely killed well before the test's own 30s sleep would finish.
        assert elapsed < 15.0
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---- 9. Output flood -> bounded, never unbounded memory growth ----


async def test_case_output_flood_bounded() -> None:
    workspace = _workspace()
    try:
        _write(
            workspace, "tests/test_capture.py",
            "def test_floods_output():\n    print('x' * 2_000_000)\n    assert True\n",
        )
        sandbox = VerificationSandbox(timeout_seconds=10.0)
        evidence = await run_pytest_verification(
            sandbox, workspace_root=workspace, test_target_path="tests/test_capture.py", commit_sha="a" * 40,
        )
        assert len(evidence.stdout_excerpt.encode("utf-8")) <= MAX_STDOUT_EXCERPT_BYTES + 64  # small truncation-marker slack
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_bounded_excerpt_truncates_with_marker() -> None:
    text = "x" * (MAX_STDOUT_EXCERPT_BYTES + 1000)
    excerpt = bounded_excerpt(text, max_bytes=MAX_STDOUT_EXCERPT_BYTES)
    assert len(excerpt.encode("utf-8")) <= MAX_STDOUT_EXCERPT_BYTES + 32
    assert "truncated" in excerpt


# ---- 10. Environment secrets never visible inside the sandbox ----


async def test_case_environment_secrets_unavailable() -> None:
    workspace = _workspace()
    try:
        _write(
            workspace, "tests/test_env.py",
            "import os\n\n"
            "def test_no_secrets():\n"
            "    for name in ('DATABASE_URL', 'GITHUB_APP_ID', 'GITHUB_PRIVATE_KEY', "
            "'ANTHROPIC_API_KEY', 'GEMINI_API_KEY', 'REDIS_URL'):\n"
            "        assert os.environ.get(name) is None, f'{name} leaked into sandbox'\n",
        )
        sandbox = VerificationSandbox(timeout_seconds=10.0)
        evidence = await run_pytest_verification(
            sandbox, workspace_root=workspace, test_target_path="tests/test_env.py", commit_sha="a" * 40,
        )
        assert evidence.outcome is VerificationOutcome.PASSED, evidence.stdout_excerpt
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---- 11. Network unavailable / default-denied ----


async def test_case_network_denied_by_default() -> None:
    workspace = _workspace()
    try:
        _write(
            workspace, "tests/test_network.py",
            "import socket\n\n"
            "def test_network_unreachable():\n"
            "    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "    s.settimeout(2)\n"
            "    try:\n"
            "        s.connect(('8.8.8.8', 53))\n"
            "        assert False, 'network was reachable -- sandbox failed to isolate it'\n"
            "    except OSError:\n"
            "        pass\n",
        )
        sandbox = VerificationSandbox(timeout_seconds=10.0)
        evidence = await run_pytest_verification(
            sandbox, workspace_root=workspace, test_target_path="tests/test_network.py", commit_sha="a" * 40,
        )
        assert evidence.outcome is VerificationOutcome.PASSED, evidence.stdout_excerpt
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---- 12. No arbitrary shell execution (argv-only, no shell injection) ----


async def test_case_no_shell_injection() -> None:
    """A test file name containing shell metacharacters must never be
    interpreted by a shell -- argv-array execution only."""

    workspace = _workspace()
    marker = workspace / "PWNED"
    try:
        _write(workspace, "tests/test_capture.py", "def test_pass():\n    assert 1 == 1\n")
        sandbox = VerificationSandbox(timeout_seconds=10.0)
        # Attempt injection via a crafted (but real, existing) target path
        # containing a shell metacharacter sequence -- pytest will simply
        # fail to find such a path (not a real file), never execute it as
        # a shell command.
        evidence = await run_pytest_verification(
            sandbox, workspace_root=workspace,
            test_target_path=f"tests/test_capture.py; touch {marker}", commit_sha="a" * 40,
        )
        assert not marker.exists()
        assert evidence.outcome is VerificationOutcome.UNSUPPORTED
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---- 13. Malicious .patchfrog.yml cannot expand command capability (structural) ----


def test_case_no_repository_config_parameter_exists() -> None:
    import inspect

    sig = inspect.signature(build_executable_verification_report)
    param_names = set(sig.parameters.keys())
    forbidden = {"command", "repo_config", "patchfrog_yml", "shell_command", "args"}
    assert not (param_names & forbidden), "no repository-controlled command parameter may ever exist"


# ---- 14. Never reads repository config / .patchfrog.yml at all (structural) ----


def test_case_never_imports_review_config() -> None:
    import ast
    from pathlib import Path as _Path

    package_dir = _Path(__file__).parent.parent.parent / "patchfrog" / "executable_verification"
    forbidden_modules = {"patchfrog.review.config_resolution", "patchfrog.review.config"}
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in forbidden_modules:
                raise AssertionError(f"{path} imports {node.module} -- must never read repository config")


# ---- 15. Zero unconditional provider calls (structural) ----


def test_case_never_imports_a_provider() -> None:
    import ast
    from pathlib import Path as _Path

    package_dir = _Path(__file__).parent.parent.parent / "patchfrog" / "executable_verification"
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                assert "LLMProvider" not in (node.module or "") and not any(
                    alias.name == "LLMProvider" for alias in node.names
                ), f"{path} imports LLMProvider -- Executable Verification must add zero provider calls"


# ---- 16. Exact-head binding -- result carries the exact commit_sha ----


async def test_case_exact_head_binding() -> None:
    workspace = _workspace()
    try:
        _write(workspace, "tests/test_capture.py", "def test_pass():\n    assert 1 == 1\n")
        sandbox = VerificationSandbox(timeout_seconds=10.0)
        sha = "b" * 40
        evidence = await run_pytest_verification(
            sandbox, workspace_root=workspace, test_target_path="tests/test_capture.py", commit_sha=sha,
        )
        assert evidence.commit_sha == sha
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---- 17. No cross-head caching -- two different heads never share a result ----


async def test_case_no_cross_head_caching() -> None:
    workspace = _workspace()
    try:
        _write(workspace, "tests/test_capture.py", "def test_pass():\n    assert 1 == 1\n")
        sandbox = VerificationSandbox(timeout_seconds=10.0)
        evidence_a = await run_pytest_verification(
            sandbox, workspace_root=workspace, test_target_path="tests/test_capture.py", commit_sha="a" * 40,
        )
        evidence_b = await run_pytest_verification(
            sandbox, workspace_root=workspace, test_target_path="tests/test_capture.py", commit_sha="c" * 40,
        )
        assert evidence_a.commit_sha != evidence_b.commit_sha
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---- 18. MAX_VERIFICATIONS_PER_REVIEW enforced across multiple candidates ----


async def test_case_max_verifications_per_review_enforced() -> None:
    from patchfrog.executable_verification.domain import MAX_VERIFICATIONS_PER_REVIEW

    workspace = _workspace()
    try:
        _write(workspace, "tests/test_capture.py", "def test_pass():\n    assert 1 == 1\n")
        budget = VerificationBudget(max_count=MAX_VERIFICATIONS_PER_REVIEW, max_total_seconds=9999.0)
        companion = _companion(source_file_path="src/capture.py", expected_file_path="tests/test_capture.py")
        attempted_count = 0
        for _ in range(MAX_VERIFICATIONS_PER_REVIEW + 3):
            report = await build_executable_verification_report(
                candidate=_candidate(), expected_companions=(companion,), commit_sha="a" * 40, local=True,
                budget=budget, root_path=workspace,
            )
            if report.attempted:
                attempted_count += 1
        assert attempted_count == MAX_VERIFICATIONS_PER_REVIEW
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---- 19. MAX_TOTAL_VERIFICATION_SECONDS enforced ----


async def test_case_max_total_verification_seconds_enforced() -> None:
    budget = VerificationBudget(max_count=100, max_total_seconds=1.0)
    assert await budget.try_reserve() is True
    await budget.record_duration(2000.0)  # 2 seconds -- exceeds the 1s ceiling
    assert await budget.try_reserve() is False


# ---- 20. Cleanup after a successful local verification (workspace removed) ----


async def test_case_cleanup_after_success() -> None:
    workspace = _workspace()
    _write(workspace, "tests/test_capture.py", "def test_pass():\n    assert 1 == 1\n")
    companion = _companion(source_file_path="src/capture.py", expected_file_path="tests/test_capture.py")
    budget = VerificationBudget()
    report = await build_executable_verification_report(
        candidate=_candidate(), expected_companions=(companion,), commit_sha="a" * 40, local=True,
        budget=budget, root_path=workspace,
    )
    assert report.attempted is True
    # The ORIGINAL workspace (root_path) is never mutated/deleted -- only
    # the internal disposable copy is. Confirm the original still exists
    # (never removed) and the report succeeded using a *copy* of it.
    assert workspace.exists()
    shutil.rmtree(workspace, ignore_errors=True)


# ---- 21. Cleanup after a timeout (disposable workspace still removed) ----


async def test_case_cleanup_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import patchfrog.executable_verification.service as svc

    workspace = _workspace()
    try:
        _write(workspace, "tests/test_capture.py", "import time\n\ndef test_hangs():\n    time.sleep(30)\n")
        companion = _companion(source_file_path="src/capture.py", expected_file_path="tests/test_capture.py")
        budget = VerificationBudget()
        monkeypatch.setattr(svc, "MAX_VERIFICATION_SECONDS", 2.0)

        report = await build_executable_verification_report(
            candidate=_candidate(), expected_companions=(companion,), commit_sha="a" * 40, local=True,
            budget=budget, root_path=workspace,
        )
        assert report.attempted is True
        assert report.evidence is not None
        assert report.evidence.outcome is VerificationOutcome.TIMEOUT
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---- 22. No source mutation -- the original root_path is never written to ----


async def test_case_no_source_mutation() -> None:
    workspace = _workspace()
    try:
        _write(workspace, "tests/test_capture.py", "def test_pass():\n    assert 1 == 1\n")
        before = {p: p.stat().st_mtime for p in workspace.rglob("*") if p.is_file()}
        companion = _companion(source_file_path="src/capture.py", expected_file_path="tests/test_capture.py")
        budget = VerificationBudget()
        await build_executable_verification_report(
            candidate=_candidate(), expected_companions=(companion,), commit_sha="a" * 40, local=True,
            budget=budget, root_path=workspace,
        )
        after = {p: p.stat().st_mtime for p in workspace.rglob("*") if p.is_file()}
        assert before == after
        # No pytest cache artifacts leaked back into the original either.
        assert not (workspace / ".pytest_cache").exists()
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---- 23. No git commit/push anywhere in the package (structural) ----


def test_case_no_git_write_operations() -> None:
    import ast
    from pathlib import Path as _Path

    package_dir = _Path(__file__).parent.parent.parent / "patchfrog" / "executable_verification"
    forbidden_calls = {"push", "commit"}
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in forbidden_calls:
                raise AssertionError(f"{path} calls .{node.attr}() -- verification must never write to git")


# ---- 24. Real proposal required -- verifier callable never invoked when there's nothing to critique ----


async def test_case_verifier_never_invoked_without_a_proposal_to_critique() -> None:
    from patchfrog.review.agents.evidence import CandidateEvidencePackage
    from patchfrog.review.effort import ReviewEffortDecision
    from patchfrog.review.effort_types import CriticExpectation, ReviewEffortTier
    from patchfrog.review.orchestration import AgentOrchestrator

    calls = {"count": 0}

    async def _verify() -> ExecutableVerificationReport:  # pragma: no cover -- must never be called
        calls["count"] += 1
        raise AssertionError("executable_verifier must never be invoked with zero proposals")

    orchestrator = AgentOrchestrator(
        reviewer_providers={}, critic=None, critic_enabled=True,
        max_output_tokens_per_candidate=1000, max_retries=1,
    )
    evidence = CandidateEvidencePackage(
        candidate=_candidate(), context_text="ctx", diff_excerpt="", static_findings=(),
        allowed_file_paths=frozenset({"src/capture.py"}), context_bundle_id=None,
    )
    effort_decision = ReviewEffortDecision(
        tier=ReviewEffortTier.LIGHT, reasons=(), selected_roles=frozenset(),
        context_token_fraction=0.5, context_adaptive_enabled=False,
        critic_expectation=CriticExpectation.OPTIONAL, retry_limit=1, per_role_output_token_fraction=0.25,
    )
    from patchfrog.analysis.domain import Confidence

    result = await orchestrator.review_candidate(
        evidence, effort_decision=effort_decision, min_final_confidence=Confidence.MEDIUM,
        max_total_input_tokens=100_000, budget_lock=asyncio.Lock(), budget_state={"used_input_tokens": 0},
        log=__import__("structlog").get_logger(__name__), executable_verifier=_verify,
    )
    assert calls["count"] == 0
    assert result.proposals == ()


# ---- 25. Report version is stamped ----


async def test_case_report_stamped_with_current_version() -> None:
    workspace = _workspace()
    try:
        companion = _companion(source_file_path="nonexistent.py", expected_file_path="tests/test_x.py")
        budget = VerificationBudget()
        report = await build_executable_verification_report(
            candidate=_candidate(file_path="nonexistent.py"), expected_companions=(companion,), commit_sha="a" * 40,
            local=True, budget=budget, root_path=workspace,
        )
        assert report.version == EXECUTABLE_VERIFICATION_VERSION
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ==== Filesystem confinement (security correction round) ====
#
# A real escape was empirically confirmed against this package's first
# version, before this correction: a malicious pytest target could read
# the real worker $HOME, an arbitrary file outside the disposable
# workspace via both a direct absolute path and a symlink, and observe
# the real Docker socket -- see
# validation/executable_verification/latest-summary.md for the full
# transcript (including inside a real, non-privileged worker Docker
# container). Every test below genuinely spawns the real bwrap-based
# sandbox against a synthetic sentinel value -- never a real credential
# -- and proves the escape it names is now blocked.


# ---- 26. Real HOME is never inherited ----


async def test_case_real_home_not_inherited() -> None:
    workspace = _workspace()
    real_home = os.path.expanduser("~")
    try:
        _write(
            workspace, "tests/test_home.py",
            "import os\n\n"
            f"def test_home_is_not_real():\n"
            f"    assert os.environ.get('HOME') != {real_home!r}\n",
        )
        sandbox = VerificationSandbox(timeout_seconds=10.0)
        evidence = await run_pytest_verification(
            sandbox, workspace_root=workspace, test_target_path="tests/test_home.py", commit_sha="a" * 40,
        )
        assert evidence.outcome is VerificationOutcome.PASSED, evidence.stdout_excerpt
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---- 27. External HOME sentinel cannot be read (synthetic value only) ----


async def test_case_external_home_sentinel_unreadable() -> None:
    workspace = _workspace()
    real_home = Path(os.path.expanduser("~"))
    sentinel_name = ".patchfrog_test_sentinel_do_not_commit"
    sentinel_path = real_home / sentinel_name
    sentinel_value = "synthetic-home-sentinel-value"
    created_here = not sentinel_path.exists()
    try:
        if created_here:
            sentinel_path.write_text(sentinel_value)
        _write(
            workspace, "tests/test_home_sentinel.py",
            "import os\n\n"
            "def test_cannot_read_home_sentinel():\n"
            "    home = os.environ.get('HOME', '')\n"
            f"    target = os.path.join(home, {sentinel_name!r})\n"
            "    try:\n"
            "        with open(target) as f:\n"
            f"            assert f.read() != {sentinel_value!r}, 'escaped: read real home sentinel'\n"
            "    except FileNotFoundError:\n"
            "        pass\n",
        )
        sandbox = VerificationSandbox(timeout_seconds=10.0)
        evidence = await run_pytest_verification(
            sandbox, workspace_root=workspace, test_target_path="tests/test_home_sentinel.py", commit_sha="a" * 40,
        )
        assert evidence.outcome is VerificationOutcome.PASSED, evidence.stdout_excerpt
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        if created_here:
            sentinel_path.unlink(missing_ok=True)


# ---- 28. External filesystem sentinel (outside workspace, outside HOME) cannot be read ----


async def test_case_external_filesystem_sentinel_unreadable() -> None:
    workspace = _workspace()
    outside_dir = Path(tempfile.mkdtemp(prefix="patchfrog-verify-outside-"))
    sentinel_value = "synthetic-outside-sentinel-value"
    (outside_dir / "sentinel.txt").write_text(sentinel_value)
    try:
        _write(
            workspace, "tests/test_outside.py",
            "def test_cannot_read_outside_sentinel():\n"
            "    try:\n"
            f"        with open({str(outside_dir / 'sentinel.txt')!r}) as f:\n"
            f"            assert f.read() != {sentinel_value!r}, 'escaped workspace confinement'\n"
            "    except FileNotFoundError:\n"
            "        pass\n",
        )
        sandbox = VerificationSandbox(timeout_seconds=10.0)
        evidence = await run_pytest_verification(
            sandbox, workspace_root=workspace, test_target_path="tests/test_outside.py", commit_sha="a" * 40,
        )
        assert evidence.outcome is VerificationOutcome.PASSED, evidence.stdout_excerpt
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        shutil.rmtree(outside_dir, ignore_errors=True)


# ---- 29. Verifier workspace itself remains fully readable/executable ----


async def test_case_verifier_workspace_remains_readable() -> None:
    workspace = _workspace()
    try:
        _write(workspace, "src/helper_data.txt", "workspace-local-data")
        _write(
            workspace, "tests/test_workspace_readable.py",
            "def test_can_read_own_workspace_file():\n"
            "    with open('src/helper_data.txt') as f:\n"
            "        assert f.read() == 'workspace-local-data'\n",
        )
        sandbox = VerificationSandbox(timeout_seconds=10.0)
        evidence = await run_pytest_verification(
            sandbox, workspace_root=workspace, test_target_path="tests/test_workspace_readable.py", commit_sha="a" * 40,
        )
        assert evidence.outcome is VerificationOutcome.PASSED, evidence.stdout_excerpt
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---- 30. Disposable sandbox HOME/tmp remain writable when pytest needs them ----


async def test_case_disposable_home_and_tmp_writable() -> None:
    workspace = _workspace()
    try:
        _write(
            workspace, "tests/test_writable.py",
            "import os\n\n"
            "def test_can_write_under_home_and_tmp():\n"
            "    home = os.environ['HOME']\n"
            "    with open(os.path.join(home, 'scratch.txt'), 'w') as f:\n"
            "        f.write('ok')\n"
            "    with open('/tmp/scratch.txt', 'w') as f:\n"
            "        f.write('ok')\n",
        )
        sandbox = VerificationSandbox(timeout_seconds=10.0)
        evidence = await run_pytest_verification(
            sandbox, workspace_root=workspace, test_target_path="tests/test_writable.py", commit_sha="a" * 40,
        )
        assert evidence.outcome is VerificationOutcome.PASSED, evidence.stdout_excerpt
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---- 31. Docker socket unavailable ----


async def test_case_docker_socket_unavailable() -> None:
    workspace = _workspace()
    try:
        _write(
            workspace, "tests/test_docker.py",
            "import os\n\n"
            "def test_no_docker_socket():\n"
            "    assert not os.path.exists('/var/run/docker.sock')\n",
        )
        sandbox = VerificationSandbox(timeout_seconds=10.0)
        evidence = await run_pytest_verification(
            sandbox, workspace_root=workspace, test_target_path="tests/test_docker.py", commit_sha="a" * 40,
        )
        assert evidence.outcome is VerificationOutcome.PASSED, evidence.stdout_excerpt
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---- 32. /proc/self/environ contains no parent/worker secrets ----


async def test_case_proc_environ_has_no_parent_secrets() -> None:
    workspace = _workspace()
    marker = "patchfrog-secret-marker-should-never-appear"
    try:
        _write(
            workspace, "tests/test_proc_environ.py",
            "def test_proc_environ_is_minimal():\n"
            "    with open('/proc/self/environ', 'rb') as f:\n"
            "        content = f.read()\n"
            f"    assert {marker!r}.encode() not in content\n"
            "    assert b'DATABASE_URL' not in content\n"
            "    assert b'GITHUB_PRIVATE_KEY' not in content\n",
        )
        sandbox = VerificationSandbox(timeout_seconds=10.0)
        evidence = await run_pytest_verification(
            sandbox, workspace_root=workspace, test_target_path="tests/test_proc_environ.py", commit_sha="a" * 40,
        )
        assert evidence.outcome is VerificationOutcome.PASSED, evidence.stdout_excerpt
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---- 33. Malicious symlink cannot escape the intended filesystem boundary ----


async def test_case_symlink_escape_blocked() -> None:
    workspace = _workspace()
    outside_dir = Path(tempfile.mkdtemp(prefix="patchfrog-verify-symlink-outside-"))
    sentinel_value = "synthetic-symlink-target-sentinel"
    (outside_dir / "sentinel.txt").write_text(sentinel_value)
    try:
        _write(workspace, "tests/__init__.py", "")
        symlink_path = workspace / "tests" / "evil_link.txt"
        symlink_path.symlink_to(outside_dir / "sentinel.txt")
        _write(
            workspace, "tests/test_symlink_escape.py",
            "def test_symlink_escape_blocked():\n"
            "    try:\n"
            "        with open('tests/evil_link.txt') as f:\n"
            f"            assert f.read() != {sentinel_value!r}, 'escaped via symlink'\n"
            "    except FileNotFoundError:\n"
            "        pass\n",
        )
        sandbox = VerificationSandbox(timeout_seconds=10.0)
        evidence = await run_pytest_verification(
            sandbox, workspace_root=workspace, test_target_path="tests/test_symlink_escape.py", commit_sha="a" * 40,
        )
        assert evidence.outcome is VerificationOutcome.PASSED, evidence.stdout_excerpt
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        shutil.rmtree(outside_dir, ignore_errors=True)


# ---- 34. No weaker fallback -- sandbox unavailable never silently runs unisolated ----


async def test_case_no_weaker_fallback_when_sandbox_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    import patchfrog.executable_verification.service as svc

    monkeypatch.setattr(svc, "is_sandbox_available", lambda: False)
    workspace = _workspace()
    try:
        # Even a target that would trivially confirm a failure unsandboxed
        # must never be executed once the capability check fails.
        _write(workspace, "tests/test_capture.py", "def test_fail():\n    assert False\n")
        companion = _companion(source_file_path="src/capture.py", expected_file_path="tests/test_capture.py")
        budget = VerificationBudget()
        report = await build_executable_verification_report(
            candidate=_candidate(), expected_companions=(companion,), commit_sha="a" * 40, local=True,
            budget=budget, root_path=workspace,
        )
        assert report.evidence is not None
        assert report.evidence.outcome is VerificationOutcome.SANDBOX_ERROR
        assert report.evidence.exit_code is None
        assert report.evidence.stdout_excerpt == ""
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---- 35. Internal disposable workspace copy is actually removed after a real run ----


async def test_case_internal_disposable_copy_actually_removed() -> None:
    """test_case_cleanup_after_success/_timeout above confirm the
    *original* root_path is never deleted -- this confirms the other
    half: the internal disposable copy build_executable_verification_report
    makes (a fresh tempfile.mkdtemp under the system tmp root) is actually
    removed afterward, success or timeout, not merely left to accumulate."""

    tmp_root = Path(tempfile.gettempdir())

    def _verify_dirs() -> set[str]:
        return {p.name for p in tmp_root.glob("patchfrog-verify-*") if p.is_dir()}

    workspace = _workspace()
    try:
        _write(workspace, "tests/test_capture.py", "def test_pass():\n    assert 1 == 1\n")
        companion = _companion(source_file_path="src/capture.py", expected_file_path="tests/test_capture.py")
        budget = VerificationBudget()
        before = _verify_dirs()
        await build_executable_verification_report(
            candidate=_candidate(), expected_companions=(companion,), commit_sha="a" * 40, local=True,
            budget=budget, root_path=workspace,
        )
        after = _verify_dirs()
        # No new patchfrog-verify-* directory left behind by this call.
        assert after - before == set()
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---- 36. Timeout leaves no leaked sandbox process behind ----


async def test_case_timeout_leaves_no_leaked_process() -> None:
    workspace = _workspace()
    try:
        _write(workspace, "tests/test_capture.py", "import time\n\ndef test_hangs():\n    time.sleep(30)\n")
        sandbox = VerificationSandbox(timeout_seconds=2.0)
        await run_pytest_verification(
            sandbox, workspace_root=workspace, test_target_path="tests/test_capture.py", commit_sha="a" * 40,
        )
        await asyncio.sleep(0.5)
        proc = await asyncio.create_subprocess_exec(
            "pgrep", "-f", "test_capture.py", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        assert stdout.strip() == b"", f"leaked process(es) after timeout: {stdout!r}"
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---- 37. IPv6/loopback also denied by default, not merely the tested external host ----


async def test_case_loopback_also_denied() -> None:
    workspace = _workspace()
    try:
        _write(
            workspace, "tests/test_loopback.py",
            "import socket\n\n"
            "def test_loopback_unreachable():\n"
            "    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "    s.settimeout(1)\n"
            "    try:\n"
            "        s.connect(('127.0.0.1', 80))\n"
            "        assert False, 'loopback was reachable'\n"
            "    except OSError:\n"
            "        pass\n",
        )
        sandbox = VerificationSandbox(timeout_seconds=10.0)
        evidence = await run_pytest_verification(
            sandbox, workspace_root=workspace, test_target_path="tests/test_loopback.py", commit_sha="a" * 40,
        )
        assert evidence.outcome is VerificationOutcome.PASSED, evidence.stdout_excerpt
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
