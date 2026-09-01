"""Real, end-to-end (fake-GitHub) coverage that a Phase 7
(:mod:`patchfrog.review_memory`) zero-AI-call carried-forward finding can
actually be published once a publish gate opens on a later head -- the
exact product gap Milestone H's own live dogfood exposed (see
``validation/production_e2e/latest-summary.md``'s "known limitation"
section) and
:func:`patchfrog.publishing.queries.get_current_active_findings` closes.

Real git repo (three real commits, real ``git diff``/``git log``
plumbing -- mirrors ``tests/integration/test_review_memory_end_to_end.py``
exactly), real :class:`~patchfrog.review.service.PullRequestReviewService`
+ :class:`~patchfrog.review_memory.service.IncrementalReviewMemoryService`
orchestration, real :class:`~patchfrog.publishing.service.ReviewPublicationService`
against a :class:`~patchfrog.publishing.fake_publisher.FakeReviewPublisher`
(no network -- the real-GitHub proof of this same mechanism is the live
dogfood recorded in ``validation/production_e2e/``, never re-run here).

Scenario:

    commit1: introduces a real bug in ``divide`` (math_ops.py). Publish
             gate is disabled -- the real, accepted finding is never
             published.
    commit2: config-only change (README.md only; ``divide`` itself is
             byte-for-byte untouched) -- publish gate is now enabled.
             ``divide``'s finding survives as a zero-AI-call
             CARRIED_FORWARD memory row -- zero provider calls for
             ``divide`` specifically (a real provider call for the
             never-before-seen README.md region is expected and
             unrelated) -- and must still become publishable.
    commit3: another config-only change -- ``divide`` carried forward
             again; already actually published by commit2, so it is
             suppressed (never a duplicate GitHub write).
    commit4: ``divide``'s bug is genuinely fixed -- a real AI recheck
             (evidence changed) resolves it; a resolved finding is never
             published.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.domain.pull_request import ChangedFile, FileChangeStatus, PullRequestMetadata
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.models.publishing import ReviewPublicationModel
from patchfrog.persistence.repositories import (
    PullRequestRepository,
    RepositoryIndexRepository,
    RepositoryRepository,
)
from patchfrog.persistence.repositories.review_memory_finding import ReviewMemoryFindingRepository
from patchfrog.publishing.config import PublicationConfig
from patchfrog.publishing.domain import (
    ReviewPublicationMode,
    ReviewPublicationResult,
    ReviewPublicationStatus,
)
from patchfrog.publishing.fake_publisher import FakeReviewPublisher
from patchfrog.publishing.service import ReviewPublicationService
from patchfrog.repository.git import run_git
from patchfrog.review.config import ReviewConfig
from patchfrog.review.local_diff import diff_against_base
from patchfrog.review.provider import ProviderRequest
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse
from patchfrog.review.service import PullRequestReviewService
from patchfrog.review_memory.config import IncrementalConfig
from patchfrog.review_memory.domain import FindingMemoryStatus
from patchfrog.review_memory.service import IncrementalReviewMemoryService
from tests.support.git_repo import commit_all, init_git_repo
from tests.support.publishing import whole_file_added_patch

_DIVIDE_BUGGY = '''def divide(a, b):
    return a - b  # BUG: should be a / b
'''
_DIVIDE_FIXED = '''def divide(a, b):
    return a / b
'''
_DIVIDE_BUG_TITLE = "divide() subtracts instead of dividing"
_DIVIDE_BUG_FINDING = {
    "title": _DIVIDE_BUG_TITLE,
    "message": "divide(a, b) returns a - b, not a / b.",
    "category": "correctness",
    "severity": "high",
    "confidence": "high",
    "file_path": "math_ops.py",
    "start_line": 1,
    "end_line": 2,
    "evidence": [{"file_path": "math_ops.py", "start_line": 2, "end_line": 2, "quoted_text": "return a - b"}],
    "reasoning_summary": "Function name promises division; body performs subtraction.",
    "suggested_fix": "return a / b",
}
_NO_FINDINGS: dict[str, list[object]] = {"findings": []}
_ACCEPT_VERDICT = {
    "decision": "accept", "reasoning_summary": "confirmed", "downgraded_severity": None, "downgraded_confidence": None,
}


def _make_reviewer() -> tuple[FakeLLMProvider, FakeLLMProvider]:
    def factory(request: ProviderRequest) -> ScriptedResponse:
        if request.schema_name == "critic_verdict":
            return ScriptedResponse(raw_json=json.dumps(_ACCEPT_VERDICT))
        if "Review target: `divide`" in request.user_prompt and "return a - b" in request.user_prompt:
            return ScriptedResponse(raw_json=json.dumps({"findings": [_DIVIDE_BUG_FINDING]}))
        return ScriptedResponse(raw_json=json.dumps(_NO_FINDINGS))

    return FakeLLMProvider(response_factory=factory), FakeLLMProvider(response_factory=factory)


def _divide_was_prompted(provider: FakeLLMProvider) -> bool:
    """Whether ``divide`` itself was ever sent to this provider -- the
    ground truth for "zero-AI-call carry-forward" (see
    ``tests/integration/test_review_memory_end_to_end.py``'s own
    ``_prompted_targets`` helper). A run can legitimately still make a
    real provider call for some *other*, never-before-seen candidate
    (e.g. a brand-new file added in a config-only commit) without that
    contradicting divide's own carry-forward being AI-call-free."""

    return any("Review target: `divide`" in call.user_prompt for call in provider.calls)


