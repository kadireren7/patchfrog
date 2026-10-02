"""Lossless JSON round trip for :class:`ExternalChangeEvent` (M11).

A hosted pipeline detects a change in one process and evaluates it per
workspace in others, so the event must cross a process boundary without being
re-derived. Everything M6 consumer mapping needs is preserved, including the
old contract's normalized structure (OpenAPI path matching reads it); a
normalized contract larger than the bound is dropped and the event says so in
its ``notes`` rather than silently degrading.

Nothing here is secret-bearing: an event holds contract structure, versions,
release metadata and digests -- never notes text, credentials or source code.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from patchfrog.dependencies.domain import Ecosystem
from patchfrog.upstream.domain import (
    ChangeClassification,
    ChangeRisk,
    CompatibilityClass,
    ContractDiffItem,
    DependencyRelease,
    DependencyTarget,
    DiffItemKind,
    DiffSubject,
    ExternalChangeEvent,
    ExternalChangeKind,
    ExternalChangeSource,
    ExternalContractRevision,
    SubjectKind,
)

SERIALIZATION_VERSION = 1
MAX_SERIALIZED_CONTRACT_BYTES = 256_000


def _revision(revision: ExternalContractRevision, label: str, notes: list[str]) -> dict[str, Any]:
    normalized: Any = None
    if revision.normalized is not None:
        rendered = json.dumps(revision.normalized, sort_keys=True, separators=(",", ":"), default=str)
        if len(rendered.encode()) <= MAX_SERIALIZED_CONTRACT_BYTES:
            normalized = json.loads(rendered)
        else:
            notes.append(f"{label} contract was too large to carry across processes; consumer mapping may be less precise")
    return {
        "version": revision.version, "fingerprint": revision.fingerprint, "source_ref": revision.source_ref,
        "normalized": normalized, "contract_format": revision.contract_format,
    }


def event_to_json(event: ExternalChangeEvent) -> str:
    notes = list(event.notes)
    old = _revision(event.old, "old", notes)
    new = _revision(event.new, "new", notes)
    target = event.target
    payload = {
        "v": SERIALIZATION_VERSION,
        "target": {
            "provider_key": target.provider_key, "ecosystem": target.ecosystem.value if target.ecosystem else None,
            "package_name": target.package_name, "dependency_key": target.dependency_key,
            "api_hosts": list(target.api_hosts), "modules": list(target.modules), "display_name": target.display_name,
        },
        "source": event.source.value, "kind": event.kind.value, "source_ref": event.source_ref,
        "old": old, "new": new,
        "diff": [
            {
                "kind": i.kind.value, "location": i.location,
                "subject": {"kind": i.subject.kind.value, "method": i.subject.method, "path": i.subject.path,
                            "name": i.subject.name, "member": i.subject.member},
                "compatibility": i.compatibility.value, "old": i.old, "new": i.new, "explanation": i.explanation,
                "evidence": list(i.evidence), "replacement": i.replacement,
            }
            for i in event.diff
        ],
        "classification": {
            "risk": event.classification.risk.value, "compatibility": event.classification.compatibility.value,
            "reasons": list(event.classification.reasons), "counts": dict(event.classification.counts),
            "has_structural_evidence": event.classification.has_structural_evidence,
        },
        "fingerprint": event.fingerprint, "observed_at": event.observed_at.isoformat(),
        "release": (
            {"version": event.release.version, "tag": event.release.tag, "published_at": event.release.published_at,
             "url": event.release.url, "notes_sha256": event.release.notes_sha256, "prerelease": event.release.prerelease}
            if event.release else None
        ),
        "hints_fingerprint": event.hints_fingerprint, "hints_provenance": event.hints_provenance,
        "truncated": event.truncated, "notes": notes, "engine_version": event.engine_version,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _revision_from(data: dict[str, Any]) -> ExternalContractRevision:
    return ExternalContractRevision(
        version=data["version"], fingerprint=data["fingerprint"], source_ref=data["source_ref"],
        normalized=data["normalized"], contract_format=data["contract_format"],
    )


def event_from_json(text: str) -> ExternalChangeEvent:
    data: dict[str, Any] = json.loads(text)
    if data.get("v") != SERIALIZATION_VERSION:
        raise ValueError(f"unsupported event serialization version {data.get('v')!r}")
    t = data["target"]
    target = DependencyTarget(
        provider_key=t["provider_key"], ecosystem=Ecosystem(t["ecosystem"]) if t["ecosystem"] else None,
        package_name=t["package_name"], dependency_key=t["dependency_key"], api_hosts=tuple(t["api_hosts"]),
        modules=tuple(t["modules"]), display_name=t["display_name"],
    )
    diff = tuple(
        ContractDiffItem(
            kind=DiffItemKind(i["kind"]), location=i["location"],
            subject=DiffSubject(
                SubjectKind(i["subject"]["kind"]), method=i["subject"]["method"], path=i["subject"]["path"],
                name=i["subject"]["name"], member=i["subject"]["member"],
            ),
            compatibility=CompatibilityClass(i["compatibility"]), old=i["old"], new=i["new"],
            explanation=i["explanation"], evidence=tuple(i["evidence"]), replacement=i["replacement"],
        )
        for i in data["diff"]
    )
    c = data["classification"]
    release = data["release"]
    return ExternalChangeEvent(
        target=target, source=ExternalChangeSource(data["source"]), kind=ExternalChangeKind(data["kind"]),
        source_ref=data["source_ref"], old=_revision_from(data["old"]), new=_revision_from(data["new"]), diff=diff,
        classification=ChangeClassification(
            risk=ChangeRisk(c["risk"]), compatibility=CompatibilityClass(c["compatibility"]),
            reasons=tuple(c["reasons"]), counts=dict(c["counts"]), has_structural_evidence=c["has_structural_evidence"],
        ),
        fingerprint=data["fingerprint"], observed_at=datetime.fromisoformat(data["observed_at"]),
        release=DependencyRelease(**release) if release else None, hints_fingerprint=data["hints_fingerprint"],
        hints_provenance=data["hints_provenance"], truncated=data["truncated"], notes=tuple(data["notes"]),
        engine_version=data["engine_version"],
    )


__all__ = ["MAX_SERIALIZED_CONTRACT_BYTES", "SERIALIZATION_VERSION", "event_from_json", "event_to_json"]
