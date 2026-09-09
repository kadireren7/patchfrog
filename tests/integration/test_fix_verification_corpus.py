"""Real, DB-backed + real-git corpus for Fix Verification (Milestone T,
T3). Uses a real local bare git remote (never a real GitHub network call)
via ``FixVerificationService``'s injectable ``clone_url_factory`` --
mirrors Milestone S6's own corpus tests' use of ``file://`` remotes with a
synthetic token. The GitHub installation-token exchange itself is mocked
with ``respx`` (real JWT signing, real HTTP call shape, no real network).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import respx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.agent_handoff.service import AgentHandoffService
from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.config.settings import Settings
from patchfrog.fix_verification.domain import FixAttemptStatus
from patchfrog.fix_verification.service import FixAttemptValidationError, FixVerificationService
from patchfrog.persistence.models.analysis import AnalysisRunModel, AnalysisRunStatus, FindingModel
from patchfrog.persistence.models.repository import RepositoryModel
from patchfrog.persistence.models.review import (
    AIFindingModel,
    AIFindingProposalModel,
    ReviewCandidateModel,
    ReviewRunModel,
)
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.repository.git import run_git
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.domain import ProposalStatus, ReviewCandidateReason, ReviewRunStatus
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse
from tests.support.git_repo import commit_all

_API_BASE = "https://api.github.com"
_UNDEFINED_NAME_MODULE = "def f():\n    return undefined_name\n"
_FIXED_MODULE = "def f():\n    return 1\n"


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
            symbol_name="f", qualified_name="m.f", start_line=start_line, end_line=end_line,
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
async def test_identical_sha_comparison_is_still_present(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Part U: an explicit no-op comparison (same SHA) is allowed, not
    rejected -- and correctly resolves to STILL_PRESENT since nothing at
    all changed."""

    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    original_sha = _push_commit(remote, tmp_path, files={"m.py": _UNDEFINED_NAME_MODULE})

    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(session_factory, repository_id=repository.id, commit_sha=original_sha)
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    async with session_factory() as session:
        attempt = await _service(remote=remote).start_fix_attempt(
            session, handoff=handoff, candidate_fix_commit_sha=original_sha
        )

    assert attempt.status == FixAttemptStatus.STILL_PRESENT
    assert attempt.result is not None
    assert any("byte-identical" in e for e in attempt.result.deterministic_evidence)


@respx.mock
async def test_unrelated_file_change_leaves_flagged_file_unchanged_still_present(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    _mock_token_route()
    remote = _init_bare_remote(tmp_path)
    original_sha = _push_commit(remote, tmp_path, files={"m.py": _UNDEFINED_NAME_MODULE, "other.py": "a = 1\n"})
    candidate_sha = _push_commit(remote, tmp_path, files={"other.py": "a = 2\n"})

    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(session_factory, repository_id=repository.id, commit_sha=original_sha)
    handoff = await _build_handoff(session_factory, finding_id=finding_id)

    async with session_factory() as session:
        attempt = await _service(remote=remote).start_fix_attempt(
            session, handoff=handoff, candidate_fix_commit_sha=candidate_sha
        )

    assert attempt.status == FixAttemptStatus.STILL_PRESENT


@respx.mock
async def test_static_recheck_confirms_fixed(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> None:
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

    assert attempt.status == FixAttemptStatus.FIXED
    assert attempt.result is not None
    assert any("no longer fires" in e for e in attempt.result.deterministic_evidence)


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
    original_sha = _push_commit(remote, tmp_path, files={"m.py": "a = 1\nb = 2\n"})
    candidate_sha = _push_commit(remote, tmp_path, files={"m.py": "a = 1\nb = 3\n"})

    repository = await _make_repository(session_factory, "acme/widgets")
    finding_id = await _stage_finding(
        session_factory, repository_id=repository.id, commit_sha=original_sha, start_line=2, end_line=2,
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
async def test_no_provider_call_when_deterministic_static_signal_is_sufficient(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Part X: deterministic-first -- a configured critic provider must
    never be called when static re-check already decided the outcome."""

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

    provider = FakeLLMProvider([])  # exhausted -- raises if ever called
    async with session_factory() as session:
        attempt = await _service(remote=remote, fix_critic_provider=provider).start_fix_attempt(
            session, handoff=handoff, candidate_fix_commit_sha=candidate_sha
        )

    assert attempt.status == FixAttemptStatus.FIXED
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