def _rev_parse(root: Path, ref: str) -> str:
    return run_git(["-C", str(root), "rev-parse", ref]).strip()


def _pr_metadata(*, number: int, head_sha: str) -> PullRequestMetadata:
    return PullRequestMetadata(
        number=number, title="t", body=None, author="a", base_branch="main", head_branch="feature",
        base_sha="0" * 40, head_sha=head_sha, html_url="https://github.com/test/repo/pull/1", state="open",
    )


def _changed_files(root: Path) -> list[ChangedFile]:
    content = (root / "math_ops.py").read_text()
    return [
        ChangedFile(
            path="math_ops.py", previous_path=None, status=FileChangeStatus.ADDED,
            additions=len(content.splitlines()), deletions=0, patch=whole_file_added_patch(content),
        )
    ]


async def test_carried_forward_finding_publish_lifecycle(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/carried-forward-publish-lifecycle"
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("scratch repo for carried-forward publish testing\n")
    init_git_repo(root)
    commit_all(root, "base")
    base_sha = _rev_parse(root, "HEAD")

    async with session_factory() as session:
        repo_row = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner=full_name.split("/")[0], name=full_name.split("/")[-1],
            full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = repo_row.id

        pr_row = await PullRequestRepository().upsert(
            session, repository_id=repository_id, github_pr_number=1, title="carried-forward e2e",
            author="test", base_sha=base_sha, head_sha=base_sha, state="open",
        )
        await session.commit()
        pull_request_id = pr_row.id

    indexing = RepositoryIndexingService(session_factory=session_factory)
    memory = IncrementalReviewMemoryService(session_factory=session_factory)
    incremental_config = IncrementalConfig()

    # ---- commit1: introduce divide's bug; publish gate disabled ----
    (root / "math_ops.py").write_text(_DIVIDE_BUGGY)
    commit_all(root, "introduce divide bug")
    commit1_sha = _rev_parse(root, "HEAD")
    await indexing.index_local_repository(repository_id=repository_id, root_path=root, repository_full_name=full_name)
    diff_files1 = diff_against_base(root, base_sha)

    reviewer1, critic1 = _make_reviewer()
    async with session_factory() as session:
        index1 = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index1 is not None
    candidates1 = await memory.build_candidates(
        repository_id=repository_id, repository_index_id=index1.id, commit_sha=commit1_sha,
        diff_files=diff_files1, max_candidates=40,
    )
    prepared1 = await memory.prepare(
        pull_request_id=pull_request_id, repository_index_id=index1.id, commit_sha=commit1_sha,
        clone_url=str(root), token=None, current_candidates=candidates1,
        reviewer_provider="fake", reviewer_model="fake-model", incremental_config=incremental_config,
    )
    service1 = PullRequestReviewService(session_factory=session_factory, reviewer_provider=reviewer1, critic_provider=critic1)
    summary1 = await service1.review_local(
        repository_id=repository_id, root_path=root, repository_full_name=full_name, commit_sha=commit1_sha,
        diff_files=diff_files1, pull_request_id=pull_request_id,
        candidate_filter=prepared1.candidate_filter, incremental_context_fingerprint=prepared1.incremental_context_fingerprint,
        config=ReviewConfig(max_concurrent_requests=1),
    )
    assert summary1.accepted_count == 1
    await memory.finalize(
        review_run_id=summary1.run_id, repository_id=repository_id, pull_request_id=pull_request_id,
        commit_sha=commit1_sha, prepared=prepared1,
    )

    publisher1 = FakeReviewPublisher(
        pull_request=_pr_metadata(number=1, head_sha=commit1_sha), changed_files=_changed_files(root)
    )
    publish_service1 = ReviewPublicationService(session_factory=session_factory, publisher=publisher1)
    result1 = await publish_service1.publish(
        review_run_id=summary1.run_id, mode=ReviewPublicationMode.PUBLISH,
        config=PublicationConfig(enabled=False),
    )
    assert result1.status is ReviewPublicationStatus.SKIPPED_DISABLED
    assert publisher1.publish_calls == []  # never actually published

    # ---- commit2: config-only change; divide untouched; publish gate now enabled ----
    (root / "README.md").write_text("scratch repo for carried-forward publish testing\nconfig-only follow-up\n")
    commit_all(root, "config-only follow-up (publish gate opens)")
    commit2_sha = _rev_parse(root, "HEAD")
    await indexing.index_local_repository(repository_id=repository_id, root_path=root, repository_full_name=full_name)
    diff_files2 = diff_against_base(root, base_sha)

    reviewer2, critic2 = _make_reviewer()
    async with session_factory() as session:
        index2 = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index2 is not None
    candidates2 = await memory.build_candidates(
        repository_id=repository_id, repository_index_id=index2.id, commit_sha=commit2_sha,
        diff_files=diff_files2, max_candidates=40,
    )
    prepared2 = await memory.prepare(
        pull_request_id=pull_request_id, repository_index_id=index2.id, commit_sha=commit2_sha,
        clone_url=str(root), token=None, current_candidates=candidates2,
        reviewer_provider="fake", reviewer_model="fake-model", incremental_config=incremental_config,
    )
    service2 = PullRequestReviewService(session_factory=session_factory, reviewer_provider=reviewer2, critic_provider=critic2)
    summary2 = await service2.review_local(
        repository_id=repository_id, root_path=root, repository_full_name=full_name, commit_sha=commit2_sha,
        diff_files=diff_files2, pull_request_id=pull_request_id,
        candidate_filter=prepared2.candidate_filter, incremental_context_fingerprint=prepared2.incremental_context_fingerprint,
        config=ReviewConfig(max_concurrent_requests=1),
    )
    assert summary2.accepted_count == 0  # zero fresh findings this run
    assert not _divide_was_prompted(reviewer2)  # zero AI calls for divide -- true carry-forward
    await memory.finalize(
        review_run_id=summary2.run_id, repository_id=repository_id, pull_request_id=pull_request_id,
        commit_sha=commit2_sha, prepared=prepared2,
    )

    async with session_factory() as session:
        open_findings2 = await ReviewMemoryFindingRepository().get_open_for_pr(session, pull_request_id=pull_request_id)
    assert {f.title: f.status for f in open_findings2}[_DIVIDE_BUG_TITLE] is FindingMemoryStatus.CARRIED_FORWARD

    publisher2 = FakeReviewPublisher(
        pull_request=_pr_metadata(number=1, head_sha=commit2_sha), changed_files=_changed_files(root)
    )
    publish_service2 = ReviewPublicationService(session_factory=session_factory, publisher=publisher2)
    result2 = await publish_service2.publish(
        review_run_id=summary2.run_id, mode=ReviewPublicationMode.PUBLISH,
        config=PublicationConfig(enabled=True),
    )
    assert result2.status is ReviewPublicationStatus.PUBLISHED
    assert result2.published_inline == 1  # the carried-forward finding, published for real
    assert len(publisher2.publish_calls) == 1

    # Live publication idempotency: retry the exact same publication identity.
    retry2 = await publish_service2.publish(
        review_run_id=summary2.run_id, mode=ReviewPublicationMode.PUBLISH,
        config=PublicationConfig(enabled=True),
    )
    assert retry2.status is ReviewPublicationStatus.PUBLISHED
    assert retry2.reconciled is True
    assert retry2.publication_id == result2.publication_id
    assert len(publisher2.publish_calls) == 1  # still exactly one -- no duplicate GitHub write
    assert not _divide_was_prompted(reviewer2)  # retry never re-calls the LLM for divide either

    # ---- commit3: another config-only change; divide carried forward again, already published ----
    (root / "README.md").write_text(
        "scratch repo for carried-forward publish testing\nconfig-only follow-up\nsecond line\n"
    )
    commit_all(root, "second config-only follow-up")
    commit3_sha = _rev_parse(root, "HEAD")
    await indexing.index_local_repository(repository_id=repository_id, root_path=root, repository_full_name=full_name)
    diff_files3 = diff_against_base(root, base_sha)

    reviewer3, critic3 = _make_reviewer()
    async with session_factory() as session:
        index3 = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index3 is not None
    candidates3 = await memory.build_candidates(
        repository_id=repository_id, repository_index_id=index3.id, commit_sha=commit3_sha,
        diff_files=diff_files3, max_candidates=40,
    )
    prepared3 = await memory.prepare(
        pull_request_id=pull_request_id, repository_index_id=index3.id, commit_sha=commit3_sha,
        clone_url=str(root), token=None, current_candidates=candidates3,
        reviewer_provider="fake", reviewer_model="fake-model", incremental_config=incremental_config,
    )
    service3 = PullRequestReviewService(session_factory=session_factory, reviewer_provider=reviewer3, critic_provider=critic3)
    summary3 = await service3.review_local(
        repository_id=repository_id, root_path=root, repository_full_name=full_name, commit_sha=commit3_sha,
        diff_files=diff_files3, pull_request_id=pull_request_id,
        candidate_filter=prepared3.candidate_filter, incremental_context_fingerprint=prepared3.incremental_context_fingerprint,
        config=ReviewConfig(max_concurrent_requests=1),
    )
    assert summary3.accepted_count == 0
    assert not _divide_was_prompted(reviewer3)
    await memory.finalize(
        review_run_id=summary3.run_id, repository_id=repository_id, pull_request_id=pull_request_id,
        commit_sha=commit3_sha, prepared=prepared3,
    )

    publisher3 = FakeReviewPublisher(
        pull_request=_pr_metadata(number=1, head_sha=commit3_sha), changed_files=_changed_files(root)
    )
    publish_service3 = ReviewPublicationService(session_factory=session_factory, publisher=publisher3)
    result3 = await publish_service3.publish(
        review_run_id=summary3.run_id, mode=ReviewPublicationMode.PUBLISH,
        config=PublicationConfig(enabled=True),
    )
    assert result3.status is ReviewPublicationStatus.SKIPPED_NO_FINDINGS  # already reported, nothing new
    assert publisher3.publish_calls == []  # zero new GitHub writes -- never a duplicate

    # The stale-head guard must still win for a run that was never
    # actually published (run1, SKIPPED_DISABLED -- so this is a fresh
    # publication identity, not a reconciled retry of an already-PUBLISHED
    # one) once a newer head exists -- even though get_current_active_findings
    # still merges in a carried-forward candidate for it.
    stale_publisher = FakeReviewPublisher(
        pull_request=_pr_metadata(number=1, head_sha=commit3_sha), changed_files=_changed_files(root)
    )
    stale_result = await ReviewPublicationService(session_factory=session_factory, publisher=stale_publisher).publish(
        review_run_id=summary1.run_id, mode=ReviewPublicationMode.PUBLISH, config=PublicationConfig(enabled=True)
    )
    assert stale_result.status is ReviewPublicationStatus.STALE
    assert stale_publisher.publish_calls == []

    # ---- commit4: divide's bug genuinely fixed -- real recheck resolves it ----
    (root / "math_ops.py").write_text(_DIVIDE_FIXED)
    commit_all(root, "fix divide")
    commit4_sha = _rev_parse(root, "HEAD")
    await indexing.index_local_repository(repository_id=repository_id, root_path=root, repository_full_name=full_name)
    diff_files4 = diff_against_base(root, base_sha)

    reviewer4, critic4 = _make_reviewer()
    async with session_factory() as session:
        index4 = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index4 is not None
    candidates4 = await memory.build_candidates(
        repository_id=repository_id, repository_index_id=index4.id, commit_sha=commit4_sha,
        diff_files=diff_files4, max_candidates=40,
    )
    prepared4 = await memory.prepare(
        pull_request_id=pull_request_id, repository_index_id=index4.id, commit_sha=commit4_sha,
        clone_url=str(root), token=None, current_candidates=candidates4,
        reviewer_provider="fake", reviewer_model="fake-model", incremental_config=incremental_config,
    )
    service4 = PullRequestReviewService(session_factory=session_factory, reviewer_provider=reviewer4, critic_provider=critic4)
    summary4 = await service4.review_local(
        repository_id=repository_id, root_path=root, repository_full_name=full_name, commit_sha=commit4_sha,
        diff_files=diff_files4, pull_request_id=pull_request_id,
        candidate_filter=prepared4.candidate_filter, incremental_context_fingerprint=prepared4.incremental_context_fingerprint,
        config=ReviewConfig(max_concurrent_requests=1),
    )
    reconciliation4 = await memory.finalize(
        review_run_id=summary4.run_id, repository_id=repository_id, pull_request_id=pull_request_id,
        commit_sha=commit4_sha, prepared=prepared4,
    )
    assert len(reconciliation4.resolved_memory_finding_ids) == 1  # divide's bug resolved for real

    publisher4 = FakeReviewPublisher(
        pull_request=_pr_metadata(number=1, head_sha=commit4_sha), changed_files=_changed_files(root)
    )
    publish_service4 = ReviewPublicationService(session_factory=session_factory, publisher=publisher4)
    result4 = await publish_service4.publish(
        review_run_id=summary4.run_id, mode=ReviewPublicationMode.PUBLISH,
        config=PublicationConfig(enabled=True),
    )
    assert result4.status is ReviewPublicationStatus.SKIPPED_NO_FINDINGS  # resolved finding never published
    assert publisher4.publish_calls == []


async def test_post_clean_summary_never_fires_for_an_already_published_carried_forward_finding(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Regression test for a real bug found in review of this exact
    feature interaction: get_current_active_findings deliberately
    returns an EMPTY findings list for a carried-forward finding that
    was already actually published (surfaced only via
    already_reported_finding_ids instead -- see that function's own
    docstring). A naive `if not findings: post a clean summary` check
    would misread that as "genuinely clean" and post a false "no
    publishable findings" summary over an active, already-known
    finding -- repeating that false claim on every subsequent
    synchronize. Proven here through the REAL
    get_current_active_findings -> ReviewPublicationService ->
    PublicationPlanner path (never a hand-built findings list), with
    post_clean_summary=True enabled throughout every step, including
    the one real live-shaped publish (case E: a never-before-published
    carried/fresh finding must still publish exactly once, never
    replaced by a clean summary just because the setting is on)."""

    full_name = "test/carried-forward-clean-summary-interaction"
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("scratch repo for clean-summary interaction testing\n")
    init_git_repo(root)
    commit_all(root, "base")
    base_sha = _rev_parse(root, "HEAD")

    async with session_factory() as session:
        repo_row = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner=full_name.split("/")[0], name=full_name.split("/")[-1],
            full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = repo_row.id

        pr_row = await PullRequestRepository().upsert(
            session, repository_id=repository_id, github_pr_number=1, title="clean-summary interaction e2e",
            author="test", base_sha=base_sha, head_sha=base_sha, state="open",
        )
        await session.commit()
        pull_request_id = pr_row.id

    indexing = RepositoryIndexingService(session_factory=session_factory)
    memory = IncrementalReviewMemoryService(session_factory=session_factory)
    incremental_config = IncrementalConfig()

    async def _commit_review_and_publish(
        *, commit_message: str, config: PublicationConfig
    ) -> tuple[ReviewPublicationResult, FakeReviewPublisher]:
        commit_sha = commit_all(root, commit_message)
        await indexing.index_local_repository(repository_id=repository_id, root_path=root, repository_full_name=full_name)
        diff_files = diff_against_base(root, base_sha)
        reviewer, critic = _make_reviewer()
        async with session_factory() as session:
            index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
            assert index is not None
        candidates = await memory.build_candidates(
            repository_id=repository_id, repository_index_id=index.id, commit_sha=commit_sha,
            diff_files=diff_files, max_candidates=40,
        )
        prepared = await memory.prepare(
            pull_request_id=pull_request_id, repository_index_id=index.id, commit_sha=commit_sha,
            clone_url=str(root), token=None, current_candidates=candidates,
            reviewer_provider="fake", reviewer_model="fake-model", incremental_config=incremental_config,
        )
        service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=reviewer, critic_provider=critic)
        summary = await service.review_local(
            repository_id=repository_id, root_path=root, repository_full_name=full_name, commit_sha=commit_sha,
            diff_files=diff_files, pull_request_id=pull_request_id,
            candidate_filter=prepared.candidate_filter, incremental_context_fingerprint=prepared.incremental_context_fingerprint,
            config=ReviewConfig(max_concurrent_requests=1),
        )
        await memory.finalize(
            review_run_id=summary.run_id, repository_id=repository_id, pull_request_id=pull_request_id,
            commit_sha=commit_sha, prepared=prepared,
        )
        publisher = FakeReviewPublisher(pull_request=_pr_metadata(number=1, head_sha=commit_sha), changed_files=_changed_files(root))
        result = await ReviewPublicationService(session_factory=session_factory, publisher=publisher).publish(
            review_run_id=summary.run_id, mode=ReviewPublicationMode.PUBLISH, config=config,
        )
        return result, publisher

    # ---- commit1: introduce divide's bug; publish disabled ----
    (root / "math_ops.py").write_text(_DIVIDE_BUGGY)
    result1, publisher1 = await _commit_review_and_publish(
        commit_message="introduce divide bug",
        config=PublicationConfig(enabled=False, post_clean_summary=True),
    )
    assert result1.status is ReviewPublicationStatus.SKIPPED_DISABLED
    assert publisher1.publish_calls == []

    # ---- commit2: config-only change; divide untouched; publish gate now enabled ----
    # Case E: a never-before-published carried finding must still publish
    # for real, exactly once -- post_clean_summary=True must never
    # preempt or replace a real, active finding.
    (root / "README.md").write_text("scratch repo for clean-summary interaction testing\nconfig-only follow-up\n")
    result2, publisher2 = await _commit_review_and_publish(
        commit_message="config-only follow-up (publish gate opens)",
        config=PublicationConfig(enabled=True, post_clean_summary=True),
    )
    assert result2.status is ReviewPublicationStatus.PUBLISHED
    assert result2.published_inline == 1
    assert len(publisher2.publish_calls) == 1
    assert "no publishable findings" not in publisher2.publish_calls[0].body

    # ---- commit3: another config-only change; divide carried forward, already published ----
    # Case A: get_current_active_findings returns findings=[] for this
    # run (the finding is only surfaced via already_reported_finding_ids)
    # -- must be SKIPPED_NO_FINDINGS, never a false clean-summary post.
    (root / "README.md").write_text(
        "scratch repo for clean-summary interaction testing\nconfig-only follow-up\nsecond line\n"
    )
    result3, publisher3 = await _commit_review_and_publish(
        commit_message="second config-only follow-up",
        config=PublicationConfig(enabled=True, post_clean_summary=True),
    )
    assert result3.status is ReviewPublicationStatus.SKIPPED_NO_FINDINGS
    assert publisher3.publish_calls == []
    async with session_factory() as session:
        model3 = await session.get(ReviewPublicationModel, result3.publication_id)
        assert model3 is not None
        assert model3.reason is not None and "already reported" in model3.reason

    # ---- commit4: yet another config-only change (a second subsequent synchronize) ----
    # Case B: no clean-summary spam on a second later synchronize either.
    (root / "README.md").write_text(
        "scratch repo for clean-summary interaction testing\nconfig-only follow-up\nsecond line\nthird line\n"
    )
    result4, publisher4 = await _commit_review_and_publish(
        commit_message="third config-only follow-up",
        config=PublicationConfig(enabled=True, post_clean_summary=True),
    )
    assert result4.status is ReviewPublicationStatus.SKIPPED_NO_FINDINGS
    assert publisher4.publish_calls == []
