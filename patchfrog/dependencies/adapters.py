"""External-dependency provider adapters (M5.3).

Every provider sits behind :class:`DependencyProviderAdapter`. Adding a
provider (Anthropic, AWS, Twilio, Slack, ...) is one :class:`ProviderSpec`
entry in :data:`KNOWN_PROVIDER_SPECS` -- no change to discovery, the
registry, the graph or the CLI. A provider with genuinely different
contract mechanics (like OpenAPI) implements the protocol directly.

Upstream change monitoring is deliberately NOT part of this interface
yet (M6).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from patchfrog.dependencies.domain import (
    ESTABLISHING_EVIDENCE,
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
    PackageDeclaration,
    PackageResolution,
)
from patchfrog.dependencies.openapi import fingerprint_normalized
from patchfrog.dependencies.scan import ObservationKind, SourceObservation


@dataclass(frozen=True, slots=True)
class FileScanContext:
    """One scanned file, shared by every adapter (sources are parsed once)."""

    path: str
    language: str  # "python" | "javascript"
    observations: tuple[SourceObservation, ...]
    #: Top-level Python modules defined *inside* the repository -- an
    #: ``import github`` that resolves to the repo's own ``github.py`` is
    #: not the PyGithub SDK.
    local_python_modules: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class PackageIndex:
    declarations: tuple[PackageDeclaration, ...]
    resolutions: tuple[PackageResolution, ...]

    def declared(self, ecosystem: Ecosystem, names: Sequence[str]) -> list[PackageDeclaration]:
        wanted = set(names)
        return [d for d in self.declarations if d.ecosystem is ecosystem and d.name in wanted]

    def resolved(self, ecosystem: Ecosystem, name: str) -> PackageResolution | None:
        return next((r for r in self.resolutions if r.ecosystem is ecosystem and r.name == name), None)


@dataclass(frozen=True, slots=True)
class DependencyDraft:
    """An adapter's normalized dependency *before* discovery attaches
    enclosing symbols to its usage evidence."""

    key: str
    provider: DependencyProvider
    kind: ExternalDependencyKind
    ecosystem: Ecosystem
    package_name: str | None
    version: DependencyVersion
    evidence: tuple[DiscoveryEvidence, ...]
    confidence: DetectionConfidence
    contract: DependencyContract | None
    metadata: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


class DependencyProviderAdapter(Protocol):
    @property
    def provider(self) -> DependencyProvider: ...

    def tracked_modules(self) -> frozenset[str]:
        """Top-level Python/JS module names whose bound names the
        scanners should follow to SDK call chains."""
        ...

    def discover_usage(self, context: FileScanContext) -> list[DiscoveryEvidence]: ...

    def normalize_dependency(
        self, evidence: Sequence[DiscoveryEvidence], packages: PackageIndex
    ) -> list[DependencyDraft]: ...

    def resolve_contract_source(self, draft: DependencyDraft) -> ContractSource | None: ...

    def parse_contract(self, draft: DependencyDraft) -> Mapping[str, Any] | None: ...

    def fingerprint_contract(self, normalized: Mapping[str, Any]) -> ContractFingerprint: ...

    def build_provider_metadata(self, draft: DependencyDraft) -> Mapping[str, str]: ...


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    key: str
    display_name: str
    #: Package names per ecosystem (normalized: PyPI names lower/dashed).
    packages: Mapping[Ecosystem, tuple[str, ...]]
    python_modules: tuple[str, ...] = ()
    js_modules: tuple[str, ...] = ()
    #: Exact API hostnames (a suffix match is never used -- see
    #: ``api.github.com.evil.example``).
    api_hosts: tuple[str, ...] = ()
    env_var_pattern: str | None = None


KNOWN_PROVIDER_SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        key="openai",
        display_name="OpenAI",
        packages={Ecosystem.PYPI: ("openai",), Ecosystem.NPM: ("openai",), Ecosystem.GO: ("github.com/openai/openai-go",)},
        python_modules=("openai",),
        js_modules=("openai",),
        api_hosts=("api.openai.com",),
        env_var_pattern=r"^OPENAI_[A-Z0-9_]+$",
    ),
    ProviderSpec(
        key="stripe",
        display_name="Stripe",
        packages={
            Ecosystem.PYPI: ("stripe",),
            Ecosystem.NPM: ("stripe", "@stripe/stripe-js"),
            Ecosystem.GO: ("github.com/stripe/stripe-go",),
        },
        python_modules=("stripe",),
        js_modules=("stripe", "@stripe/stripe-js"),
        api_hosts=("api.stripe.com", "files.stripe.com"),
        env_var_pattern=r"^STRIPE_[A-Z0-9_]+$",
    ),
    ProviderSpec(
        key="github",
        display_name="GitHub API",
        packages={
            Ecosystem.PYPI: ("pygithub", "githubkit", "ghapi"),
            Ecosystem.NPM: ("@octokit/rest", "@octokit/core", "octokit", "@octokit/graphql"),
            Ecosystem.GO: ("github.com/google/go-github",),
        },
        python_modules=("github", "githubkit", "ghapi"),
        js_modules=("@octokit/rest", "@octokit/core", "octokit", "@octokit/graphql"),
        api_hosts=("api.github.com", "uploads.github.com"),
        env_var_pattern=r"^(GITHUB|GH)_[A-Z0-9_]+$",
    ),
)


def _module_root(module: str, js: bool) -> str:
    if js and module.startswith("@"):
        return "/".join(module.split("/")[:2])
    return module.split("/")[0].split(".")[0]


class KnownProviderAdapter:
    """Declarative adapter for SDK-style providers."""

    def __init__(self, spec: ProviderSpec) -> None:
        self._spec = spec
        self._provider = DependencyProvider(key=spec.key, display_name=spec.display_name)
        self._env_re = re.compile(spec.env_var_pattern) if spec.env_var_pattern else None

    @property
    def provider(self) -> DependencyProvider:
        return self._provider

    @property
    def spec(self) -> ProviderSpec:
        return self._spec

    def tracked_modules(self) -> frozenset[str]:
        return frozenset(self._spec.python_modules) | frozenset(self._spec.js_modules)

    def _modules_for(self, language: str) -> tuple[str, ...]:
        return self._spec.js_modules if language == "javascript" else self._spec.python_modules

    def discover_usage(self, context: FileScanContext) -> list[DiscoveryEvidence]:
        modules = set(self._modules_for(context.language))
        js = context.language == "javascript"
        out: list[DiscoveryEvidence] = []
        for obs in context.observations:
            if obs.kind in (ObservationKind.IMPORT, ObservationKind.CONSTRUCTOR, ObservationKind.CALL):
                root = _module_root(obs.subject, js)
                if root not in modules:
                    continue
                if not js and root in context.local_python_modules:
                    continue  # the repository's own module, not the SDK
                if obs.kind is ObservationKind.IMPORT:
                    out.append(self._evidence(EvidenceType.IMPORT, context.path, obs.line, obs.subject))
                elif obs.kind is ObservationKind.CONSTRUCTOR:
                    out.append(self._evidence(EvidenceType.SDK_CONSTRUCTOR, context.path, obs.line, obs.detail))
                else:
                    out.append(self._evidence(EvidenceType.SDK_CALL, context.path, obs.line, obs.detail))
            elif obs.kind is ObservationKind.URL and obs.subject in self._spec.api_hosts:
                out.append(self._evidence(EvidenceType.HTTP_ENDPOINT, context.path, obs.line, f"{obs.subject}{obs.detail}"))
            elif obs.kind is ObservationKind.ENV_VAR and self._env_re is not None and self._env_re.match(obs.subject):
                out.append(self._evidence(EvidenceType.ENV_VAR_NAME, context.path, obs.line, obs.subject))
        return out

    def _evidence(self, kind: EvidenceType, path: str, line: int | None, token: str) -> DiscoveryEvidence:
        return DiscoveryEvidence(provider_key=self._spec.key, evidence_type=kind, file_path=path, line=line, token=token)

    def normalize_dependency(
        self, evidence: Sequence[DiscoveryEvidence], packages: PackageIndex
    ) -> list[DependencyDraft]:
        by_ecosystem: dict[Ecosystem, list[DiscoveryEvidence]] = {}
        declared_by_ecosystem: dict[Ecosystem, PackageDeclaration] = {}
        for ecosystem, names in self._spec.packages.items():
            declarations = packages.declared(ecosystem, names)
            if declarations:
                declared_by_ecosystem[ecosystem] = declarations[0]
                by_ecosystem.setdefault(ecosystem, []).extend(
                    self._evidence(EvidenceType.MANIFEST_DECLARATION, d.manifest_path, d.line, d.name)
                    for d in declarations
                )
                resolution = packages.resolved(ecosystem, declarations[0].name)
                if resolution is not None:
                    by_ecosystem[ecosystem].append(
                        self._evidence(EvidenceType.LOCKFILE_RESOLUTION, resolution.lockfile_path, None, resolution.name)
                    )
        http: list[DiscoveryEvidence] = []
        env: list[DiscoveryEvidence] = []
        for item in evidence:
            if item.evidence_type is EvidenceType.HTTP_ENDPOINT:
                http.append(item)
            elif item.evidence_type is EvidenceType.ENV_VAR_NAME:
                env.append(item)
            else:
                ecosystem = Ecosystem.NPM if _is_js_path(item.file_path) else Ecosystem.PYPI
                by_ecosystem.setdefault(ecosystem, []).append(item)

        drafts: list[DependencyDraft] = []
        sdk_ecosystems = sorted(by_ecosystem, key=lambda e: e.value)
        for index, ecosystem in enumerate(sdk_ecosystems):
            items = list(by_ecosystem[ecosystem])
            if index == 0:
                items += http + env  # corroborating evidence attaches to one SDK dependency
            declaration = declared_by_ecosystem.get(ecosystem)
            package_name = declaration.name if declaration else self._spec.packages.get(ecosystem, (None,))[0]
            resolution = packages.resolved(ecosystem, package_name) if package_name else None
            version = DependencyVersion(
                declared=declaration.spec if declaration else None,
                resolved=resolution.version if resolution else None,
                declared_in=declaration.manifest_path if declaration else None,
                resolved_in=resolution.lockfile_path if resolution else None,
            )
            drafts.append(self._draft(f"{self._spec.key}:{ecosystem.value}", ExternalDependencyKind.SDK, ecosystem,
                                      package_name, version, items))
        if not drafts and http:
            drafts.append(self._draft(f"{self._spec.key}:http", ExternalDependencyKind.HTTP_API, Ecosystem.NONE,
                                      None, DependencyVersion(), http + env))
        return [d for d in drafts if any(e.evidence_type in ESTABLISHING_EVIDENCE for e in d.evidence)]

    def _draft(
        self,
        key: str,
        kind: ExternalDependencyKind,
        ecosystem: Ecosystem,
        package_name: str | None,
        version: DependencyVersion,
        evidence: list[DiscoveryEvidence],
    ) -> DependencyDraft:
        types = {e.evidence_type for e in evidence}
        declared = bool(types & {EvidenceType.MANIFEST_DECLARATION, EvidenceType.LOCKFILE_RESOLUTION})
        used = bool(types & {EvidenceType.IMPORT, EvidenceType.SDK_CONSTRUCTOR, EvidenceType.SDK_CALL})
        confidence = DetectionConfidence.HIGH if declared and used else DetectionConfidence.MEDIUM
        ordered = tuple(sorted(set(evidence), key=_evidence_order))
        draft = DependencyDraft(
            key=key, provider=self._provider, kind=kind, ecosystem=ecosystem, package_name=package_name,
            version=version, evidence=ordered, confidence=confidence, contract=None,
        )
        source = self.resolve_contract_source(draft)
        normalized = self.parse_contract(draft)
        contract = None
        if source is not None and normalized is not None:
            contract = DependencyContract(
                source=source,
                fingerprint=self.fingerprint_contract(normalized),
                normalized=normalized,
                summary=MappingProxyType({
                    "sdk_calls": len(normalized.get("consumed_surface", ())),
                    "http_endpoints": len(normalized.get("http_endpoints", ())),
                }),
            )
        return DependencyDraft(
            key=draft.key, provider=draft.provider, kind=draft.kind, ecosystem=draft.ecosystem,
            package_name=draft.package_name, version=draft.version, evidence=draft.evidence,
            confidence=draft.confidence, contract=contract, metadata=self.build_provider_metadata(draft),
        )

    def resolve_contract_source(self, draft: DependencyDraft) -> ContractSource | None:
        if draft.kind is ExternalDependencyKind.HTTP_API:
            return ContractSource(ContractSourceType.HTTP_OBSERVED, ",".join(self._spec.api_hosts))
        pinned = draft.version.resolved or draft.version.declared or "unversioned"
        return ContractSource(ContractSourceType.SDK_STATIC, f"{draft.ecosystem.value}:{draft.package_name}@{pinned}")

    def parse_contract(self, draft: DependencyDraft) -> Mapping[str, Any] | None:
        """The *consumed* surface: which SDK entry points / endpoints this
        repository actually calls, at which version -- what an upstream
        change (M6) will be diffed against."""

        return {
            "provider": self._spec.key,
            "ecosystem": draft.ecosystem.value,
            "package": draft.package_name,
            "declared": draft.version.declared,
            "resolved": draft.version.resolved,
            "consumed_surface": sorted(
                {e.token for e in draft.evidence if e.evidence_type in (EvidenceType.SDK_CALL, EvidenceType.SDK_CONSTRUCTOR)}
            ),
            "http_endpoints": sorted({e.token for e in draft.evidence if e.evidence_type is EvidenceType.HTTP_ENDPOINT}),
        }

    def fingerprint_contract(self, normalized: Mapping[str, Any]) -> ContractFingerprint:
        return fingerprint_normalized(normalized)

    def build_provider_metadata(self, draft: DependencyDraft) -> Mapping[str, str]:
        return MappingProxyType(
            {"relationship": "direct_consumer", "api_hosts": ",".join(self._spec.api_hosts)}
        )


def _is_js_path(path: str) -> bool:
    return path.endswith((".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"))


def _evidence_order(item: DiscoveryEvidence) -> tuple[str, int, str, str]:
    return (item.file_path, item.line or 0, item.evidence_type.value, item.token)


def default_provider_adapters() -> tuple[KnownProviderAdapter, ...]:
    return tuple(KnownProviderAdapter(spec) for spec in KNOWN_PROVIDER_SPECS)


__all__ = [
    "KNOWN_PROVIDER_SPECS",
    "DependencyDraft",
    "DependencyProviderAdapter",
    "FileScanContext",
    "KnownProviderAdapter",
    "PackageIndex",
    "ProviderSpec",
    "default_provider_adapters",
]
