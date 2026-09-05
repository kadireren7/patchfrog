"""Deterministic Test Story prefix -- one bounded sentence, prepended to
the existing Change/Contract/Intent Story text, never a separate
publication block (mirrors :mod:`patchfrog.intent_verification.story`'s
own "prefix, not a block" discipline). Only rendered when there is at
least one real gap -- never for a clean, well-tested PR."""

from __future__ import annotations

from patchfrog.test_intelligence.domain import PotentialTestGap, TestExpectationReasonCode


def build_test_story_prefix(gaps: tuple[PotentialTestGap, ...]) -> str:
    if not gaps:
        return ""

    no_surface = sum(
        1 for g in gaps if g.expectation.reason_code is TestExpectationReasonCode.NO_TEST_SURFACE_FOUND
    )
    weakened = sum(
        1 for g in gaps if g.expectation.reason_code is TestExpectationReasonCode.TEST_TOUCHED_BUT_WEAKENED
    )

    parts: list[str] = []
    if no_surface:
        parts.append(f"{no_surface} changed symbol(s) with no discoverable test surface")
    if weakened:
        parts.append(f"{weakened} touched test file(s) with a weakened structural test signal")

    return "Test impact: " + "; ".join(parts) + "."
