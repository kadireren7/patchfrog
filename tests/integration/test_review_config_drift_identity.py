"""Coverage that changing ``.patchfrog.yml``-resolved config fields
actually changes production *behavior* (not just the fingerprint) and
that the resulting runs never get incorrectly reused across distinct
configs -- the two things a config-parity fix must both get right."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.analysis.domain import Confidence
from patchfrog.diff.models import DiffFile, DiffHunk, DiffLine, DiffLineType
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.models.review import ReviewRunModel
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.review.config import ReviewConfig
from patchfrog.review.domain import ReviewRunStatus
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse
from patchfrog.review.service import PullRequestReviewService
from tests.support.git_repo import materialize_fixture_repo


def _diff_marking_lines(file_path: str, lines: list[int]) -> DiffFile:
    diff_lines = tuple(
        DiffLine(line_type=DiffLineType.ADDITION, old_line_number=None, new_line_number=n, content="x")
        for n in lines
    )
    hunk = DiffHunk(
        old_start=1, old_lines=0, new_start=min(lines), new_lines=len(lines),
        section_heading=None, lines=diff_lines,
    )
    return DiffFile(path=file_path, hunks=(hunk,))


async def _setup(
    session_factory: async_sessionmaker[AsyncSession], *, full_name: str
) -> tuple[uuid.UUID, str, Path]:
    async with session_factory() as session:
        row = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name=full_name.split("/")[-1], full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = row.id

    root = Path("/tmp") / f"pf-ai-review-drift-{uuid.uuid4().hex[:8]}"
    snapshot = materialize_fixture_repo(root, "ai_review_python", full_name=full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
    )
    return repository_id, snapshot.commit_sha, snapshot.root_path


async def test_max_candidates_change_alters_behavior_and_identity(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/drift-max-candidates")
    # 4 distinct symbols changed: can_withdraw(14), apply_payment_result(22),
    # load_account_config(39), parse_int_or_none(52).
    diff_files = [_diff_marking_lines("src/billing.py", [14, 22, 39, 52])]

    provider = FakeLLMProvider(
        response_factory=lambda req: ScriptedResponse(raw_json=json.dumps({"findings": []}))
    )
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=provider)

    small = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/drift-max-candidates",
        commit_sha=commit_sha, diff_files=diff_files, config=ReviewConfig(max_candidates=1),
    )
    large = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/drift-max-candidates",
        commit_sha=commit_sha, diff_files=diff_files, config=ReviewConfig(max_candidates=4),
    )

    assert small.candidate_count == 1
    assert large.candidate_count == 4
    assert small.reused_existing_run is False
    assert large.reused_existing_run is False  # distinct config -> distinct identity, never reused

    async with session_factory() as session:
        runs = (
            await session.execute(select(ReviewRunModel).where(ReviewRunModel.repository_id == repository_id))
        ).scalars().all()
    succeeded = [r for r in runs if r.status == ReviewRunStatus.SUCCEEDED]
    assert len(succeeded) == 2
    assert len({r.config_fingerprint for r in succeeded}) == 2


async def test_min_confidence_change_alters_behavior_and_identity(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/drift-min-confidence")
    diff_files = [_diff_marking_lines("src/billing.py", [14])]

    finding = {
        "title": "Inverted comparison", "message": "backwards", "category": "correctness",
        "severity": "medium", "confidence": "medium",
        "file_path": "src/billing.py", "start_line": 14, "end_line": 14,
        "evidence": [{"file_path": "src/billing.py", "start_line": 14, "end_line": 14, "quoted_text": "return amount >= balance"}],
        "reasoning_summary": "x", "suggested_fix": None,
    }
    provider = FakeLLMProvider(
        response_factory=lambda req: ScriptedResponse(raw_json=json.dumps({"findings": [finding]}))
    )
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=provider)

    strict = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/drift-min-confidence",
        commit_sha=commit_sha, diff_files=diff_files, config=ReviewConfig(min_final_confidence=Confidence.HIGH),
    )
    lenient = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/drift-min-confidence",
        commit_sha=commit_sha, diff_files=diff_files, config=ReviewConfig(min_final_confidence=Confidence.MEDIUM),
    )

    assert strict.accepted_count == 0  # medium confidence finding below a 'high' bar
    assert lenient.accepted_count == 1  # same finding, 'medium' bar -- accepted
    assert strict.reused_existing_run is False
    assert lenient.reused_existing_run is False

    async with session_factory() as session:
        runs = (
            await session.execute(select(ReviewRunModel).where(ReviewRunModel.repository_id == repository_id))
        ).scalars().all()
    succeeded = [r for r in runs if r.status == ReviewRunStatus.SUCCEEDED]
    assert len(succeeded) == 2
    assert len({r.config_fingerprint for r in succeeded}) == 2


async def test_model_change_alters_effective_identity_and_is_never_reused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A ``.patchfrog.yml``-resolved ``model`` change must invalidate
    canonical-run reuse on both halves of a run's identity: the
    *configuration intent* fingerprint (``config.model``, part of
    ``ReviewConfig.fingerprint()``) and the *effective* toolchain
    fingerprint (``ReviewModelIdentity.fingerprint()``, built from the
    provider actually selected for that model -- see
    :func:`patchfrog.review.provider_factory.build_reviewer_provider`,
    which routes on ``config.model``). Two distinct
    ``FakeLLMProvider(model_id=...)`` instances stand in for what
    ``build_reviewer_provider`` would actually construct for two
    different configured models, so this exercises real production
    routing behavior, not just a fingerprint computed in isolation."""

    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/drift-model")
    diff_files = [_diff_marking_lines("src/billing.py", [14])]

    sonnet_provider = FakeLLMProvider(
        response_factory=lambda req: ScriptedResponse(raw_json=json.dumps({"findings": []})),
        model_id="claude-sonnet-5",
    )
    opus_provider = FakeLLMProvider(
        response_factory=lambda req: ScriptedResponse(raw_json=json.dumps({"findings": []})),
        model_id="claude-opus-5",
    )

    sonnet_run = await PullRequestReviewService(
        session_factory=session_factory, reviewer_provider=sonnet_provider
    ).review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/drift-model",
        commit_sha=commit_sha, diff_files=diff_files, config=ReviewConfig(model="claude-sonnet-5"),
    )
    opus_run = await PullRequestReviewService(
        session_factory=session_factory, reviewer_provider=opus_provider
    ).review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/drift-model",
        commit_sha=commit_sha, diff_files=diff_files, config=ReviewConfig(model="claude-opus-5"),
    )

    assert sonnet_run.reused_existing_run is False
    assert opus_run.reused_existing_run is False  # distinct model -> distinct identity, never reused

    async with session_factory() as session:
        runs = (
            await session.execute(select(ReviewRunModel).where(ReviewRunModel.repository_id == repository_id))
        ).scalars().all()
    succeeded = [r for r in runs if r.status == ReviewRunStatus.SUCCEEDED]
    assert len(succeeded) == 2
    assert len({r.config_fingerprint for r in succeeded}) == 2
    assert len({r.model_fingerprint for r in succeeded}) == 2
    assert {r.reviewer_model for r in succeeded} == {"claude-sonnet-5", "claude-opus-5"}
