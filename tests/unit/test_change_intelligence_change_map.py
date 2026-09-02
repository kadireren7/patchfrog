"""Unit coverage for :mod:`patchfrog.change_intelligence.change_map` --
deterministic diagram eligibility (spec section 17's mandatory "diagram
spam prevention" cases) and bounded rendering. Pure/synchronous, no
database, no LLM -- every case is a hand-built
:class:`~patchfrog.change_intelligence.domain.ChangeUnit`.
"""

from __future__ import annotations

import uuid

from patchfrog.change_intelligence.change_map import (
    render_change_map,
    select_change_map_unit,
    should_render_change_map,
)
from patchfrog.change_intelligence.domain import (
    MAX_CHANGE_MAP_EDGES,
    MAX_CHANGE_MAP_NODES,
    AffectedRelation,
    AffectedSymbolRef,
    ChangeKind,
    ChangeUnit,
)
from patchfrog.review.domain import ReviewCandidate, ReviewCandidateReason


def _candidate(
    *,
    file_path: str = "src/a.py",
    symbol_id: uuid.UUID | None = None,
    qualified_name: str | None = "a.func",
    symbol_name: str | None = "func",
    line: int = 10,
) -> ReviewCandidate:
    return ReviewCandidate(
        file_path=file_path,
        symbol_id=symbol_id if symbol_id is not None else uuid.uuid4(),
        symbol_name=symbol_name,
        qualified_name=qualified_name,
        start_line=line,
        end_line=line + 5,
        changed_lines=(line,),
        static_finding_ids=(),
        reason=ReviewCandidateReason.CHANGED_SYMBOL,
    )


def _module_region_candidate(*, file_path: str) -> ReviewCandidate:
    return ReviewCandidate(
        file_path=file_path,
        symbol_id=None,
        symbol_name=None,
        qualified_name=None,
        start_line=1,
        end_line=10,
        changed_lines=(1, 2, 3),
        static_finding_ids=(),
        reason=ReviewCandidateReason.CHANGED_MODULE_REGION,
    )


def _affected(
    *, file_path: str, qualified_name: str, relation: AffectedRelation = AffectedRelation.DIRECTLY_DEPENDENT, distance: int = 1
) -> AffectedSymbolRef:
    return AffectedSymbolRef(
        file_path=file_path,
        qualified_name=qualified_name,
        symbol_name=qualified_name.rsplit(".", 1)[-1],
        relation=relation,
        distance=distance,
        reason=f"directly calls or is called by a changed symbol ({qualified_name!r})",
    )


def _unit(
    *,
    changed: tuple[ReviewCandidate, ...],
    affected: tuple[AffectedSymbolRef, ...] = (),
    kind: ChangeKind = ChangeKind.BEHAVIOR,
) -> ChangeUnit:
    return ChangeUnit(id=uuid.uuid4().hex[:16], title="unit", change_kind=kind, changed_candidates=changed, affected_surface=affected)


# -- Mandatory NOT-eligible cases (spec section 17) --------------------


def test_docs_only_pr_no_diagram() -> None:
    """A docs-only PR produces only module-region candidates (no
    parser extracts symbols from Markdown) -- zero symbol-level nodes."""

    unit = _unit(changed=(_module_region_candidate(file_path="docs/readme.md"),))
    assert should_render_change_map((unit,)) is False


def test_isolated_one_function_fix_no_diagram() -> None:
    """One changed function, no resolvable callers/callees -- 1 node,
    below the minimum-node threshold."""

    unit = _unit(changed=(_candidate(qualified_name="mod.leaf_helper"),), affected=())
    assert should_render_change_map((unit,)) is False


def test_simple_rename_no_diagram() -> None:
    """A narrow rename touching the definition and at most one same-file
    caller -- still below the minimum node/file thresholds."""

    unit = _unit(
        changed=(_candidate(file_path="src/a.py", qualified_name="a.old_name", line=5),),
        affected=(_affected(file_path="src/a.py", qualified_name="a.caller_in_same_file"),),
    )
    assert should_render_change_map((unit,)) is False


def test_one_file_leaf_helper_change_no_diagram() -> None:
    """Multiple changed symbols but all confined to one file -- fails
    the >=2-distinct-files requirement even with >=3 nodes."""

    unit = _unit(
        changed=(
            _candidate(file_path="src/a.py", qualified_name="a.helper_one", line=5),
            _candidate(file_path="src/a.py", qualified_name="a.helper_two", line=20),
            _candidate(file_path="src/a.py", qualified_name="a.helper_three", line=35),
        ),
    )
    assert should_render_change_map((unit,)) is False


def test_formatting_only_change_no_diagram() -> None:
    """A pure module-region (whitespace/formatting) change -- no symbol
    nodes at all."""

    unit = _unit(changed=(_module_region_candidate(file_path="src/a.py"),))
    assert should_render_change_map((unit,)) is False


