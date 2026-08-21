"""Runs the full Phase 8 incremental-review-memory benchmark scenario
suite (:mod:`patchfrog.evaluation.incremental`) against the standard
in-memory SQLite ``session_factory`` fixture -- the committed regression
test for the "unsafe carry-forward must always be zero" invariant."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.evaluation.incremental import run_all_incremental_scenarios


async def test_all_incremental_scenarios_pass_with_zero_unsafe_carry_forward(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    results = await run_all_incremental_scenarios(session_factory, tmp_path_root=tmp_path)
    assert len(results) == 9

    failures = [r for r in results if not r.passed]
    assert not failures, [(r.scenario_id, r.detail) for r in failures]

    unsafe = [r for r in results if r.unsafe_carry_forward]
    assert not unsafe, [(r.scenario_id, r.detail) for r in unsafe]

    scenario_ids = {r.scenario_id for r in results}
    assert scenario_ids == {
        "unrelated_change", "bug_remains_unchanged", "bug_fixed", "evidence_region_changed",
        "symbol_moved", "file_renamed", "function_renamed_ambiguously", "force_push", "base_change",
    }
