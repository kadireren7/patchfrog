"""Real, DB-backed + real-git corpus for Fix Verification (Milestone T,
T3). Uses a real local bare git remote (never a real GitHub network call)
via ``FixVerificationService``'s injectable ``clone_url_factory`` --
mirrors Milestone S6's own corpus tests' use of ``file://`` remotes with a
synthetic token. The GitHub installation-token exchange itself is mocked
with ``respx`` (real JWT signing, real HTTP call shape, no real network).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import redis
import respx
from celery import Celery
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.agent_handoff.service import AgentHandoffService
from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.config.settings import Settings
from patchfrog.executable_verification.dispatch import VerifierDispatcher
from patchfrog.executable_verification.sandbox import is_sandbox_available
from patchfrog.fix_verification.domain import FixAttemptStatus
from patchfrog.fix_verification.service import FixAttemptValidationError, FixVerificationService
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.models.analysis import AnalysisRunModel, AnalysisRunStatus, FindingModel
from patchfrog.persistence.models.repository import RepositoryModel
from patchfrog.persistence.models.review import (
    AIFindingModel,
    AIFindingProposalModel,
    ReviewCandidateModel,
    ReviewRunModel,
)
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.persistence.repositories.repository_index import RepositoryIndexRepository
from patchfrog.repository.git import run_git
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.domain import ProposalStatus, ReviewCandidateReason, ReviewRunStatus
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse
from tests.support.git_repo import commit_all

_API_BASE = "https://api.github.com"
_UNDEFINED_NAME_MODULE = "def f():\n    return undefined_name\n"
_FIXED_MODULE = "def f():\n    return 1\n"
_REDIS_URL = "redis://localhost:6379/0"
_WORKER_READY_TIMEOUT_SECONDS = 20.0

# A real, existing-targeted-test fixture: a source file with one function
# and a real pytest test file exercising it -- reused across the EV-signal
# tests below (Blocker 1's "PASS is supporting-only" / "CONFIRMED_FAILURE
# is strong contradiction" corpus).
_EV_SOURCE_BUGGY = "def add(a, b):\n    return a - b  # bug: should be a + b\n"
_EV_SOURCE_FIXED = "def add(a, b):\n    return a + b\n"
_EV_TEST = "from src import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def _redis_available() -> bool:
    try:
        return bool(redis.Redis.from_url(_REDIS_URL, socket_connect_timeout=2).ping())
    except (redis.RedisError, OSError):
        return False


_ev_infra_available = is_sandbox_available() and _redis_available()


def _mock_token_route() -> None:
    respx.post(f"{_API_BASE}/app/installations/1/access_tokens").mock(
        return_value=httpx.Response(
            201, json={"token": "ghs_synthetictoken", "expires_at": "2099-01-01T00:00:00Z"}
        )
    )


def _init_bare_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    run_git(["init", "--quiet", "--bare", str(remote)])
    # Pin HEAD to refs/heads/main regardless of this host's
    # init.defaultBranch -- otherwise a second, independent clone of this
    # remote (as every subsequent _push_commit call does) can land on an
    # unborn/mismatched default branch, turning its next commit into an
    # unrelated root commit and making the push a spurious non-fast-forward.
    run_git(["-C", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"])
    return remote


def _push_commit(remote: Path, tmp_path: Path, *, files: dict[str, str], message: str = "commit") -> str:
    work = tmp_path / f"work-{uuid.uuid4()}"
    if not work.exists():
        run_git(["clone", "--quiet", str(remote), str(work)])
        init_git_repo_config(work)
    for rel, content in files.items():
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return commit_and_push(work, remote, message)


def init_git_repo_config(root: Path) -> None:
    run_git(["-C", str(root), "config", "user.email", "t@example.com"])
    run_git(["-C", str(root), "config", "user.name", "T"])


def commit_and_push(work: Path, remote: Path, message: str) -> str:
    sha = commit_all(work, message)
    run_git(["-C", str(work), "push", "--quiet", "origin", "HEAD:refs/heads/main"])
    return sha


async def _make_repository(session_factory: async_sessionmaker[AsyncSession], full_name: str) -> RepositoryModel:
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name=full_name.split("/")[-1], full_name=full_name, installation_id=1,
        )
        await session.commit()
        return repo


async def _stage_finding(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository_id: uuid.UUID,
    commit_sha: str,
    file_path: str = "m.py",
    start_line: int = 2,
    end_line: int = 2,
    corroborated_by_static: bool = False,
    static_finding: FindingModel | None = None,
) -> uuid.UUID:
    async with session_factory() as session:
        run = ReviewRunModel(
            id=uuid.uuid4(), repository_id=repository_id, repository_index_id=uuid.uuid4(),
            commit_sha=commit_sha, config_fingerprint="c" * 64, model_fingerprint="m" * 64,
            incremental_context_fingerprint="i" * 64, status=ReviewRunStatus.SUCCEEDED,
            reviewer_provider="fake", reviewer_model="fake-model",
            started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()

        candidate = ReviewCandidateModel(
            id=uuid.uuid4(), review_run_id=run.id, file_path=file_path, symbol_id=None,
            symbol_name="f", qualified_name="f", start_line=start_line, end_line=end_line,
            changed_lines=json.dumps([start_line]), reason=ReviewCandidateReason.CHANGED_SYMBOL,
        )
        session.add(candidate)
        await session.flush()

        proposal = AIFindingProposalModel(
            id=uuid.uuid4(), review_run_id=run.id, candidate_id=candidate.id, title="undefined name",
            message="undefined_name is not defined", category=FindingCategory.CORRECTNESS,
            severity=Severity.HIGH, confidence=Confidence.HIGH, file_path=file_path,
            start_line=start_line, end_line=end_line, evidence="[]",
            reasoning_summary="referenced before assignment", status=ProposalStatus.ACCEPTED,
            agent_role=AgentRole.CORRECTNESS,
        )
        session.add(proposal)
        await session.flush()

        finding = AIFindingModel(
            id=uuid.uuid4(), review_run_id=run.id, proposal_id=proposal.id, candidate_id=candidate.id,
            title="undefined name", message="undefined_name is not defined", category=FindingCategory.CORRECTNESS,
            severity=Severity.HIGH, confidence=Confidence.HIGH, file_path=file_path,
            start_line=start_line, end_line=end_line, evidence="[]",
            reasoning_summary="referenced before assignment", corroborated_by_static=corroborated_by_static,
            static_finding_ids=json.dumps([str(static_finding.id)] if static_finding is not None else []),
            agent_role=AgentRole.CORRECTNESS,
        )
        session.add(finding)
        await session.flush()
        finding_id = finding.id
        await session.commit()
        return finding_id


async def _make_static_finding(
    session_factory: async_sessionmaker[AsyncSession], *, file_path: str, start_line: int, end_line: int
) -> FindingModel:
    async with session_factory() as session:
        analysis_run = AnalysisRunModel(
            id=uuid.uuid4(), repository_id=uuid.uuid4(), repository_index_id=uuid.uuid4(), commit_sha="a" * 40,
            config_fingerprint="cfg", toolchain_fingerprint="tc", status=AnalysisRunStatus.SUCCEEDED,
            started_at=datetime.now(UTC),
        )
        session.add(analysis_run)
        await session.flush()
        finding = FindingModel(
            id=uuid.uuid4(), analysis_run_id=analysis_run.id, fingerprint="f" * 32, rule_id="F821",
            category=FindingCategory.CORRECTNESS, title="undefined name", message="undefined_name",
            severity=Severity.HIGH, confidence=Confidence.HIGH, file_path=file_path, start_line=start_line,
            end_line=end_line, start_column=0, end_column=0, source_analyzer="ruff",
        )
        session.add(finding)
        await session.commit()
        await session.refresh(finding)
        return finding


def _service(
    *, remote: Path, verifier_dispatcher: object | None = None, fix_critic_provider: FakeLLMProvider | None = None
) -> FixVerificationService:
    return FixVerificationService(
        settings=Settings(),
        verifier_dispatcher=verifier_dispatcher,  # type: ignore[arg-type]
        fix_critic_provider=fix_critic_provider,
        clone_url_factory=lambda repository: f"file://{remote}",
    )


async def _build_handoff(session_factory: async_sessionmaker[AsyncSession], *, finding_id: uuid.UUID):  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        handoff, reason = await AgentHandoffService().build_handoff(session, finding_id=finding_id)
        assert handoff is not None, reason
        return handoff


# ---- Fix attempt creation / idempotency / validation (Part AR "FIX ATTEMPT") ----


@respx.mock
async def test_create_valid_fix_attempt(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> None:
    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    original_sha = _push_commit(remote, tmp_path, files={"m.py": _FIXED_MODULE, "README.md": "x"})
    candidate_sha = _push_commit(remote, tmp_path, files={"README.md": "y"})

    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(session_factory, repository_id=repository.id, commit_sha=original_sha)
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    async with session_factory() as session:
        attempt = await _service(remote=remote).start_fix_attempt(
            session, handoff=handoff, candidate_fix_commit_sha=candidate_sha
        )

    assert attempt.handoff_id == handoff.handoff_id
    assert attempt.original_commit_sha == original_sha
    assert attempt.candidate_fix_commit_sha == candidate_sha
    assert attempt.status in FixAttemptStatus  # a real terminal status was reached


@respx.mock
async def test_duplicate_same_candidate_sha_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    original_sha = _push_commit(remote, tmp_path, files={"m.py": _FIXED_MODULE})
    candidate_sha = _push_commit(remote, tmp_path, files={"README.md": "y"})

    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(session_factory, repository_id=repository.id, commit_sha=original_sha)
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    service = _service(remote=remote)
    async with session_factory() as session:
        first = await service.start_fix_attempt(session, handoff=handoff, candidate_fix_commit_sha=candidate_sha)
    async with session_factory() as session:
        second = await service.start_fix_attempt(session, handoff=handoff, candidate_fix_commit_sha=candidate_sha)

    assert first.fix_attempt_id == second.fix_attempt_id


@respx.mock
async def test_different_candidate_sha_creates_a_distinct_attempt(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    original_sha = _push_commit(remote, tmp_path, files={"m.py": _FIXED_MODULE})
    candidate_sha_1 = _push_commit(remote, tmp_path, files={"README.md": "y"})
    candidate_sha_2 = _push_commit(remote, tmp_path, files={"README.md": "z"})

    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(session_factory, repository_id=repository.id, commit_sha=original_sha)
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    service = _service(remote=remote)
    async with session_factory() as session:
        first = await service.start_fix_attempt(session, handoff=handoff, candidate_fix_commit_sha=candidate_sha_1)
    async with session_factory() as session:
        second = await service.start_fix_attempt(session, handoff=handoff, candidate_fix_commit_sha=candidate_sha_2)

    assert first.fix_attempt_id != second.fix_attempt_id


async def test_malformed_candidate_sha_rejected(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> None:
    remote = _init_bare_remote(tmp_path)
    original_sha = _push_commit(remote, tmp_path, files={"m.py": _FIXED_MODULE})
    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(session_factory, repository_id=repository.id, commit_sha=original_sha)
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    async with session_factory() as session:
        try:
            await _service(remote=remote).start_fix_attempt(
                session, handoff=handoff, candidate_fix_commit_sha="not-a-sha"
            )
            raise AssertionError("expected FixAttemptValidationError")
        except FixAttemptValidationError:
            pass


@respx.mock
async def test_get_fix_attempt_returns_completed_attempt(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    original_sha = _push_commit(remote, tmp_path, files={"m.py": _FIXED_MODULE})
    candidate_sha = _push_commit(remote, tmp_path, files={"README.md": "y"})

    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(session_factory, repository_id=repository.id, commit_sha=original_sha)
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    service = _service(remote=remote)
    async with session_factory() as session:
        created = await service.start_fix_attempt(session, handoff=handoff, candidate_fix_commit_sha=candidate_sha)
    async with session_factory() as session:
        fetched = await service.get_fix_attempt(session, fix_attempt_id=created.fix_attempt_id)

    assert fetched is not None
    assert fetched.fix_attempt_id == created.fix_attempt_id
    assert fetched.status == created.status


async def test_get_fix_attempt_returns_none_for_unknown_id(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    remote = _init_bare_remote(tmp_path)
    async with session_factory() as session:
        result = await _service(remote=remote).get_fix_attempt(session, fix_attempt_id=uuid.uuid4())
    assert result is None


# ---- Fix verification algorithm (Part AR "FIX VERIFICATION") ----


@respx.mock
async def test_unrelated_candidate_sha_is_stale(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> None:
    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    original_sha = _push_commit(remote, tmp_path, files={"m.py": _FIXED_MODULE})

    # An orphan branch shares no history with `original_sha` at all.
    work = tmp_path / f"work-{uuid.uuid4()}"
    run_git(["clone", "--quiet", str(remote), str(work)])
    init_git_repo_config(work)
    run_git(["-C", str(work), "checkout", "--orphan", "unrelated"])
    (work / "other.py").write_text("x = 1\n")
    unrelated_sha = commit_and_push_branch(work, "unrelated")

    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(session_factory, repository_id=repository.id, commit_sha=original_sha)
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    async with session_factory() as session:
        attempt = await _service(remote=remote).start_fix_attempt(
            session, handoff=handoff, candidate_fix_commit_sha=unrelated_sha
        )

    assert attempt.status == FixAttemptStatus.STALE


def commit_and_push_branch(work: Path, branch: str) -> str:
    run_git(["-C", str(work), "add", "-A"])
    run_git(["-C", str(work), "commit", "--quiet", "-m", "unrelated"])
    run_git(["-C", str(work), "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"])
    return run_git(["-C", str(work), "rev-parse", "HEAD"]).strip()


@respx.mock
async def test_identical_sha_comparison_unchanged_alone_is_inconclusive(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Part U: an explicit no-op comparison (same SHA) is allowed, not
    rejected. Security correction round 2, Blocker 2: "nothing changed" is
    only ever weak SUPPORTS_PRESENT evidence -- with no provider
    configured to weigh it, the honest result is INCONCLUSIVE, never an
    automatic STILL_PRESENT (the original defect could, in principle,
    have been resolved by context entirely outside this exact surface)."""

    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    original_sha = _push_commit(remote, tmp_path, files={"m.py": _UNDEFINED_NAME_MODULE})

    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(session_factory, repository_id=repository.id, commit_sha=original_sha)
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    async with session_factory() as session:
        attempt = await _service(remote=remote, fix_critic_provider=None).start_fix_attempt(
            session, handoff=handoff, candidate_fix_commit_sha=original_sha
        )

    assert attempt.status == FixAttemptStatus.INCONCLUSIVE
    assert attempt.result is not None
    assert any("supporting evidence only" in e for e in attempt.result.deterministic_evidence)


