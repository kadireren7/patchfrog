"""Top-level Historical Regression Memory orchestrator.

:func:`build_historical_regression_report` is the one entry point
everything else in this package composes into. Called once per review
run, after Test Intelligence (consuming Change/Contract/Intent/Test
Intelligence's already-built evidence directly, plus the one bounded
trust query this package adds) -- see
:mod:`patchfrog.review.service`'s integration point.

The only async/session-dependent module in this package -- historical
evidence lives in the database, not in this run's own in-memory
objects (unlike J/K/L/M's own matching logic, which is pure). Zero LLM
calls.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.change_intelligence.domain import ChangeUnit, ExpectedCompanionChange
from patchfrog.contract_intelligence.domain import ContractDelta
from patchfrog.historical_regression_memory.domain import (
    HISTORICAL_REGRESSION_MEMORY_VERSION,
    HistoricalRegressionReport,
)
from patchfrog.historical_regression_memory.matching import derive_historical_regression_candidates
from patchfrog.historical_regression_memory.queries import fetch_trusted_historical_records
from patchfrog.historical_regression_memory.story import build_historical_story_prefix
from patchfrog.intent_verification.domain import PotentialIntentGap
from patchfrog.test_intelligence.domain import PotentialTestGap


async def build_historical_regression_report(
    session: AsyncSession,
    *,
    repository_id: uuid.UUID,
    change_units: tuple[ChangeUnit, ...] = (),
    contract_deltas: tuple[ContractDelta, ...] = (),
    intent_gaps: tuple[PotentialIntentGap, ...] = (),
    test_gaps: tuple[PotentialTestGap, ...] = (),
    expected_companions: tuple[ExpectedCompanionChange, ...] = (),
) -> HistoricalRegressionReport:
    trusted_records = await fetch_trusted_historical_records(session, repository_id=repository_id)
    if not trusted_records:
        return HistoricalRegressionReport(
            version=HISTORICAL_REGRESSION_MEMORY_VERSION, trusted_records_considered=(), candidates=(),
            historical_story="",
        )

    candidates = derive_historical_regression_candidates(
        trusted_records=trusted_records,
        change_units=change_units,
        contract_deltas=contract_deltas,
        intent_gaps=intent_gaps,
        test_gaps=test_gaps,
        expected_companions=expected_companions,
    )
    story = build_historical_story_prefix(candidates)

    return HistoricalRegressionReport(
        version=HISTORICAL_REGRESSION_MEMORY_VERSION,
        trusted_records_considered=trusted_records,
        candidates=candidates,
        historical_story=story,
    )
