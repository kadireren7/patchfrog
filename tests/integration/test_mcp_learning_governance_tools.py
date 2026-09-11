"""Real corpus for the two Milestone Y/Z MCP tools -- calls them exactly
as an MCP client would (spec test Z25 / Y20: both tools are read-only,
no mutation tool exists)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.analysis.domain import FindingCategory
from patchfrog.config.settings import Settings
from patchfrog.learning_records.domain import (
    LearningEvidenceRef,
    LearningMaturity,
    LearningSurface,
    LearningType,
    RepositoryLearningRecord,
)
from patchfrog.mcp.server import PatchFrogMCPServer
from patchfrog.persistence.repositories import (
    RepositoryLearningRecordRepository,
    RepositoryRepository,
)


async def _make_repository(session_factory: async_sessionmaker[AsyncSession], full_name: str) -> uuid.UUID:
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name=full_name.split("/")[-1], full_name=full_name, installation_id=1,
        )
        await session.commit()
        return repo.id


def _server(session_factory: async_sessionmaker[AsyncSession]) -> PatchFrogMCPServer:
    return PatchFrogMCPServer(session_factory=session_factory, settings=Settings())


async def _call(server: PatchFrogMCPServer, name: str, **arguments: object) -> dict[str, object]:
    _blocks, structured = await server.mcp.call_tool(name, arguments)
    assert isinstance(structured, dict)
    return structured


async def test_list_repository_learnings_returns_persisted_records(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/mcp-learnings")
    now = datetime.now(UTC).isoformat()
    async with session_factory() as session:
        await RepositoryLearningRecordRepository().upsert_many(
            session,
            records=(
                RepositoryLearningRecord(
                    id=None, repository_id=repository_id, learning_type=LearningType.USEFUL_FINDING_PATTERN,
                    surface=LearningSurface(file_path="a.py", qualified_name="a.f", category=FindingCategory.CORRECTNESS),
                    maturity=LearningMaturity.ESTABLISHED, support_count=3,
                    evidence=(LearningEvidenceRef(finding_id=uuid.uuid4(), review_run_id=uuid.uuid4(), observed_at=now),),
                    first_observed_at=now, last_observed_at=now,
                ),
            ),
        )
        await session.commit()

    server = _server(session_factory)
    result = await _call(server, "list_repository_learnings", repository_full_name="acme/mcp-learnings")
    assert len(result["learnings"]) == 1
    assert result["learnings"][0]["maturity"] == "established"


async def test_list_repository_learnings_unknown_repository_returns_error(session_factory: async_sessionmaker[AsyncSession]) -> None:
    server = _server(session_factory)
    result = await _call(server, "list_repository_learnings", repository_full_name="acme/does-not-exist")
    assert result["error"] == "repository_not_found"


async def test_get_effective_policy_returns_platform_floor(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await _make_repository(session_factory, "acme/mcp-policy")
    server = _server(session_factory)
    result = await _call(server, "get_effective_policy", repository_full_name="acme/mcp-policy")
    policy = result["effective_policy"]
    assert policy["security_block_severity_floor"] == "high"
    assert policy["contributing_scopes"] == ["platform"]


async def test_get_effective_policy_unknown_repository_returns_error(session_factory: async_sessionmaker[AsyncSession]) -> None:
    server = _server(session_factory)
    result = await _call(server, "get_effective_policy", repository_full_name="acme/does-not-exist")
    assert result["error"] == "repository_not_found"


async def test_no_governance_or_learning_mutation_tool_exists(session_factory: async_sessionmaker[AsyncSession]) -> None:
    server = _server(session_factory)
    tools = await server.mcp.list_tools()
    names = {t.name for t in tools}
    forbidden = {"set_policy", "disable_security", "force_ready", "override_block", "set_learning", "disable_learning"}
    assert names.isdisjoint(forbidden)
    assert {"list_repository_learnings", "get_effective_policy"}.issubset(names)
