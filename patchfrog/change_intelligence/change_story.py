"""Deterministic PR-level "Change Story" text -- 2-4 sentences,
never a generic file-list summary, never a claim not directly
supported by a :class:`~patchfrog.change_intelligence.domain.ChangeUnit`
or an :class:`~patchfrog.change_intelligence.domain.ExpectedCompanionChange`.

No LLM call is used to phrase this in this milestone (spec: "no extra
provider call solely for summary" is a hard constraint, and this
milestone adds zero new provider calls anywhere) -- purely templated
from already-computed, evidence-backed structures. Never invents intent
("introduces retry policy") the deterministic evidence can't actually
support; states only what the graph/diff evidence shows (what changed,
what kind of change it structurally looks like, what related surface
wasn't touched).
"""

from __future__ import annotations

from patchfrog.change_intelligence.domain import (
    ChangeKind,
    ChangeUnit,
    CompanionStatus,
    ExpectedCompanionChange,
)

_MAX_UNIT_TITLES_LISTED = 3
_MAX_MISSING_COMPANIONS_LISTED = 2

_KIND_LABELS: dict[ChangeKind, str] = {
    ChangeKind.CONTRACT: "cross-module contract",
    ChangeKind.PERSISTENCE: "persistence/schema",
    ChangeKind.CONFIGURATION: "configuration",
    ChangeKind.INFRASTRUCTURE: "infrastructure",
    ChangeKind.TEST: "test",
    ChangeKind.BEHAVIOR: "behavior",
    ChangeKind.MIXED: "mixed",
}


def build_change_story(
    change_units: tuple[ChangeUnit, ...],
    expected_companions: tuple[ExpectedCompanionChange, ...],
) -> str:
    if not change_units:
        return ""

    sentences: list[str] = []

    unit_titles = [u.title for u in change_units[:_MAX_UNIT_TITLES_LISTED]]
    file_count = len({f for u in change_units for f in u.changed_files})
    overview = f"This PR makes {len(change_units)} logical change" + ("s" if len(change_units) != 1 else "")
    overview += f" across {file_count} file" + ("s" if file_count != 1 else "") + ": "
    overview += "; ".join(unit_titles)
    if len(change_units) > _MAX_UNIT_TITLES_LISTED:
        overview += f" (+{len(change_units) - _MAX_UNIT_TITLES_LISTED} more)"
    overview += "."
    sentences.append(overview)

    notable_kinds = {u.change_kind for u in change_units if u.change_kind in (ChangeKind.CONTRACT, ChangeKind.PERSISTENCE)}
    if notable_kinds:
        labels = ", ".join(sorted(_KIND_LABELS[k] for k in notable_kinds))
        sentences.append(f"It includes {labels} surface changes.")

    missing = [c for c in expected_companions if c.status is CompanionStatus.MISSING]
    if missing:
        names = [c.expected_qualified_name for c in missing[:_MAX_MISSING_COMPANIONS_LISTED]]
        suffix = f" (+{len(missing) - _MAX_MISSING_COMPANIONS_LISTED} more)" if len(missing) > _MAX_MISSING_COMPANIONS_LISTED else ""
        plural = "s were" if len(missing) != 1 else " was"
        sentences.append(
            f"{len(missing)} related surface{plural} not touched in this diff: {', '.join(names)}{suffix}."
        )

    return " ".join(sentences[:4])