@respx.mock
async def test_unchanged_flagged_code_plus_llm_confirms_still_present(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """When a provider *is* configured, unchanged weak evidence still only
    ever unlocks the LLM fallback -- the model's own (here, scripted)
    judgment is what actually produces STILL_PRESENT, not the unchanged
    bytes by themselves."""

    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    original_sha = _push_commit(remote, tmp_path, files={"m.py": _UNDEFINED_NAME_MODULE})

    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(session_factory, repository_id=repository.id, commit_sha=original_sha)
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    provider = FakeLLMProvider(
        [ScriptedResponse(raw_json=json.dumps({"decision": "still_present", "reasoning_summary": "still buggy"}))]
    )
    async with session_factory() as session:
        attempt = await _service(remote=remote, fix_critic_provider=provider).start_fix_attempt(
            session, handoff=handoff, candidate_fix_commit_sha=original_sha
        )

    assert attempt.status == FixAttemptStatus.STILL_PRESENT
    assert len(provider.calls) == 1


@respx.mock
async def test_unchanged_bytes_but_llm_finds_it_resolved_externally_is_fixed(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Part V of the security correction: "the flagged code is unchanged"
    must never force STILL_PRESENT -- the underlying defect can, in
    principle, be resolved entirely by context outside the exact flagged
    surface. This test proves the algorithm does not hardcode "unchanged
    -> present": with a provider configured, the (scripted) model's own
    judgment can still land on FIXED even though the bytes never changed."""

    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    original_sha = _push_commit(remote, tmp_path, files={"m.py": _UNDEFINED_NAME_MODULE})

    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(session_factory, repository_id=repository.id, commit_sha=original_sha)
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    provider = FakeLLMProvider(
        [ScriptedResponse(raw_json=json.dumps({"decision": "fixed", "reasoning_summary": "caller now validates"}))]
    )
    async with session_factory() as session:
        attempt = await _service(remote=remote, fix_critic_provider=provider).start_fix_attempt(
            session, handoff=handoff, candidate_fix_commit_sha=original_sha
        )

    assert attempt.status == FixAttemptStatus.FIXED


@respx.mock
async def test_unrelated_file_change_leaves_flagged_file_unchanged_inconclusive(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """"Caller validation added elsewhere" analogue: the flagged file's
    own bytes never changed, but *something else in the repository* did
    -- weak evidence only, never an automatic verdict."""

    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    original_sha = _push_commit(remote, tmp_path, files={"m.py": _UNDEFINED_NAME_MODULE, "other.py": "a = 1\n"})
    candidate_sha = _push_commit(remote, tmp_path, files={"other.py": "a = 2\n"})

    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(session_factory, repository_id=repository.id, commit_sha=original_sha)
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    async with session_factory() as session:
        attempt = await _service(remote=remote, fix_critic_provider=None).start_fix_attempt(
            session, handoff=handoff, candidate_fix_commit_sha=candidate_sha
        )

    assert attempt.status == FixAttemptStatus.INCONCLUSIVE


@respx.mock
async def test_static_recheck_absence_alone_is_never_sufficient_for_fixed(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Security correction, Blocker 2: a static rule no longer firing at
    the safely mapped surface is supporting evidence only -- it must
    never, by itself (no fallback provider configured), produce FIXED."""

    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    original_sha = _push_commit(remote, tmp_path, files={"m.py": _UNDEFINED_NAME_MODULE})
    candidate_sha = _push_commit(remote, tmp_path, files={"m.py": _FIXED_MODULE})

    static_finding = await _make_static_finding(session_factory, file_path="m.py", start_line=2, end_line=2)
    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(
        session_factory, repository_id=repository.id, commit_sha=original_sha,
        corroborated_by_static=True, static_finding=static_finding,
    )
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    async with session_factory() as session:
        attempt = await _service(remote=remote, fix_critic_provider=None).start_fix_attempt(
            session, handoff=handoff, candidate_fix_commit_sha=candidate_sha
        )

    assert attempt.status == FixAttemptStatus.INCONCLUSIVE
    assert attempt.result is not None
    assert any("supporting evidence only" in e for e in attempt.result.deterministic_evidence)
    assert any("no fix-verification provider configured" in lim for lim in attempt.result.limitations)


@respx.mock
async def test_static_recheck_absence_plus_llm_confirmation_is_fixed(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """The only way a statically-corroborated finding reaches FIXED in v1:
    the (weak) static-absence signal makes the bounded LLM fallback
    available, and the model -- shown the actual, safely mapped current
    code -- confirms resolution."""

    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    original_sha = _push_commit(remote, tmp_path, files={"m.py": _UNDEFINED_NAME_MODULE})
    candidate_sha = _push_commit(remote, tmp_path, files={"m.py": _FIXED_MODULE})

    static_finding = await _make_static_finding(session_factory, file_path="m.py", start_line=2, end_line=2)
    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(
        session_factory, repository_id=repository.id, commit_sha=original_sha,
        corroborated_by_static=True, static_finding=static_finding,
    )
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    provider = FakeLLMProvider(
        [ScriptedResponse(raw_json=json.dumps({"decision": "fixed", "reasoning_summary": "no longer undefined"}))]
    )
    async with session_factory() as session:
        attempt = await _service(remote=remote, fix_critic_provider=provider).start_fix_attempt(
            session, handoff=handoff, candidate_fix_commit_sha=candidate_sha
        )

    assert attempt.status == FixAttemptStatus.FIXED
    assert len(provider.calls) == 1
    # The model was shown the real, mapped current code -- not the
    # original (now-stale) location.
    assert "return 1" in provider.calls[0].user_prompt


@respx.mock
async def test_static_recheck_confirms_still_present(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    original_sha = _push_commit(remote, tmp_path, files={"m.py": _UNDEFINED_NAME_MODULE})
    # Changed, but the rule still fires -- a cosmetic edit, not a real fix.
    candidate_sha = _push_commit(
        remote, tmp_path, files={"m.py": "def f():\n    return undefined_name  # still broken\n"}
    )

    static_finding = await _make_static_finding(session_factory, file_path="m.py", start_line=2, end_line=2)
    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(
        session_factory, repository_id=repository.id, commit_sha=original_sha,
        corroborated_by_static=True, static_finding=static_finding,
    )
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    async with session_factory() as session:
        attempt = await _service(remote=remote).start_fix_attempt(
            session, handoff=handoff, candidate_fix_commit_sha=candidate_sha
        )

    assert attempt.status == FixAttemptStatus.STILL_PRESENT


@respx.mock
async def test_no_deterministic_signal_and_no_provider_is_inconclusive(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    original_sha = _push_commit(remote, tmp_path, files={"m.py": "a = 1\nb = 2\n"})
    candidate_sha = _push_commit(remote, tmp_path, files={"m.py": "a = 1\nb = 3\n"})

    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(
        session_factory, repository_id=repository.id, commit_sha=original_sha, start_line=2, end_line=2,
    )
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    async with session_factory() as session:
        attempt = await _service(remote=remote, fix_critic_provider=None).start_fix_attempt(
            session, handoff=handoff, candidate_fix_commit_sha=candidate_sha
        )

    assert attempt.status == FixAttemptStatus.INCONCLUSIVE
    assert attempt.result is not None
    assert any("no fix-verification provider configured" in lim for lim in attempt.result.limitations)


@respx.mock
async def test_critic_fallback_used_only_when_no_deterministic_signal(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    original = "def f():\n    a = 1\n    b = 2\n    return a + b\n"
    candidate = "def f():\n    a = 1\n    b = 3\n    return a + b\n"
    original_sha = _push_commit(remote, tmp_path, files={"m.py": original})
    candidate_sha = _push_commit(remote, tmp_path, files={"m.py": candidate})

    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(
        session_factory, repository_id=repository.id, commit_sha=original_sha, start_line=1, end_line=4,
    )
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    provider = FakeLLMProvider(
        [ScriptedResponse(raw_json=json.dumps({"decision": "fixed", "reasoning_summary": "value corrected"}))]
    )
    async with session_factory() as session:
        attempt = await _service(remote=remote, fix_critic_provider=provider).start_fix_attempt(
            session, handoff=handoff, candidate_fix_commit_sha=candidate_sha
        )

    assert attempt.status == FixAttemptStatus.FIXED
    assert len(provider.calls) == 1


@respx.mock
async def test_no_provider_call_when_static_signal_strongly_contradicts_fixed(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Part X: deterministic-first -- a configured critic provider must
    never be called when a strong, contradicting deterministic signal
    (the rule still firing at the mapped surface) already decided the
    outcome."""

    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    original_sha = _push_commit(remote, tmp_path, files={"m.py": _UNDEFINED_NAME_MODULE})
    # Changed, but the exact same bug is still there -- a cosmetic edit.
    candidate_sha = _push_commit(
        remote, tmp_path, files={"m.py": "def f():\n    return undefined_name  # still broken\n"}
    )

    static_finding = await _make_static_finding(session_factory, file_path="m.py", start_line=2, end_line=2)
    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(
        session_factory, repository_id=repository.id, commit_sha=original_sha,
        corroborated_by_static=True, static_finding=static_finding,
    )
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    provider = FakeLLMProvider([])  # exhausted -- raises if ever called
    async with session_factory() as session:
        attempt = await _service(remote=remote, fix_critic_provider=provider).start_fix_attempt(
            session, handoff=handoff, candidate_fix_commit_sha=candidate_sha
        )

    assert attempt.status == FixAttemptStatus.STILL_PRESENT
    assert provider.calls == []


@respx.mock
async def test_result_always_documents_the_no_original_sha_reexecution_limitation(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    original_sha = _push_commit(remote, tmp_path, files={"m.py": _UNDEFINED_NAME_MODULE})
    candidate_sha = _push_commit(remote, tmp_path, files={"m.py": _FIXED_MODULE})

    static_finding = await _make_static_finding(session_factory, file_path="m.py", start_line=2, end_line=2)
    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(
        session_factory, repository_id=repository.id, commit_sha=original_sha,
        corroborated_by_static=True, static_finding=static_finding,
    )
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    async with session_factory() as session:
        attempt = await _service(remote=remote).start_fix_attempt(
            session, handoff=handoff, candidate_fix_commit_sha=candidate_sha
        )

    assert attempt.result is not None
    assert any("original commit" in lim for lim in attempt.result.limitations)


# ---- Move/rename/delete surface-mapping regressions (no false FIXED) ----


@respx.mock
async def test_static_rule_moved_within_file_still_fires_not_fixed(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Blocker 2: the buggy symbol moved ~10 lines away (well outside any
    fixed line-number window) but the exact same rule still fires there --
    content-hash-based surface mapping must still find it and report
    STILL_PRESENT, never FIXED merely because the original line numbers
    no longer contain it."""

    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    original_sha = _push_commit(remote, tmp_path, files={"m.py": _UNDEFINED_NAME_MODULE})
    padding = "\n".join(f"x{i} = {i}" for i in range(10))
    candidate_sha = _push_commit(remote, tmp_path, files={"m.py": f"{padding}\n\n\n{_UNDEFINED_NAME_MODULE}"})

    static_finding = await _make_static_finding(session_factory, file_path="m.py", start_line=2, end_line=2)
    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(
        session_factory, repository_id=repository.id, commit_sha=original_sha,
        corroborated_by_static=True, static_finding=static_finding,
    )
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    provider = FakeLLMProvider([])  # exhausted -- must never be called
    async with session_factory() as session:
        attempt = await _service(remote=remote, fix_critic_provider=provider).start_fix_attempt(
            session, handoff=handoff, candidate_fix_commit_sha=candidate_sha
        )

    assert attempt.status == FixAttemptStatus.STILL_PRESENT
    assert provider.calls == []


@respx.mock
async def test_original_file_deleted_moved_elsewhere_is_inconclusive_not_fixed(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """The buggy function moved to a *different file* -- out of scope for
    this milestone's same-file-only mapping (see
    ``validation/agent_handoff/latest-summary.md``). Must never guess
    FIXED just because the original file is gone."""

    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    original_sha = _push_commit(remote, tmp_path, files={"m.py": _UNDEFINED_NAME_MODULE})
    work = tmp_path / f"work-{uuid.uuid4()}"
    run_git(["clone", "--quiet", str(remote), str(work)])
    init_git_repo_config(work)
    (work / "m.py").unlink()
    (work / "other.py").write_text(_UNDEFINED_NAME_MODULE)
    candidate_sha = commit_and_push(work, remote, "move to other.py")

    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(session_factory, repository_id=repository.id, commit_sha=original_sha)
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    provider = FakeLLMProvider([])  # exhausted -- must never be called
    async with session_factory() as session:
        attempt = await _service(remote=remote, fix_critic_provider=provider).start_fix_attempt(
            session, handoff=handoff, candidate_fix_commit_sha=candidate_sha
        )

    assert attempt.status == FixAttemptStatus.INCONCLUSIVE
    assert provider.calls == []


@respx.mock
async def test_original_file_deleted_with_no_replacement_is_inconclusive(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    original_sha = _push_commit(remote, tmp_path, files={"m.py": _UNDEFINED_NAME_MODULE, "keep.py": "x = 1\n"})
    work = tmp_path / f"work-{uuid.uuid4()}"
    run_git(["clone", "--quiet", str(remote), str(work)])
    init_git_repo_config(work)
    (work / "m.py").unlink()
    candidate_sha = commit_and_push(work, remote, "delete m.py")

    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(session_factory, repository_id=repository.id, commit_sha=original_sha)
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    async with session_factory() as session:
        attempt = await _service(remote=remote).start_fix_attempt(
            session, handoff=handoff, candidate_fix_commit_sha=candidate_sha
        )

    assert attempt.status == FixAttemptStatus.INCONCLUSIVE


@respx.mock
async def test_file_renamed_is_inconclusive_not_falsely_fixed(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """A pure git rename (same content, new path) -- surface mapping is
    scoped to the original file path only (never a false FIXED)."""

    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    original_sha = _push_commit(remote, tmp_path, files={"m.py": _UNDEFINED_NAME_MODULE})
    work = tmp_path / f"work-{uuid.uuid4()}"
    run_git(["clone", "--quiet", str(remote), str(work)])
    init_git_repo_config(work)
    run_git(["-C", str(work), "mv", "m.py", "renamed.py"])
    candidate_sha = commit_and_push(work, remote, "rename m.py to renamed.py")

    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(session_factory, repository_id=repository.id, commit_sha=original_sha)
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    provider = FakeLLMProvider([])  # exhausted -- must never be called
    async with session_factory() as session:
        attempt = await _service(remote=remote, fix_critic_provider=provider).start_fix_attempt(
            session, handoff=handoff, candidate_fix_commit_sha=candidate_sha
        )

    assert attempt.status == FixAttemptStatus.INCONCLUSIVE
    assert provider.calls == []


@respx.mock
async def test_symbol_moved_within_same_file_still_present_not_falsely_fixed(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """A method moved from one class to another (a real qualified-name
    change, mapped via identical body content-hash) but the bug is still
    there -- must resolve STILL_PRESENT, never FIXED."""

    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    original = "class A:\n    def method(self):\n        return undefined_name\n"
    candidate = (
        "class B:\n    def method(self):\n        return undefined_name\n\n\n"
        "class A:\n    def other(self):\n        return 1\n"
    )
    original_sha = _push_commit(remote, tmp_path, files={"m.py": original})
    candidate_sha = _push_commit(remote, tmp_path, files={"m.py": candidate})

    # A real static rule at the ORIGINAL location -- the deterministic
    # signal this test actually needs (without it, nothing but the LLM
    # fallback could ever know the bug moved rather than vanished, which
    # is a separate, weaker case already covered elsewhere).
    static_finding = await _make_static_finding(session_factory, file_path="m.py", start_line=2, end_line=3)
    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(
        session_factory, repository_id=repository.id, commit_sha=original_sha, start_line=2, end_line=3,
        corroborated_by_static=True, static_finding=static_finding,
    )
    # Override the candidate's qualified_name to match the class-based fixture.
    async with session_factory() as session:
        finding = await session.get(AIFindingModel, finding_id)
        assert finding is not None
        candidate_model = await session.get(ReviewCandidateModel, finding.candidate_id)
        assert candidate_model is not None
        candidate_model.qualified_name = "A.method"
        candidate_model.symbol_name = "method"
        await session.commit()
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    provider = FakeLLMProvider([])  # exhausted -- must never be called
    async with session_factory() as session:
        attempt = await _service(remote=remote, fix_critic_provider=provider).start_fix_attempt(
            session, handoff=handoff, candidate_fix_commit_sha=candidate_sha
        )

    assert attempt.status == FixAttemptStatus.STILL_PRESENT
    assert provider.calls == []


@respx.mock
async def test_unmapped_surface_never_asks_the_llm(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Blocker 2 / LLM fallback gate: when the original symbol cannot be
    safely mapped to the candidate head, the fallback model must never be
    invoked at all -- not invoked-and-ignored, never invoked."""

    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    original_sha = _push_commit(remote, tmp_path, files={"m.py": _UNDEFINED_NAME_MODULE})
    candidate_sha = _push_commit(remote, tmp_path, files={"m.py": "def totally_different():\n    return 42\n"})

    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(session_factory, repository_id=repository.id, commit_sha=original_sha)
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    provider = FakeLLMProvider([])  # exhausted -- raises if ever called
    async with session_factory() as session:
        attempt = await _service(remote=remote, fix_critic_provider=provider).start_fix_attempt(
            session, handoff=handoff, candidate_fix_commit_sha=candidate_sha
        )

    assert attempt.status == FixAttemptStatus.INCONCLUSIVE
    assert provider.calls == []
    assert attempt.result is not None
    assert any("could not be safely mapped" in lim for lim in attempt.result.limitations)


async def test_analyzer_unavailable_is_inconclusive_not_fixed(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    with respx.mock:
        _mock_token_route()
        remote = _init_bare_remote(tmp_path)
        original_sha = _push_commit(remote, tmp_path, files={"m.py": _UNDEFINED_NAME_MODULE})
        candidate_sha = _push_commit(remote, tmp_path, files={"m.py": _FIXED_MODULE})

        static_finding = await _make_static_finding(
            session_factory, file_path="m.py", start_line=2, end_line=2,
        )
        async with session_factory() as session:
            model = await session.get(FindingModel, static_finding.id)
            assert model is not None
            model.source_analyzer = "not_a_real_analyzer"
            await session.commit()

        repository = await _make_repository(session_factory, "acme/widgets")
        finding_id = await _stage_finding(
            session_factory, repository_id=repository.id, commit_sha=original_sha,
            corroborated_by_static=True, static_finding=static_finding,
        )
        handoff = await _build_handoff(session_factory, finding_id=finding_id)

        async with session_factory() as session:
            attempt = await _service(remote=remote, fix_critic_provider=None).start_fix_attempt(
                session, handoff=handoff, candidate_fix_commit_sha=candidate_sha
            )

    assert attempt.status == FixAttemptStatus.INCONCLUSIVE


# ---- Executable Verification signal strength (Blocker 1) -- real S6 verifier ----


@pytest.fixture(scope="module")
def staging_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("fix-verification-staging-root")


@pytest.fixture(scope="module")
def verifier_worker(staging_root: Path) -> Iterator[None]:
    """A real, separate `celery -A apps.verifier.celery_app worker`
    subprocess -- see tests/integration/test_production_execution_corpus.py's
    own identical fixture. Skipped entirely (module-level) when the
    sandbox/Redis this needs isn't available on this host."""

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "celery", "-A", "apps.verifier.celery_app", "worker",
            "-Q", "patchfrog-verification", "--loglevel=INFO", "--concurrency=1",
        ],
        env={"REDIS_URL": _REDIS_URL, "VERIFIER_STAGING_ROOT": str(staging_root), "PATH": os.environ.get("PATH", "")},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.monotonic() + _WORKER_READY_TIMEOUT_SECONDS
        ready = False
        assert proc.stdout is not None
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            if "ready." in line:
                ready = True
                break
        if not ready:
            proc.terminate()
            raise RuntimeError("verifier worker subprocess did not become ready in time")
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _ev_producer_app() -> Celery:
    return Celery("patchfrog-test-producer", broker=_REDIS_URL, backend=_REDIS_URL)


async def _stage_ev_finding(
    session_factory: async_sessionmaker[AsyncSession], *, tmp_path: Path, remote: Path, source: str,
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Real repository indexing (so a real FILE_TESTS_FILE edge exists,
    reused by FixVerificationService._known_test_companions -- never a
    hand-built companion). Returns (repository_id, finding_id, original_sha)."""

    work = tmp_path / f"ev-work-{uuid.uuid4()}"
    run_git(["clone", "--quiet", str(remote), str(work)])
    init_git_repo_config(work)
    (work / "src.py").write_text(source)
    (work / "test_src.py").write_text(_EV_TEST)
    original_sha = commit_and_push(work, remote, "add src + test")

    repository = await _make_repository(session_factory, "acme/widgets")
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository.id, root_path=work, repository_full_name=repository.full_name,
    )

    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository.id)
        assert index is not None

    finding_id = await _stage_finding(
        session_factory, repository_id=repository.id, commit_sha=original_sha,
        file_path="src.py", start_line=1, end_line=2,
    )

    async with session_factory() as session:
        finding = await session.get(AIFindingModel, finding_id)
        assert finding is not None
        run = await session.get(ReviewRunModel, finding.review_run_id)
        assert run is not None
        run.repository_index_id = index.id
        candidate = await session.get(ReviewCandidateModel, finding.candidate_id)
        assert candidate is not None
        candidate.qualified_name = "add"
        candidate.symbol_name = "add"
        await session.commit()

    return repository.id, finding_id, original_sha


@pytest.mark.skipif(not _ev_infra_available, reason="bwrap sandbox and/or Redis not available on this host")
@respx.mock
async def test_executable_verification_pass_alone_is_inconclusive_not_fixed(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path, staging_root: Path,
    verifier_worker: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocker 1: a single passing targeted test is supporting evidence
    only -- with no other strong signal, and no fallback provider, this
    must be INCONCLUSIVE, never FIXED."""

    monkeypatch.setenv("VERIFICATION_SNAPSHOT_ROOT", str(staging_root))
    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    _repository_id, finding_id, _original_sha = await _stage_ev_finding(
        session_factory, tmp_path=tmp_path, remote=remote, source=_EV_SOURCE_BUGGY,
    )
    candidate_sha = _push_commit(remote, tmp_path, files={"src.py": _EV_SOURCE_FIXED})
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    dispatcher = VerifierDispatcher(celery_app=_ev_producer_app(), wait_timeout_seconds=20.0)
    async with session_factory() as session:
        attempt = await _service(
            remote=remote, verifier_dispatcher=dispatcher, fix_critic_provider=None,
        ).start_fix_attempt(session, handoff=handoff, candidate_fix_commit_sha=candidate_sha)

    assert attempt.status == FixAttemptStatus.INCONCLUSIVE
    assert attempt.result is not None
    assert attempt.result.executable_verification_outcome == "passed"
    assert any("supporting evidence only" in e for e in attempt.result.deterministic_evidence)


@pytest.mark.skipif(not _ev_infra_available, reason="bwrap sandbox and/or Redis not available on this host")
@respx.mock
async def test_executable_verification_confirmed_failure_alone_is_inconclusive(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path, staging_root: Path,
    verifier_worker: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Security correction round 2, Blocker 1: a candidate-head test
    failure is *not* durable, finding-specific proof the *original*
    finding is still present -- original-SHA EV evidence is never
    persisted, the candidate-head test target is reconstructed (not a
    persisted original binding), and a test file can fail for reasons
    unrelated to this specific finding (an unrelated regression, a setup/
    environment difference, a different assertion). With no provider
    configured to weigh this weak evidence, the honest result is
    INCONCLUSIVE, never an automatic STILL_PRESENT."""

    monkeypatch.setenv("VERIFICATION_SNAPSHOT_ROOT", str(staging_root))
    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    _repository_id, finding_id, _original_sha = await _stage_ev_finding(
        session_factory, tmp_path=tmp_path, remote=remote, source=_EV_SOURCE_BUGGY,
    )
    # A cosmetic edit *inside* the buggy function itself -- the exact bug
    # remains, but the function's own body is no longer byte-identical
    # (a trailing comment *outside* the function would leave the mapped
    # symbol itself UNCHANGED, which is now also only weak evidence, but
    # this test wants to isolate the EV signal specifically).
    candidate_sha = _push_commit(
        remote, tmp_path,
        files={"src.py": "def add(a, b):\n    # unrelated cosmetic comment\n    return a - b  # still buggy\n"},
    )
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    dispatcher = VerifierDispatcher(celery_app=_ev_producer_app(), wait_timeout_seconds=20.0)
    async with session_factory() as session:
        attempt = await _service(
            remote=remote, verifier_dispatcher=dispatcher, fix_critic_provider=None,
        ).start_fix_attempt(session, handoff=handoff, candidate_fix_commit_sha=candidate_sha)

    assert attempt.status == FixAttemptStatus.INCONCLUSIVE
    assert attempt.result is not None
    assert attempt.result.executable_verification_outcome == "confirmed_failure"
    assert any("supporting evidence only" in e for e in attempt.result.deterministic_evidence)
    assert any("not proof this is the original finding" in e for e in attempt.result.deterministic_evidence)


@respx.mock
async def test_executable_verification_confirmed_failure_plus_llm_confirms_still_present(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path, staging_root: Path,
    verifier_worker: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a provider configured, the weak EV-failure signal unlocks the
    LLM fallback -- the model's own judgment (here, scripted) is what
    actually produces STILL_PRESENT, never the test failure by itself."""

    monkeypatch.setenv("VERIFICATION_SNAPSHOT_ROOT", str(staging_root))
    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    _repository_id, finding_id, _original_sha = await _stage_ev_finding(
        session_factory, tmp_path=tmp_path, remote=remote, source=_EV_SOURCE_BUGGY,
    )
    candidate_sha = _push_commit(
        remote, tmp_path,
        files={"src.py": "def add(a, b):\n    # unrelated cosmetic comment\n    return a - b  # still buggy\n"},
    )
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    dispatcher = VerifierDispatcher(celery_app=_ev_producer_app(), wait_timeout_seconds=20.0)
    provider = FakeLLMProvider(
        [ScriptedResponse(raw_json=json.dumps({"decision": "still_present", "reasoning_summary": "confirmed"}))]
    )
    async with session_factory() as session:
        attempt = await _service(
            remote=remote, verifier_dispatcher=dispatcher, fix_critic_provider=provider,
        ).start_fix_attempt(session, handoff=handoff, candidate_fix_commit_sha=candidate_sha)

    assert attempt.status == FixAttemptStatus.STILL_PRESENT
    assert len(provider.calls) == 1


@respx.mock
async def test_original_finding_remains_but_candidate_test_happens_to_pass_is_not_fixed(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path, staging_root: Path,
    verifier_worker: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mirror image of the round-1 correction: a passing candidate-
    head test is weak SUPPORTS_RESOLVED evidence, and with a provider
    that (correctly) still finds the original condition present, the
    result must be STILL_PRESENT -- never forced to FIXED merely because
    one test happened to pass."""

    monkeypatch.setenv("VERIFICATION_SNAPSHOT_ROOT", str(staging_root))
    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    _repository_id, finding_id, _original_sha = await _stage_ev_finding(
        session_factory, tmp_path=tmp_path, remote=remote, source=_EV_SOURCE_BUGGY,
    )
    # The targeted test happens to pass (e.g. it doesn't exercise the
    # exact regressed input), but the underlying bug is still there.
    candidate_sha = _push_commit(
        remote, tmp_path, files={"src.py": "def add(a, b):\n    if a == 2 and b == 3:\n        return 5\n"
                                            "    return a - b  # still buggy for other inputs\n"},
    )
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    dispatcher = VerifierDispatcher(celery_app=_ev_producer_app(), wait_timeout_seconds=20.0)
    provider = FakeLLMProvider(
        [ScriptedResponse(raw_json=json.dumps({"decision": "still_present", "reasoning_summary": "only patched for one case"}))]
    )
    async with session_factory() as session:
        attempt = await _service(
            remote=remote, verifier_dispatcher=dispatcher, fix_critic_provider=provider,
        ).start_fix_attempt(session, handoff=handoff, candidate_fix_commit_sha=candidate_sha)

    assert attempt.status == FixAttemptStatus.STILL_PRESENT
    assert attempt.result is not None
    assert attempt.result.executable_verification_outcome == "passed"


@respx.mock
async def test_weak_present_and_weak_resolved_conflict_forces_no_terminal_verdict(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path, staging_root: Path,
    verifier_worker: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two weak signals pointing in opposite directions at once (the
    flagged bytes are unchanged -- SUPPORTS_PRESENT -- while an
    insufficiently targeted test happens to pass anyway -- SUPPORTS_RESOLVED)
    must never be resolved into a forced terminal verdict by the
    combination logic itself. With no provider configured, the only
    honest result is INCONCLUSIVE."""

    monkeypatch.setenv("VERIFICATION_SNAPSHOT_ROOT", str(staging_root))
    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    # A weak test: both 0-0 and 0+0 equal 0, so it passes whether or not
    # the subtraction-instead-of-addition bug is present.
    weak_test = "from src import add\n\n\ndef test_add():\n    assert add(0, 0) == 0\n"
    work = tmp_path / f"ev-work-{uuid.uuid4()}"
    run_git(["clone", "--quiet", str(remote), str(work)])
    init_git_repo_config(work)
    (work / "src.py").write_text(_EV_SOURCE_BUGGY)
    (work / "test_src.py").write_text(weak_test)
    original_sha = commit_and_push(work, remote, "add src + weak test")

    repository = await _make_repository(session_factory, "acme/widgets")
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository.id, root_path=work, repository_full_name=repository.full_name,
    )
    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository.id)
        assert index is not None
    finding_id = await _stage_finding(
        session_factory, repository_id=repository.id, commit_sha=original_sha,
        file_path="src.py", start_line=1, end_line=2,
    )
    async with session_factory() as session:
        finding = await session.get(AIFindingModel, finding_id)
        assert finding is not None
        run = await session.get(ReviewRunModel, finding.review_run_id)
        assert run is not None
        run.repository_index_id = index.id
        candidate = await session.get(ReviewCandidateModel, finding.candidate_id)
        assert candidate is not None
        candidate.qualified_name = "add"
        candidate.symbol_name = "add"
        await session.commit()
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    # candidate SHA == original SHA: the flagged bytes are unchanged
    # (SUPPORTS_PRESENT), while the weak test still passes (SUPPORTS_RESOLVED).
    dispatcher = VerifierDispatcher(celery_app=_ev_producer_app(), wait_timeout_seconds=20.0)
    async with session_factory() as session:
        attempt = await _service(
            remote=remote, verifier_dispatcher=dispatcher, fix_critic_provider=None,
        ).start_fix_attempt(session, handoff=handoff, candidate_fix_commit_sha=original_sha)

    assert attempt.status == FixAttemptStatus.INCONCLUSIVE
    assert attempt.result is not None
    assert attempt.result.executable_verification_outcome == "passed"
    assert any("supporting evidence only" in e for e in attempt.result.deterministic_evidence)


@respx.mock
async def test_llm_decides_inconclusive_when_context_is_insufficient(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """A safely mapped surface does not guarantee the LLM fallback can
    actually decide -- when it says inconclusive (e.g. because the shown
    excerpt alone cannot establish resolution), the fix attempt honestly
    reports INCONCLUSIVE, never forced to a terminal verdict."""

    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    original = "def f():\n    a = 1\n    b = 2\n    return a + b\n"
    candidate = "def f():\n    a = 1\n    b = 3\n    return a + b\n"
    original_sha = _push_commit(remote, tmp_path, files={"m.py": original})
    candidate_sha = _push_commit(remote, tmp_path, files={"m.py": candidate})

    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(
        session_factory, repository_id=repository.id, commit_sha=original_sha, start_line=1, end_line=4,
    )
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    provider = FakeLLMProvider(
        [ScriptedResponse(raw_json=json.dumps({
            "decision": "inconclusive", "reasoning_summary": "cannot verify caller context",
        }))]
    )
    async with session_factory() as session:
        attempt = await _service(remote=remote, fix_critic_provider=provider).start_fix_attempt(
            session, handoff=handoff, candidate_fix_commit_sha=candidate_sha
        )

    assert attempt.status == FixAttemptStatus.INCONCLUSIVE
    assert len(provider.calls) == 1


def test_no_code_path_produces_proves_resolved_in_v1() -> None:
    """Security correction (both rounds): no static or Executable-
    Verification signal is strong enough to reach PROVES_RESOLVED in v1
    -- a deterministic FIXED can never occur by accident. Structural proof
    over the actual source rather than an exhaustive behavioral
    enumeration: neither _static_signal nor _ev_signal's source ever
    references FixEvidenceDirection.PROVES_RESOLVED."""

    import inspect

    from patchfrog.fix_verification.service import FixVerificationService

    static_source = inspect.getsource(FixVerificationService._static_signal)
    ev_source = inspect.getsource(FixVerificationService._ev_signal)
    assert "PROVES_RESOLVED" not in static_source
    assert "PROVES_RESOLVED" not in ev_source