def test_trivial_one_file_patch_no_diagram() -> None:
    unit = _unit(changed=(_candidate(file_path="src/a.py", qualified_name="a.tiny_fix"),))
    assert should_render_change_map((unit,)) is False


def test_graph_adds_no_information_beyond_changed_symbol_no_diagram() -> None:
    """Three changed symbols, but no affected surface and all in one
    file -- the graph contributes nothing beyond "these were edited"."""

    unit = _unit(
        changed=(
            _candidate(file_path="src/a.py", qualified_name="a.x", line=1),
            _candidate(file_path="src/a.py", qualified_name="a.y", line=10),
        ),
        affected=(),
    )
    assert should_render_change_map((unit,)) is False


def test_disconnected_unrelated_changes_never_produce_one_misleading_diagram() -> None:
    """Two genuinely disconnected ChangeUnits (by construction -- no
    shared graph evidence) must never be merged into one diagram; each
    is evaluated independently, and only a unit that independently
    qualifies renders -- never a fabricated cross-unit connection."""

    small_unit_a = _unit(changed=(_candidate(file_path="src/a.py", qualified_name="a.x"),))
    small_unit_b = _unit(changed=(_candidate(file_path="src/b.py", qualified_name="b.y"),))
    assert should_render_change_map((small_unit_a, small_unit_b)) is False

    # Even when one of the two independently qualifies, the map is for
    # THAT unit alone -- never a combined rendering of both.
    qualifying_unit = _unit(
        changed=(_candidate(file_path="src/c.py", qualified_name="c.controller", line=1),),
        affected=(
            _affected(file_path="src/d.py", qualified_name="d.service"),
            _affected(file_path="src/e.py", qualified_name="e.repo", relation=AffectedRelation.INDIRECTLY_AFFECTED, distance=2),
        ),
    )
    selected = select_change_map_unit((small_unit_a, qualifying_unit, small_unit_b))
    assert selected is qualifying_unit
    change_map = render_change_map(selected, expected_companions=())
    assert "a.x" not in change_map.text
    assert "b.y" not in change_map.text


# -- Mandatory eligible cases (spec section 17) -------------------------


def test_api_service_repository_change_renders_diagram() -> None:
    unit = _unit(
        changed=(_candidate(file_path="src/api.py", qualified_name="api.Controller.handle", line=1),),
        affected=(
            _affected(file_path="src/service.py", qualified_name="service.Service.process"),
            _affected(file_path="src/repository.py", qualified_name="repo.Repository.save", relation=AffectedRelation.INDIRECTLY_AFFECTED, distance=2),
        ),
    )
    assert should_render_change_map((unit,)) is True
    change_map = render_change_map(unit, expected_companions=())
    assert "Controller.handle" in change_map.text
    assert "Service.process" in change_map.text
    assert "Repository.save" in change_map.text


def test_worker_service_persistence_change_renders_diagram() -> None:
    unit = _unit(
        changed=(_candidate(file_path="src/worker.py", qualified_name="worker.Worker.run", line=1),),
        affected=(
            _affected(file_path="src/service.py", qualified_name="service.Service.handle"),
            _affected(file_path="src/persistence.py", qualified_name="persistence.Store.write", relation=AffectedRelation.INDIRECTLY_AFFECTED, distance=2),
        ),
        kind=ChangeKind.PERSISTENCE,
    )
    assert should_render_change_map((unit,)) is True


def test_schema_serializer_consumer_change_renders_diagram() -> None:
    unit = _unit(
        changed=(_candidate(file_path="src/schema.py", qualified_name="schema.Schema.field", line=1),),
        affected=(
            _affected(file_path="src/serializer.py", qualified_name="serializer.Serializer.dump"),
            _affected(file_path="src/consumer.py", qualified_name="consumer.Consumer.read", relation=AffectedRelation.INDIRECTLY_AFFECTED, distance=2),
        ),
    )
    assert should_render_change_map((unit,)) is True


def test_huge_graph_produces_a_bounded_diagram() -> None:
    changed = tuple(_candidate(file_path=f"src/f{i}.py", qualified_name=f"f{i}.func", line=1) for i in range(5))
    affected = tuple(
        _affected(file_path=f"src/dep{i}.py", qualified_name=f"dep{i}.thing") for i in range(40)
    )
    unit = _unit(changed=changed, affected=affected)
    assert should_render_change_map((unit,)) is True
    change_map = render_change_map(unit, expected_companions=())
    assert change_map.node_count <= MAX_CHANGE_MAP_NODES
    assert change_map.edge_count <= MAX_CHANGE_MAP_EDGES
    assert change_map.truncated is True
    assert "more" in change_map.text or "bounded" in change_map.text
