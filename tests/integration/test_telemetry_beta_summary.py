"""Integration coverage for ``patchfrog telemetry beta-summary``
(:mod:`patchfrog.telemetry.beta_summary`) -- external beta readiness.

Reuses the same real-pipeline fixture (``setup_reviewed_pull_request``)
every other Phase 6 publishing test does, rather than hand-rolling
``review_runs``/``ai_findings`` rows that could drift from what
production actually produces.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from patchfrog.telemetry.beta_summary import compute_beta_summary, parse_since
from tests.support.publishing import (
    finding_json,
    scripted_findings_response,
    setup_reviewed_pull_request,
)


async def test_beta_summary_counts_a_real_succeeded_run_with_a_finding(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/beta-summary-1",
        changed_lines=[14],
        response_factory=lambda req: scripted_findings_response([finding_json()]),
        tmp_root=tmp_path,
    )
    assert len(reviewed.findings) == 1

    async with session_factory() as session:
        since = datetime.now(UTC) - timedelta(hours=1)
        summary = await compute_beta_summary(session, since=since, repository_id=reviewed.repository_id)

    assert summary.runs_total == 1
    assert summary.runs_succeeded == 1
    assert summary.runs_failed == 0
    assert summary.aggregate.review_run_count == 1
    assert summary.aggregate.proposals_count >= 1


async def test_beta_summary_scoped_to_repository_never_sees_another_repositorys_runs(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed_a = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/beta-summary-repo-a",
        changed_lines=[14],
        response_factory=lambda req: scripted_findings_response([]),
        tmp_root=tmp_path / "a",
    )
    await setup_reviewed_pull_request(
        session_factory,
        full_name="test/beta-summary-repo-b",
        changed_lines=[14],
        response_factory=lambda req: scripted_findings_response([]),
        tmp_root=tmp_path / "b",
    )

    async with session_factory() as session:
        since = datetime.now(UTC) - timedelta(hours=1)
        summary = await compute_beta_summary(session, since=since, repository_id=reviewed_a.repository_id)

    assert summary.runs_total == 1


async def test_beta_summary_outside_the_time_window_is_excluded(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/beta-summary-old",
        changed_lines=[14],
        response_factory=lambda req: scripted_findings_response([]),
        tmp_root=tmp_path,
    )

    async with session_factory() as session:
        future_since = datetime.now(UTC) + timedelta(hours=1)
        summary = await compute_beta_summary(session, since=future_since, repository_id=reviewed.repository_id)

    assert summary.runs_total == 0


def test_parse_since_accepts_relative_and_iso_forms() -> None:
    relative = parse_since("7d")
    now = datetime.now(UTC)
    assert now - relative < timedelta(days=7, minutes=1)
    assert now - relative > timedelta(days=6, hours=23)

    iso = parse_since("2026-01-01T00:00:00+00:00")
    assert iso.year == 2026


async def test_beta_summary_query_count_scales_with_run_count_not_row_count(
    session_factory: async_sessionmaker[AsyncSession], db_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Documents, rather than hides, a known beta-scale tradeoff:
    ``compute_beta_summary`` calls ``collect_review_telemetry`` once per
    succeeded run in the window (each of those calls is itself already
    query-bound -- see
    ``tests/integration/test_telemetry_collector.py::test_collector_query_count_does_not_scale_linearly_with_proposal_count``),
    so the total query count scales with the *number of runs*, not with
    however many candidates/proposals/findings/feedback events those
    runs contain. Acceptable at beta scale (a handful of repositories,
    tens of runs a week -- see docs/beta-runbook.md's own note on this);
    batching `collect_review_telemetry` itself across many run ids at
    once would be a real, non-trivial rewrite of an already-tested core
    telemetry function, not the "obviously trivial" improvement this
    milestone's own instructions call for building. Left documented, not
    built, per that instruction."""

    reviewed_a = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/beta-summary-query-bound-a",
        changed_lines=[14],
        response_factory=lambda req: scripted_findings_response([finding_json()]),
        tmp_root=tmp_path / "a",
    )
    reviewed_b = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/beta-summary-query-bound-b",
        changed_lines=[14],
        response_factory=lambda req: scripted_findings_response([finding_json()]),
        tmp_root=tmp_path / "b",
    )
    assert reviewed_a.repository_id != reviewed_b.repository_id

    queries: list[str] = []

    def _count(*_args: object, **_kwargs: object) -> None:
        queries.append("q")

    sync_engine = db_engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", _count)
    try:
        async with session_factory() as session:
            since = datetime.now(UTC) - timedelta(hours=1)
            summary = await compute_beta_summary(session, since=since)
    finally:
        event.remove(sync_engine, "before_cursor_execute", _count)

    assert summary.runs_total == 2
    # A fixed per-run query budget (see the collector's own query-bound
    # test) times 2 runs, plus a small constant for the run-listing and
    # publication-count queries themselves -- never proportional to the
    # findings/proposals/candidates *within* either run.
    assert len(queries) <= 35, queries
