"""Deterministic Historical Story prefix -- at most one bounded sentence,
prepended to the existing Change/Contract/Intent/Test Story text, never
a separate publication block (mirrors every other Intelligence
package's own "prefix, not a block" discipline). Only rendered for a
genuinely strong match -- never for every PR with any historical
finding anywhere in the repository (spec section 21: "Do not pollute
every Change Story.")."""

from __future__ import annotations

from patchfrog.historical_regression_memory.domain import (
    HistoricalMatchKind,
    PotentialHistoricalRegression,
)

#: Only these two tiers are strong enough to justify one Change Story
#: sentence -- SAME_FILE/GRAPH_RELATED_SURFACE stay silent here (still
#: available as bounded per-candidate evidence, see :mod:`patchfrog.historical_regression_memory.evidence`).
_STORY_ELIGIBLE_KINDS = frozenset(
    {HistoricalMatchKind.SAME_SYMBOL, HistoricalMatchKind.SAME_QUALIFIED_NAME_IN_SAME_FILE}
)


def build_historical_story_prefix(candidates: tuple[PotentialHistoricalRegression, ...]) -> str:
    strong = [c for c in candidates if c.match_kind in _STORY_ELIGIBLE_KINDS]
    if not strong:
        return ""

    primary = strong[0]
    label = primary.current_qualified_name or primary.current_file_path
    return f"Historical context: {label!r} previously had a trusted, resolved finding."
