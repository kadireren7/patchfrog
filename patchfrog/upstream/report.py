"""Machine-readable (JSON) and human-readable views of M6 results.

The same dictionaries back ``--json`` CLI output and the bounded JSON
columns in :mod:`patchfrog.upstream.store`, so what is printed and what
is persisted never drift apart.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from patchfrog.upstream.blast_radius import BlastRadius, blast_radius_to_dict
from patchfrog.upstream.consumers import AffectedConsumer, ConsumerImpact
from patchfrog.upstream.domain import CompatibilityClass, ContractDiffItem, ExternalChangeEvent
from patchfrog.upstream.workspace import RepositoryImpact, WorkspaceImpact

MAX_REPORTED_CONSUMERS = 200


def diff_item_to_dict(item: ContractDiffItem) -> dict[str, Any]:
    return {
        "key": item.key,
        "kind": item.kind.value,
        "location": item.location,
        "subject": item.subject.as_dict(),
        "compatibility": item.compatibility.value,
        "old": item.old,
        "new": item.new,
        "explanation": item.explanation,
        "evidence": list(item.evidence),
        "replacement": item.replacement,
    }


def event_to_dict(event: ExternalChangeEvent) -> dict[str, Any]:
    return {
        "fingerprint": event.fingerprint,
        "engine_version": event.engine_version,
        "target": {**event.target.identity(), "label": event.target.label, "modules": list(event.target.modules)},
        "source": event.source.value,
        "kind": event.kind.value,
        "source_ref": event.source_ref,
        "old": {"version": event.old.version, "contract_fingerprint": event.old.fingerprint,
                "format": event.old.contract_format},
        "new": {"version": event.new.version, "contract_fingerprint": event.new.fingerprint,
                "format": event.new.contract_format},
        "release": asdict(event.release) if event.release else None,
        "classification": {
            "risk": event.classification.risk.value,
            "compatibility": event.classification.compatibility.value,
            "reasons": list(event.classification.reasons),
            "counts": dict(event.classification.counts),
            "has_structural_evidence": event.classification.has_structural_evidence,
        },
        "hints": {"fingerprint": event.hints_fingerprint, "provenance": event.hints_provenance},
        "diff": [diff_item_to_dict(i) for i in event.diff],
        "truncated": event.truncated,
        "notes": list(event.notes),
        "observed_at": event.observed_at.isoformat(),
    }


def consumer_to_dict(consumer: AffectedConsumer) -> dict[str, Any]:
    return {
        "repository": consumer.repository,
        "dependency": consumer.dependency_key,
        "file": consumer.file_path,
        "symbol": consumer.symbol,
        "line": consumer.site.line,
        "usage": {"evidence": consumer.site.evidence_type.value, "token": consumer.site.token},
        "match": [t.value for t in consumer.match_types],
        "confidence": consumer.confidence.value,
        "impact": consumer.impact.value,
        "diff_items": list(consumer.diff_item_keys),
        "reasons": list(consumer.reasons),
    }


def consumer_impact_to_dict(impact: ConsumerImpact) -> dict[str, Any]:
    return {
        "dependency": impact.dependency_key,
        "match_reason": impact.match_reason,
        "affected": [consumer_to_dict(c) for c in impact.affected[:MAX_REPORTED_CONSUMERS]],
        "ruled_out": sorted({s.location for s in impact.ruled_out_sites}),
        "unaffected": list(impact.unaffected_locations),
        "unaffected_site_count": len(impact.unaffected_sites),
        "unmapped_diff_items": list(impact.unmapped_item_keys),
        "notes": list(impact.notes),
    }


def repository_impact_to_dict(impact: RepositoryImpact) -> dict[str, Any]:
    radii: Sequence[BlastRadius] = impact.blast_radii
    return {
        "repository": impact.repository,
        "commit_sha": impact.commit_sha,
        "status": impact.status.value,
        "reason": impact.reason,
        "matched_dependencies": [
            {"key": m.identity.key, "reason": m.reason, "current_version": m.identity.current_version}
            for m in impact.matches
        ],
        "consumers": [consumer_impact_to_dict(i) for i in impact.consumer_impacts],
        "blast_radius": [blast_radius_to_dict(r) for r in radii],
        "notes": list(impact.notes),
    }


def workspace_to_dict(workspace: WorkspaceImpact) -> dict[str, Any]:
    return {
        "affected": [r.repository for r in workspace.affected],
        "uncertain": [r.repository for r in workspace.uncertain],
        "unaffected": [r.repository for r in workspace.unaffected],
        "repositories": [repository_impact_to_dict(r) for r in workspace.repositories],
    }


# -- text ----------------------------------------------------------------------------


def render_event_text(event: ExternalChangeEvent, *, show_non_breaking: bool = False) -> str:
    lines = [
        f"Upstream change: {event.target.label} ({event.source_ref})",
        f"  risk: {event.classification.risk.value.upper()}  "
        f"[{', '.join(event.classification.reasons) or 'no reasons'}]",
    ]
    if event.release is not None:
        lines.append(f"  release: {event.release.tag or event.release.version}"
                     + (f" published {event.release.published_at}" if event.release.published_at else ""))
    if event.hints_provenance:
        lines.append(f"  hints: {event.hints_provenance}")
    groups = (
        ("Breaking changes", CompatibilityClass.BREAKING),
        ("Potentially breaking", CompatibilityClass.POTENTIALLY_BREAKING),
        ("Unknown compatibility", CompatibilityClass.UNKNOWN),
    ) + ((("Non-breaking", CompatibilityClass.NON_BREAKING),) if show_non_breaking else ())
    for title, compatibility in groups:
        items = [i for i in event.diff if i.compatibility is compatibility]
        if not items:
            continue
        lines.append(f"{title}:")
        lines.extend(f"  - {i.subject.describe()}: {i.explanation}" for i in items)
    hidden = sum(1 for i in event.diff if i.compatibility is CompatibilityClass.NON_BREAKING)
    if hidden and not show_non_breaking:
        lines.append(f"({hidden} non-breaking change(s) not shown)")
    for note in event.notes:
        lines.append(f"note: {note}")
    return "\n".join(lines) + "\n"


def render_repository_text(impact: RepositoryImpact) -> str:
    lines = [f"Repository {impact.repository}: {impact.status.value.upper()} -- {impact.reason}"]
    for consumer_impact in impact.consumer_impacts:
        direct = consumer_impact.direct
        potential = consumer_impact.potential
        lines.append(f"  dependency {consumer_impact.dependency_key} (matched by {consumer_impact.match_reason})")
        if direct:
            lines.append("  Affected consumers:")
            lines.extend(
                f"    - {c.location} (line {c.site.line}, {c.confidence.value}): {c.reasons[0]}" for c in direct
            )
        if potential:
            lines.append("  Potentially affected:")
            lines.extend(f"    - {c.location} ({c.confidence.value}): {c.reasons[0]}" for c in potential)
        if consumer_impact.ruled_out_sites:
            lines.append("  Ruled out (call does not use the changed argument):")
            lines.extend(f"    - {loc}" for loc in sorted({s.location for s in consumer_impact.ruled_out_sites}))
        unaffected = consumer_impact.unaffected_locations
        if unaffected:
            lines.append(f"  Unaffected usages ignored: {len(unaffected)}")
        for note in consumer_impact.notes:
            lines.append(f"  note: {note}")
    for radius in impact.blast_radii:
        summary = radius.summary()
        lines.append(
            f"  Blast radius: {summary['direct']} direct, {summary['transitive']} transitive, "
            f"{summary['potential']} potential, {summary['related_tests']} related test file(s), "
            f"{summary['modules']} module(s)"
        )
        for node in radius.transitive:
            lines.append(f"    ~ {node.key} (depth {node.depth}, {node.confidence.value})")
        for test in radius.related_tests:
            lines.append(f"    t {test.file_path}: {test.reason}")
        if radius.files_without_call_graph:
            lines.append(f"    (no call graph for: {', '.join(radius.files_without_call_graph)} -- callers not traced)")
    for note in impact.notes:
        lines.append(f"  note: {note}")
    return "\n".join(lines) + "\n"


def render_workspace_text(workspace: WorkspaceImpact) -> str:
    lines = [
        f"Repositories: {len(workspace.affected)} affected, {len(workspace.uncertain)} uncertain, "
        f"{len(workspace.unaffected)} unaffected",
    ]
    text = "\n".join(lines) + "\n"
    return text + "".join(render_repository_text(r) for r in workspace.repositories)


__all__ = [
    "consumer_impact_to_dict",
    "consumer_to_dict",
    "diff_item_to_dict",
    "event_to_dict",
    "render_event_text",
    "render_repository_text",
    "render_workspace_text",
    "repository_impact_to_dict",
    "workspace_to_dict",
]
