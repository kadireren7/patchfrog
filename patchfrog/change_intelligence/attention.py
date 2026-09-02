"""Deterministic, bounded, explainable attention-area derivation.

Never a numeric score (spec section 12) -- a short list of *why*, each
entry naming the concrete structural signal(s) that produced it. Used
as internal review evidence and (optionally) a bounded summary line,
never published as a standalone "high risk" claim with no accompanying
finding.
"""

from __future__ import annotations

from patchfrog.change_intelligence.domain import AttentionArea, ChangeKind, ChangeUnit

#: Never surface more than this many attention areas across a whole
#: report -- bounded, explainable, never a wall of vague warnings.
MAX_ATTENTION_AREAS = 5

#: Conservative, structural (file-path) security-sensitive markers --
#: mirrors the same "path/name evidence, never prose" discipline as
#: patchfrog.change_intelligence.change_kind.
_SECURITY_MARKERS = ("auth", "session", "token", "password", "secret", "crypto")

#: A unit whose affected surface reaches at least this many nodes is
#: called out as "wide fan-out" -- deliberately the same order of
#: magnitude as MAX_AFFECTED_SURFACE_PER_UNIT/5, not an arbitrary guess.
_WIDE_FANOUT_THRESHOLD = 5


def derive_attention_areas(change_units: tuple[ChangeUnit, ...]) -> tuple[AttentionArea, ...]:
    areas: list[AttentionArea] = []
    for unit in change_units:
        signals: list[str] = []

        if unit.change_kind is ChangeKind.CONTRACT:
            signals.append("changed symbol has real cross-file callers (contract-shaped change)")
        if unit.change_kind is ChangeKind.PERSISTENCE:
            signals.append("persistence/schema-shaped code changed")

        if len(unit.affected_surface) >= _WIDE_FANOUT_THRESHOLD:
            signals.append(f"wide fan-out: {len(unit.affected_surface)} dependent symbols/files affected")

        lowered_paths = " ".join(c.file_path.lower() for c in unit.changed_candidates)
        lowered_names = " ".join(
            (c.qualified_name or c.symbol_name or "").lower() for c in unit.changed_candidates
        )
        if any(marker in lowered_paths or marker in lowered_names for marker in _SECURITY_MARKERS):
            signals.append("security-sensitive naming in the changed surface")

        if not signals:
            continue

        areas.append(
            AttentionArea(
                change_unit_id=unit.id,
                label=unit.title,
                signals=tuple(signals),
            )
        )

    return tuple(areas[:MAX_ATTENTION_AREAS])


def attach_missing_companion_signal(
    areas: tuple[AttentionArea, ...], *, change_unit_id: str, missing_count: int
) -> tuple[AttentionArea, ...]:
    """Adds a "N expected companion change(s) not observed" signal to an
    existing area for ``change_unit_id`` if one exists, or creates a new
    bounded area for it otherwise -- kept as a separate, explicit step
    (rather than folded into :func:`derive_attention_areas`) since it
    depends on companion-candidate results computed afterward, in
    :mod:`patchfrog.change_intelligence.service`."""

    if missing_count <= 0:
        return areas
    signal = f"{missing_count} expected companion change(s) not observed"
    updated: list[AttentionArea] = []
    matched = False
    for area in areas:
        if area.change_unit_id == change_unit_id:
            updated.append(AttentionArea(change_unit_id=area.change_unit_id, label=area.label, signals=(*area.signals, signal)))
            matched = True
        else:
            updated.append(area)
    if not matched and len(updated) < MAX_ATTENTION_AREAS:
        updated.append(AttentionArea(change_unit_id=change_unit_id, label=change_unit_id, signals=(signal,)))
    return tuple(updated[:MAX_ATTENTION_AREAS])
