"""End-to-end proof that a secret embedded in source content never
reaches the provider payload -- not just that the redaction function
works in isolation (see test_review_redaction.py), but that the service
actually applies it to every prompt it sends."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.diff.models import DiffFile, DiffHunk, DiffLine, DiffLineType
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse
from patchfrog.review.service import PullRequestReviewService
from tests.support.git_repo import commit_all, materialize_fixture_repo

_FAKE_SECRET = "ghp_1234567890abcdefghijklmnopqrstuvwx"


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


async def test_embedded_secret_never_reaches_the_provider_payload(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    full_name = "test/review-secret-1"
    async with session_factory() as session:
        row = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name="review-secret-1", full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = row.id

    root = Path("/tmp") / f"pf-ai-review-secret-{uuid.uuid4().hex[:8]}"
    snapshot = materialize_fixture_repo(root, "ai_review_python", full_name=full_name)

    billing = snapshot.root_path / "src" / "billing.py"
    billing.write_text(
        billing.read_text()
        + f"\n\n# leaked in a debug print left in by mistake\nDEBUG_TOKEN = '{_FAKE_SECRET}'\n"
    )
    commit_sha = commit_all(snapshot.root_path, "add leaked token")

    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
    )

    # Changed line inside can_withdraw *and* near the leaked secret line,
    # so the secret is very likely to land in at least one candidate's
    # TARGET_FILE_REGION / sibling context.
    new_lines = billing.read_text().splitlines()
    secret_line_number = next(i for i, line in enumerate(new_lines, start=1) if "DEBUG_TOKEN" in line)
    diff_files = [_diff_marking_lines("src/billing.py", [14, secret_line_number])]

    provider = FakeLLMProvider(
        response_factory=lambda req: ScriptedResponse(raw_json=json.dumps({"findings": []}))
    )
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=provider)
    await service.review_local(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name,
        commit_sha=commit_sha, diff_files=diff_files,
    )

    assert len(provider.calls) > 0
    for call in provider.calls:
        assert _FAKE_SECRET not in call.system_prompt
        assert _FAKE_SECRET not in call.user_prompt
