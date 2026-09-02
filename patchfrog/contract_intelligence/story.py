"""Deterministic Contract Story addendum -- folded into the existing
Change Story text (spec section 11: "Do NOT create another giant
summary block"), never a separate persisted field or publication
section. No LLM call; states only what the structural delta/blast-
radius evidence directly supports."""

from __future__ import annotations

from patchfrog.change_intelligence.domain import CompanionStatus, ExpectedCompanionChange
from patchfrog.contract_intelligence.domain import ContractDelta

_MAX_NAMES = 2


def build_contract_story(
    deltas: tuple[ContractDelta, ...], stale_consumers: tuple[ExpectedCompanionChange, ...]
) -> str:
    breaking = [d for d in deltas if d.is_potentially_breaking]
    if not breaking:
        return ""

    names = [d.qualified_name for d in breaking[:_MAX_NAMES]]
    suffix = f" (+{len(breaking) - _MAX_NAMES} more)" if len(breaking) > _MAX_NAMES else ""
    plural = "s" if len(breaking) != 1 else ""
    sentence = f"It changes the contract of {', '.join(names)}{suffix} in a way{plural} that may affect callers."

    missing = [c for c in stale_consumers if c.status is CompanionStatus.MISSING]
    if not missing:
        return sentence

    stale_names = sorted({c.expected_qualified_name for c in missing})
    shown = stale_names[:_MAX_NAMES]
    stale_suffix = f" (+{len(stale_names) - _MAX_NAMES} more)" if len(stale_names) > _MAX_NAMES else ""
    plural2 = "s" if len(missing) != 1 else ""
    consumer_sentence = (
        f"{len(missing)} current consumer{plural2} ({', '.join(shown)}{stale_suffix}) "
        "were not touched in this diff and may still assume the old contract."
    )
    return f"{sentence} {consumer_sentence}"
