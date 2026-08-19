"""Shared Phase 6 (GitHub review publishing) test scaffolding.

Builds a real, valid ``review_runs`` row with real ``ai_findings`` rows by
running the actual Phase 5 pipeline (``PullRequestReviewService.review_local``
with a scripted ``FakeLLMProvider``) against the ``ai_review_python``
fixture -- the same fixture and pattern every other Phase 5 test uses (see
``tests/integration/test_review_context_bundle_id.py`` etc.). This keeps
Phase 6 tests exercising real FK-valid data (``ai_finding_proposals`` /
``review_candidates`` / ``ai_findings`` all genuinely linked) rather than
hand-rolled rows that could drift from what production actually produces.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.diff.models import DiffFile, DiffHunk, DiffLine, DiffLineType
from patchfrog.domain.pull_request import ChangedFile, FileChangeStatus
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.models.pull_request import PullRequestModel
from patchfrog.persistence.models.review import AIFindingModel, ReviewRunModel
from patchfrog.persistence.repositories import PullRequestRepository, RepositoryRepository
from patchfrog.review.provider import ProviderRequest
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse
from patchfrog.review.service import PullRequestReviewService
from tests.support.git_repo import materialize_fixture_repo

BILLING_PY_LINE_COUNT = 64


def whole_file_added_patch(content: str) -> str:
    """A synthetic unified-diff patch treating the entire file as newly
    added -- every line becomes a valid, mappable diff position (every
    finding line the test fixture can produce ends up "in the diff"),
    without hand-crafting a hunk per test."""

    lines = content.splitlines()
    header = f"@@ -0,0 +1,{len(lines)} @@"
    body = "\n".join(f"+{line}" for line in lines)
    return f"{header}\n{body}"


def diff_marking_lines(file_path: str, lines: list[int]) -> DiffFile:
    """A local ``DiffFile`` (for Phase 5 candidate generation) marking
    ``lines`` as changed additions."""

    diff_lines = tuple(
        DiffLine(line_type=DiffLineType.ADDITION, old_line_number=None, new_line_number=n, content="x")
        for n in lines
    )
    hunk = DiffHunk(
        old_start=1, old_lines=0, new_start=min(lines), new_lines=len(lines),
        section_heading=None, lines=diff_lines,
    )
    return DiffFile(path=file_path, hunks=(hunk,))


@dataclass(frozen=True, slots=True)
class ReviewedPullRequest:
    repository_id: uuid.UUID
    pull_request_id: uuid.UUID
    pull_request_number: int
    review_run_id: uuid.UUID
    commit_sha: str
    root_path: Path
    changed_files: list[ChangedFile]
    findings: list[AIFindingModel]


async def setup_reviewed_pull_request(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    full_name: str,
    changed_lines: list[int],
    response_factory: Callable[[ProviderRequest], ScriptedResponse],
    pull_request_number: int = 1,
    tmp_root: Path,
    installation_id: int = 0,
) -> ReviewedPullRequest:
    async with session_factory() as session:
        repo_row = await RepositoryRepository().upsert(
            session,
            github_repository_id=uuid.uuid4().int & 0x7FFFFFFFFFFFFFFF,
            owner=full_name.split("/")[0],
            name=full_name.split("/")[-1],
            full_name=full_name,
            installation_id=installation_id,
        )
        await session.commit()
        repository_id = repo_row.id

        pr_row = await PullRequestRepository().upsert(
            session,
            repository_id=repository_id,
            github_pr_number=pull_request_number,
            title="test PR",
            author="test-author",
            base_sha="0" * 40,
            head_sha="0" * 40,  # overwritten below once the real commit exists
            state="open",
        )
        await session.commit()
        pull_request_id = pr_row.id

    snapshot = materialize_fixture_repo(tmp_root / "repo", "ai_review_python", full_name=full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
    )

    async with session_factory() as session:
        pr_model = await session.get(PullRequestModel, pull_request_id)
        assert pr_model is not None
        pr_model.head_sha = snapshot.commit_sha
        await session.commit()

    diff_files = [diff_marking_lines("src/billing.py", changed_lines)]
    provider = FakeLLMProvider(response_factory=response_factory)
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=provider)
    summary = await service.review_local(
        repository_id=repository_id,
        root_path=snapshot.root_path,
        repository_full_name=full_name,
        commit_sha=snapshot.commit_sha,
        diff_files=diff_files,
        pull_request_id=pull_request_id,
    )

    async with session_factory() as session:
        run_id = (
            await session.execute(
                select(ReviewRunModel.id).where(
                    ReviewRunModel.repository_id == repository_id,
                    ReviewRunModel.commit_sha == snapshot.commit_sha,
                )
            )
        ).scalars().one()
        findings = (
            await session.execute(select(AIFindingModel).where(AIFindingModel.review_run_id == run_id))
        ).scalars().all()

    billing_content = (snapshot.root_path / "src" / "billing.py").read_text()
    changed_files = [
        ChangedFile(
            path="src/billing.py",
            previous_path=None,
            status=FileChangeStatus.ADDED,
            additions=BILLING_PY_LINE_COUNT,
            deletions=0,
            patch=whole_file_added_patch(billing_content),
        )
    ]

    assert summary.status.value in ("succeeded", "partial"), f"unexpected review status: {summary.status.value}"
    return ReviewedPullRequest(
        repository_id=repository_id,
        pull_request_id=pull_request_id,
        pull_request_number=pull_request_number,
        review_run_id=run_id,
        commit_sha=snapshot.commit_sha,
        root_path=snapshot.root_path,
        changed_files=changed_files,
        findings=list(findings),
    )


def finding_json(
    *,
    title: str = "Inverted comparison",
    message: str = "backwards comparison",
    category: str = "correctness",
    severity: str = "medium",
    confidence: str = "medium",
    file_path: str = "src/billing.py",
    start_line: int = 14,
    end_line: int = 14,
    quoted_text: str = "return amount >= balance",
) -> dict[str, object]:
    return {
        "title": title,
        "message": message,
        "category": category,
        "severity": severity,
        "confidence": confidence,
        "file_path": file_path,
        "start_line": start_line,
        "end_line": end_line,
        "evidence": [
            {"file_path": file_path, "start_line": start_line, "end_line": end_line, "quoted_text": quoted_text}
        ],
        "reasoning_summary": "deterministic test finding",
        "suggested_fix": None,
    }


def scripted_findings_response(findings: list[dict[str, object]]) -> ScriptedResponse:
    return ScriptedResponse(raw_json=json.dumps({"findings": findings}))
