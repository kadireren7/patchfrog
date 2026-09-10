"""Real, DB-backed corpus for the MCP server surface (Milestone T, T2) --
calls the four tools exactly as an MCP client would
(``server.mcp.call_tool(name, arguments)``), never invoking the private
service methods directly."""

from __future__ import annotations

import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.config.settings import Settings
from patchfrog.mcp.server import MAX_LIST_FINDINGS_LIMIT, PatchFrogMCPServer
from patchfrog.persistence.models.review import (
    AIFindingModel,
    AIFindingProposalModel,
    ReviewCandidateModel,
    ReviewRunModel,
)
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.domain import ProposalStatus, ReviewCandidateReason, ReviewRunStatus


async def _make_repository(session_factory: async_sessionmaker[AsyncSession], full_name: str) -> uuid.UUID:
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name=full_name.split("/")[-1], full_name=full_name, installation_id=1,
        )
        await session.commit()
        return repo.id


async def _stage_findings(
    session_factory: async_sessionmaker[AsyncSession], *, repository_id: uuid.UUID, count: int, commit_sha: str | None = None,
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    sha = commit_sha or uuid.uuid4().hex[:40].ljust(40, "0")
    finding_ids: list[uuid.UUID] = []
    async with session_factory() as session:
        run = ReviewRunModel(
            id=uuid.uuid4(), repository_id=repository_id, repository_index_id=uuid.uuid4(),
            commit_sha=sha, config_fingerprint="c" * 64, model_fingerprint="m" * 64,
            incremental_context_fingerprint="i" * 64, status=ReviewRunStatus.SUCCEEDED,
            reviewer_provider="fake", reviewer_model="fake-model",
            started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()

        for i in range(count):
            candidate = ReviewCandidateModel(
                id=uuid.uuid4(), review_run_id=run.id, file_path=f"m{i}.py", symbol_id=None,
                symbol_name="f", qualified_name=f"m{i}.f", start_line=1, end_line=2,
                changed_lines="[1]", reason=ReviewCandidateReason.CHANGED_SYMBOL,
            )
            session.add(candidate)
            await session.flush()

            proposal = AIFindingProposalModel(
                id=uuid.uuid4(), review_run_id=run.id, candidate_id=candidate.id, title=f"finding {i}",
                message="m", category=FindingCategory.CORRECTNESS, severity=Severity.MEDIUM,
                confidence=Confidence.HIGH, file_path=f"m{i}.py", start_line=1, end_line=2, evidence="[]",
                reasoning_summary="r", status=ProposalStatus.ACCEPTED, agent_role=AgentRole.CORRECTNESS,
            )
            session.add(proposal)
            await session.flush()

            finding = AIFindingModel(
                id=uuid.uuid4(), review_run_id=run.id, proposal_id=proposal.id, candidate_id=candidate.id,
                title=f"finding {i}", message="m", category=FindingCategory.CORRECTNESS, severity=Severity.MEDIUM,
                confidence=Confidence.HIGH, file_path=f"m{i}.py", start_line=1, end_line=2, evidence="[]",
                reasoning_summary="r", agent_role=AgentRole.CORRECTNESS,
            )
            session.add(finding)
            await session.flush()
            finding_ids.append(finding.id)

        await session.commit()
        return run.id, finding_ids


def _server(session_factory: async_sessionmaker[AsyncSession]) -> PatchFrogMCPServer:
    return PatchFrogMCPServer(session_factory=session_factory, settings=Settings())


async def _call(server: PatchFrogMCPServer, name: str, **arguments: object) -> dict[str, object]:
    _blocks, structured = await server.mcp.call_tool(name, arguments)
    assert isinstance(structured, dict)
    return structured


async def test_list_findings_returns_findings_for_review_run(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/a")
    run_id, finding_ids = await _stage_findings(session_factory, repository_id=repository_id, count=3)

    result = await _call(
        _server(session_factory), "list_findings", repository_full_name="acme/a", review_run_id=str(run_id)
    )
    assert result["total_matching"] == 3
    findings = result["findings"]
    assert isinstance(findings, list)
    assert {f["finding_id"] for f in findings} == {str(i) for i in finding_ids}


async def test_list_findings_limit_is_bounded(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/a")
    run_id, _finding_ids = await _stage_findings(session_factory, repository_id=repository_id, count=5)

    result = await _call(
        _server(session_factory), "list_findings", repository_full_name="acme/a", review_run_id=str(run_id),
        limit=10_000,
    )
    # total_matching still reports the true count; the returned page is
    # capped at MAX_LIST_FINDINGS_LIMIT regardless of what was requested.
    assert result["total_matching"] == 5
    assert len(result["findings"]) <= MAX_LIST_FINDINGS_LIMIT  # type: ignore[arg-type]


async def test_list_findings_without_run_or_pr_is_rejected(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await _make_repository(session_factory, "acme/a")
    result = await _call(_server(session_factory), "list_findings", repository_full_name="acme/a")
    assert result["error"] == "must_provide_review_run_id_or_pull_request_number"


async def test_list_findings_unknown_repository_rejected(session_factory: async_sessionmaker[AsyncSession]) -> None:
    result = await _call(
        _server(session_factory), "list_findings", repository_full_name="nope/nope",
        review_run_id=str(uuid.uuid4()),
    )
    assert result["error"] == "repository_not_found"


async def test_list_findings_malformed_review_run_id_rejected(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await _make_repository(session_factory, "acme/a")
    result = await _call(
        _server(session_factory), "list_findings", repository_full_name="acme/a", review_run_id="not-a-uuid",
    )
    assert result["error"] == "malformed_review_run_id"


async def test_list_findings_cross_repo_review_run_rejected(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_a = await _make_repository(session_factory, "acme/a")
    await _make_repository(session_factory, "acme/b")
    run_id, _ = await _stage_findings(session_factory, repository_id=repository_a, count=1)

    result = await _call(
        _server(session_factory), "list_findings", repository_full_name="acme/b", review_run_id=str(run_id),
    )
    assert result["error"] == "review_run_not_found"


async def test_get_finding_handoff_returns_bounded_evidence(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/a")
    _run_id, finding_ids = await _stage_findings(session_factory, repository_id=repository_id, count=1)

    result = await _call(
        _server(session_factory), "get_finding_handoff", repository_full_name="acme/a", finding_id=str(finding_ids[0]),
    )
    assert "handoff" in result
    handoff = result["handoff"]
    assert isinstance(handoff, dict)
    assert handoff["finding_id"] == str(finding_ids[0])
    assert handoff["schema_version"] == 1
    assert result["known_fix_attempts"] == []


async def test_get_finding_handoff_unknown_finding_rejected(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await _make_repository(session_factory, "acme/a")
    result = await _call(
        _server(session_factory), "get_finding_handoff", repository_full_name="acme/a", finding_id=str(uuid.uuid4()),
    )
    assert result["error"] == "finding_not_found"


async def test_get_finding_handoff_malformed_finding_id_rejected(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await _make_repository(session_factory, "acme/a")
    result = await _call(
        _server(session_factory), "get_finding_handoff", repository_full_name="acme/a", finding_id="not-a-uuid",
    )
    assert result["error"] == "malformed_finding_id"


async def test_get_finding_handoff_cross_repo_finding_rejected_same_as_not_found(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Part AC: a cross-repository lookup must return the identical shape
    as a genuinely missing id -- never confirm the finding exists
    elsewhere."""

    repository_a = await _make_repository(session_factory, "acme/a")
    await _make_repository(session_factory, "acme/b")
    _run_id, finding_ids = await _stage_findings(session_factory, repository_id=repository_a, count=1)

    wrong_repo_result = await _call(
        _server(session_factory), "get_finding_handoff", repository_full_name="acme/b",
        finding_id=str(finding_ids[0]),
    )
    not_found_result = await _call(
        _server(session_factory), "get_finding_handoff", repository_full_name="acme/b", finding_id=str(uuid.uuid4()),
    )
    assert wrong_repo_result == not_found_result == {"error": "finding_not_found"}


async def test_get_finding_handoff_deterministic_across_two_calls(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/a")
    _run_id, finding_ids = await _stage_findings(session_factory, repository_id=repository_id, count=1)

    server = _server(session_factory)
    first = await _call(server, "get_finding_handoff", repository_full_name="acme/a", finding_id=str(finding_ids[0]))
    second = await _call(server, "get_finding_handoff", repository_full_name="acme/a", finding_id=str(finding_ids[0]))
    assert first == second


async def test_get_fix_attempt_unknown_id_rejected(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await _make_repository(session_factory, "acme/a")
    result = await _call(
        _server(session_factory), "get_fix_attempt", repository_full_name="acme/a",
        fix_attempt_id=str(uuid.uuid4()),
    )
    assert result["error"] == "fix_attempt_not_found"


async def test_get_fix_attempt_malformed_id_rejected(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await _make_repository(session_factory, "acme/a")
    result = await _call(
        _server(session_factory), "get_fix_attempt", repository_full_name="acme/a", fix_attempt_id="not-a-uuid",
    )
    assert result["error"] == "malformed_fix_attempt_id"


async def test_start_fix_attempt_unknown_finding_rejected(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await _make_repository(session_factory, "acme/a")
    result = await _call(
        _server(session_factory), "start_fix_attempt", repository_full_name="acme/a", finding_id=str(uuid.uuid4()),
        candidate_fix_commit_sha="a" * 40,
    )
    assert result["error"] == "finding_not_found"


async def test_start_fix_attempt_malformed_sha_rejected(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/a")
    _run_id, finding_ids = await _stage_findings(session_factory, repository_id=repository_id, count=1)

    result = await _call(
        _server(session_factory), "start_fix_attempt", repository_full_name="acme/a",
        finding_id=str(finding_ids[0]), candidate_fix_commit_sha="not-a-sha",
    )
    assert result["error"] == "validation_failed"


async def test_start_fix_attempt_handoff_id_mismatch_rejected(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/a")
    _run_id, finding_ids = await _stage_findings(session_factory, repository_id=repository_id, count=1)

    result = await _call(
        _server(session_factory), "start_fix_attempt", repository_full_name="acme/a",
        finding_id=str(finding_ids[0]), candidate_fix_commit_sha="a" * 40, handoff_id="wrong-hash",
    )
    assert result["error"] == "handoff_id_mismatch"


async def test_mcp_server_boots_and_shuts_down_cleanly_over_stdio(tmp_path: Path) -> None:
    """Part AR items 24-25: a real subprocess, real stdio transport --
    never a mocked server object standing in for a real process boundary.
    Sends a minimal real MCP initialize handshake and confirms a reply,
    then terminates and confirms a clean exit.

    Run with ``cwd`` pointed at an empty scratch directory and an
    explicit, complete, synthetic environment -- a freshly spawned
    subprocess re-imports ``patchfrog.config.settings`` from scratch and
    does not inherit this test session's own ``conftest.py`` monkeypatch
    that disables the repo's real ``.env`` file; running from a directory
    with no ``.env`` at all is what actually keeps this hermetic."""

    import asyncio
    import os

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    env = {
        **os.environ,
        "APP_ENV": "test", "DATABASE_URL": "sqlite+aiosqlite:///:memory:", "REDIS_URL": "redis://localhost:6379/0",
        "GITHUB_APP_ID": "123456", "GITHUB_PRIVATE_KEY": pem, "GITHUB_WEBHOOK_SECRET": "test",
    }
    env.pop("GITHUB_PRIVATE_KEY_PATH", None)

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "patchfrog.cli", "mcp", "serve",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=str(tmp_path), env=env,
    )
    try:
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "0.0.1"},
            },
        }
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write((json.dumps(request) + "\n").encode())
        await proc.stdin.drain()

        line = await asyncio.wait_for(proc.stdout.readline(), timeout=15.0)
        response = json.loads(line)
        assert response["id"] == 1
        assert "result" in response
        assert response["result"]["serverInfo"]["name"] == "patchfrog"
    finally:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=10.0)
