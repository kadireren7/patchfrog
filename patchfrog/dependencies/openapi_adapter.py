"""Generic OpenAPI adapter (M5.4) -- one dependency per local spec.

An OpenAPI document may describe an external SaaS API, an internal
company service, or an SDK-generated client's contract; PatchFrog treats
all three identically: the spec *is* the contract. Usage sites are
string literals in source that match one of the spec's paths (two or
more segments, so ``"/"`` or ``"/users"`` alone never match), calls to
one of its server hosts, and generated-client markers.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from patchfrog.dependencies.adapters import DependencyDraft, FileScanContext, PackageIndex
from patchfrog.dependencies.domain import (
    ContractFingerprint,
    ContractSource,
    ContractSourceType,
    DependencyContract,
    DependencyProvider,
    DependencyVersion,
    DetectionConfidence,
    DiscoveryEvidence,
    Ecosystem,
    EvidenceType,
    ExternalDependencyKind,
)
from patchfrog.dependencies.openapi import (
    fingerprint_normalized,
    normalize_spec,
    path_pattern,
    server_hosts,
    spec_title,
    summarize,
)
from patchfrog.dependencies.scan import ObservationKind

OPENAPI_PROVIDER = DependencyProvider(key="openapi", display_name="OpenAPI contract")


@dataclass(frozen=True, slots=True)
class LocalSpec:
    path: str
    document: Mapping[str, Any]


class OpenAPIAdapter:
    def __init__(self, specs: Sequence[LocalSpec] = (), *, generated_client_paths: Sequence[str] = ()) -> None:
        self._specs = tuple(sorted(specs, key=lambda s: s.path))
        self._generated = tuple(sorted(generated_client_paths))
        self._normalized = {s.path: normalize_spec(s.document) for s in self._specs}
        self._hosts = {s.path: server_hosts(s.document) for s in self._specs}
        self._patterns: dict[str, list[re.Pattern[str]]] = {
            s.path: [path_pattern(p) for p in self._normalized[s.path]["paths"] if p.strip("/").count("/") >= 1]
            for s in self._specs
        }

    @property
    def provider(self) -> DependencyProvider:
        return OPENAPI_PROVIDER

    def tracked_modules(self) -> frozenset[str]:
        return frozenset()

    def discover_usage(self, context: FileScanContext) -> list[DiscoveryEvidence]:
        out: list[DiscoveryEvidence] = []
        for spec in self._specs:
            for obs in context.observations:
                if obs.kind is ObservationKind.STRING_PATH and any(p.match(obs.subject) for p in self._patterns[spec.path]):
                    out.append(self._evidence(spec.path, EvidenceType.OPENAPI_PATH_REFERENCE, context.path, obs.line,
                                              obs.subject))
                elif obs.kind is ObservationKind.URL and obs.subject in self._hosts[spec.path]:
                    out.append(self._evidence(spec.path, EvidenceType.HTTP_ENDPOINT, context.path, obs.line,
                                              f"{obs.subject}{obs.detail}"))
        return out

    @staticmethod
    def _evidence(spec_path: str, kind: EvidenceType, path: str, line: int | None, token: str) -> DiscoveryEvidence:
        return DiscoveryEvidence(provider_key=f"openapi:{spec_path}", evidence_type=kind, file_path=path, line=line,
                                 token=token)

    def normalize_dependency(
        self, evidence: Sequence[DiscoveryEvidence], packages: PackageIndex
    ) -> list[DependencyDraft]:
        drafts: list[DependencyDraft] = []
        for spec in self._specs:
            key = f"openapi:{spec.path}"
            items = [self._evidence(spec.path, EvidenceType.OPENAPI_SPEC, spec.path, None, spec.path)]
            items += [
                self._evidence(spec.path, EvidenceType.OPENAPI_SERVER_HOST, spec.path, None, host)
                for host in self._hosts[spec.path]
            ]
            items += [e for e in evidence if e.provider_key == key]
            if len(self._specs) == 1:
                items += [
                    self._evidence(spec.path, EvidenceType.GENERATED_CLIENT, marker, None, marker)
                    for marker in self._generated
                ]
            info = spec.document.get("info")
            api_version = str(info.get("version"))[:64] if isinstance(info, Mapping) and info.get("version") else None
            draft = DependencyDraft(
                key=key,
                provider=DependencyProvider(key="openapi", display_name=spec_title(spec.document) or spec.path),
                kind=ExternalDependencyKind.OPENAPI_CONTRACT,
                ecosystem=Ecosystem.OPENAPI,
                package_name=None,
                version=DependencyVersion(declared=api_version, declared_in=spec.path),
                evidence=tuple(sorted(set(items), key=lambda e: (e.file_path, e.line or 0, e.evidence_type.value, e.token))),
                confidence=DetectionConfidence.HIGH,
                contract=None,
            )
            normalized = self.parse_contract(draft)
            source = self.resolve_contract_source(draft)
            contract = (
                DependencyContract(
                    source=source,
                    fingerprint=self.fingerprint_contract(normalized),
                    normalized=normalized,
                    summary=MappingProxyType(summarize(normalized)),
                )
                if normalized is not None and source is not None
                else None
            )
            drafts.append(
                DependencyDraft(
                    key=draft.key, provider=draft.provider, kind=draft.kind, ecosystem=draft.ecosystem,
                    package_name=None, version=draft.version, evidence=draft.evidence, confidence=draft.confidence,
                    contract=contract, metadata=self.build_provider_metadata(draft),
                )
            )
        if not self._specs and self._generated:
            drafts.append(
                DependencyDraft(
                    key="openapi:generated-client",
                    provider=DependencyProvider(key="openapi", display_name="OpenAPI generated client"),
                    kind=ExternalDependencyKind.OPENAPI_CONTRACT,
                    ecosystem=Ecosystem.OPENAPI,
                    package_name=None,
                    version=DependencyVersion(),
                    evidence=tuple(
                        DiscoveryEvidence("openapi:generated-client", EvidenceType.GENERATED_CLIENT, m, None, m)
                        for m in self._generated
                    ),
                    confidence=DetectionConfidence.MEDIUM,
                    contract=None,
                    metadata=MappingProxyType({"relationship": "generated_client_without_local_spec"}),
                )
            )
        return drafts

    def resolve_contract_source(self, draft: DependencyDraft) -> ContractSource | None:
        spec_path = draft.key.removeprefix("openapi:")
        return ContractSource(ContractSourceType.OPENAPI_LOCAL, spec_path) if spec_path in self._normalized else None

    def parse_contract(self, draft: DependencyDraft) -> Mapping[str, Any] | None:
        return self._normalized.get(draft.key.removeprefix("openapi:"))

    def fingerprint_contract(self, normalized: Mapping[str, Any]) -> ContractFingerprint:
        return fingerprint_normalized(normalized)

    def build_provider_metadata(self, draft: DependencyDraft) -> Mapping[str, str]:
        consumed = any(
            e.evidence_type in (EvidenceType.OPENAPI_PATH_REFERENCE, EvidenceType.HTTP_ENDPOINT) for e in draft.evidence
        )
        spec_path = draft.key.removeprefix("openapi:")
        return MappingProxyType(
            {
                "relationship": "consumed_in_repository" if consumed else "defined_in_repository",
                "server_hosts": ",".join(self._hosts.get(spec_path, ())),
            }
        )


__all__ = ["OPENAPI_PROVIDER", "LocalSpec", "OpenAPIAdapter"]
