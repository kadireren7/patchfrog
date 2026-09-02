"""Conditional, bounded, deterministic Change Map.

**Eligibility is deterministic, never an LLM judgment call** (spec
section 9: "Do NOT ask an LLM whether to render a diagram."). A diagram
renders only for a PR whose *single most connected* logical change
already reaches a real, evidence-backed graph -- never merged across
multiple, by-construction-disconnected
:class:`~patchfrog.change_intelligence.domain.ChangeUnit`\\ s (grouping
already separated genuinely unrelated changes into different units;
drawing them together in one map would fabricate a connection that
doesn't exist -- see :mod:`patchfrog.change_intelligence.grouping`).
At most one map is ever rendered per report.

Format is a bounded, grouped Markdown bullet list, not an ASCII/Mermaid
node-and-arrow diagram -- deliberately: laying out an arbitrary graph
as 2D ASCII art is itself a source of bugs (overlaps, ambiguous
crossing lines) this milestone has no need to take on, and a grouped
list already satisfies the spec's own semantics requirement (changed /
directly-dependent / indirectly-affected / test / missing, each
labeled) without inventing layout logic. Every line traces back to a
real :class:`~patchfrog.change_intelligence.domain.AffectedSymbolRef`/
:class:`~patchfrog.change_intelligence.domain.ExpectedCompanionChange`
already computed elsewhere in this package -- never a fabricated edge.
"""

from __future__ import annotations

from patchfrog.change_intelligence.domain import (
    MAX_CHANGE_MAP_EDGES,
    MAX_CHANGE_MAP_NODES,
    AffectedRelation,
    ChangeMap,
    ChangeUnit,
    CompanionStatus,
    ExpectedCompanionChange,
)
from patchfrog.publishing.marker import sanitize_untrusted_text

#: A unit needs at least this many *meaningful* (symbol-level, not bare
#: module-region) nodes, spanning at least this many distinct files, to
#: be diagram-eligible -- see the module docstring of
#: :mod:`patchfrog.change_intelligence.domain` and
#: ``docs/change-intelligence.md``'s "Diagram eligibility" section for
#: exactly why these two thresholds (not one) are what separate a
#: genuinely cross-component change from a one-file/one-function edit.
_MIN_ELIGIBLE_NODES = 3
_MIN_ELIGIBLE_FILES = 2

_RELATION_LABELS = {
    AffectedRelation.DIRECTLY_DEPENDENT: "Directly dependent",
    AffectedRelation.INDIRECTLY_AFFECTED: "Indirectly affected",
    AffectedRelation.TEST: "Tests",
}
_MAX_PER_SECTION = 6


def _label(file_path: str, qualified_name: str | None) -> str:
    name = qualified_name or file_path
    clean = sanitize_untrusted_text(name).replace("`", "'")
    path = sanitize_untrusted_text(file_path).replace("`", "'")
    return f"`{clean}` ({path})" if qualified_name else f"`{path}`"


def select_change_map_unit(change_units: tuple[ChangeUnit, ...]) -> ChangeUnit | None:
    """The single unit (if any) eligible for a Change Map -- the most
    node-rich eligible unit, deterministic tie-break by unit id. Never
    more than one unit is ever selected (see module docstring)."""

    eligible: list[tuple[int, ChangeUnit]] = []
    for unit in change_units:
        changed_nodes = {(c.file_path, c.qualified_name) for c in unit.changed_candidates if c.qualified_name}
        affected_nodes = {(a.file_path, a.qualified_name) for a in unit.affected_surface if a.qualified_name}
        all_nodes = changed_nodes | affected_nodes
        distinct_files = {n[0] for n in all_nodes}
        if len(all_nodes) >= _MIN_ELIGIBLE_NODES and len(distinct_files) >= _MIN_ELIGIBLE_FILES:
            eligible.append((len(all_nodes), unit))

    if not eligible:
        return None
    eligible.sort(key=lambda pair: (-pair[0], pair[1].id))
    return eligible[0][1]


def should_render_change_map(change_units: tuple[ChangeUnit, ...]) -> bool:
    return select_change_map_unit(change_units) is not None


def render_change_map(
    unit: ChangeUnit,
    *,
    expected_companions: tuple[ExpectedCompanionChange, ...] = (),
) -> ChangeMap:
    lines: list[str] = ["**Change map** (evidence-grounded, from the repository graph):", ""]
    node_count = 0
    edge_count = 0
    truncated = False
    explicit_omission_noted = False

    changed_labels = sorted({_label(c.file_path, c.qualified_name) for c in unit.changed_candidates})
    shown_changed = changed_labels[:MAX_CHANGE_MAP_NODES]
    if shown_changed:
        lines.append("Changed:")
        lines.extend(f"- {label}" for label in shown_changed)
        node_count += len(shown_changed)
        if len(changed_labels) > len(shown_changed):
            truncated = True
            explicit_omission_noted = True
            lines.append(f"- _(+{len(changed_labels) - len(shown_changed)} more changed)_")
        lines.append("")

    by_relation: dict[AffectedRelation, list[str]] = {}
    for ref in unit.affected_surface:
        if node_count >= MAX_CHANGE_MAP_NODES or edge_count >= MAX_CHANGE_MAP_EDGES:
            truncated = True
            break
        section = by_relation.setdefault(ref.relation, [])
        if len(section) >= _MAX_PER_SECTION:
            truncated = True
            continue
        section.append(f"- {_label(ref.file_path, ref.qualified_name)} -- {sanitize_untrusted_text(ref.reason)}")
        node_count += 1
        edge_count += 1

    for relation in (AffectedRelation.DIRECTLY_DEPENDENT, AffectedRelation.INDIRECTLY_AFFECTED, AffectedRelation.TEST):
        section_lines = by_relation.get(relation)
        if not section_lines:
            continue
        lines.append(f"{_RELATION_LABELS[relation]}:")
        lines.extend(section_lines)
        lines.append("")

    missing = [c for c in expected_companions if c.change_unit_id == unit.id and c.status is CompanionStatus.MISSING]
    shown_missing = missing[:_MAX_PER_SECTION]
    if shown_missing:
        lines.append("Expected but missing:")
        for c in shown_missing:
            lines.append(f"- {_label(c.expected_file_path, c.expected_qualified_name)} -- {sanitize_untrusted_text(c.reason)}")
            edge_count += 1
        if len(missing) > len(shown_missing):
            truncated = True
            explicit_omission_noted = True
            lines.append(f"- _(+{len(missing) - len(shown_missing)} more)_")
        lines.append("")

    if truncated and not explicit_omission_noted:
        lines.append("_(bounded -- some affected nodes/edges were omitted)_")

    text = "\n".join(lines).rstrip()
    return ChangeMap(text=text, node_count=node_count, edge_count=edge_count, truncated=truncated)
