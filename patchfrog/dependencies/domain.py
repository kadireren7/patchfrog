"""Provider-agnostic domain model for external API/SDK dependencies (M5).

An :class:`ExternalDependency` is something the repository's code relies
on that PatchFrog does not own: an SDK (OpenAI, Stripe, GitHub, ...), a
directly-called HTTP API, an OpenAPI-described contract, or a plain
package. It is built only from static repository evidence -- manifests,
lockfiles, imports, SDK symbols, endpoint hostnames, environment
variable *names*, OpenAPI documents -- never from runtime state.

**Secret-value discipline.** Every string stored in this model is a
controlled token extracted by a deterministic rule: a package/module
name, an identifier chain, a hostname (plus a sanitized path for known
API hosts), an environment variable *name*, or a file path. Source
lines, literal values, environment variable values, URL credentials and
query strings are never captured. Files that look like secret stores
(``.env*``, key files, credential files) are never opened at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

#: Bumped when discovery/normalization semantics change such that a
#: previously-recorded inventory is no longer comparable.
DEPENDENCY_DISCOVERY_VERSION = 1
#: Bumped when the canonical normalized-contract shape (and therefore
#: every fingerprint) changes.
CONTRACT_NORMALIZATION_VERSION = 1


class ExternalDependencyKind(StrEnum):
    SDK = "sdk"
    HTTP_API = "http_api"
    OPENAPI_CONTRACT = "openapi_contract"
    PACKAGE = "package"


class Ecosystem(StrEnum):
    PYPI = "pypi"
    NPM = "npm"
    GO = "go"
    OPENAPI = "openapi"
    NONE = "none"


class EvidenceType(StrEnum):
    MANIFEST_DECLARATION = "manifest_declaration"
    LOCKFILE_RESOLUTION = "lockfile_resolution"
    IMPORT = "import"
    SDK_CONSTRUCTOR = "sdk_constructor"
    SDK_CALL = "sdk_call"
    HTTP_ENDPOINT = "http_endpoint"
    ENV_VAR_NAME = "env_var_name"
    OPENAPI_SPEC = "openapi_spec"
    OPENAPI_SERVER_HOST = "openapi_server_host"
    OPENAPI_PATH_REFERENCE = "openapi_path_reference"
    GENERATED_CLIENT = "generated_client"


#: Evidence that can establish a dependency on its own. Environment
#: variable names only ever *corroborate* -- a lone ``GITHUB_TOKEN`` in a
#: CI helper is not proof the code calls GitHub.
ESTABLISHING_EVIDENCE: frozenset[EvidenceType] = frozenset(
    {
        EvidenceType.MANIFEST_DECLARATION,
        EvidenceType.LOCKFILE_RESOLUTION,
        EvidenceType.IMPORT,
        EvidenceType.SDK_CONSTRUCTOR,
        EvidenceType.SDK_CALL,
        EvidenceType.HTTP_ENDPOINT,
        EvidenceType.OPENAPI_SPEC,
        EvidenceType.GENERATED_CLIENT,
    }
)

#: Evidence that represents a place the code actually *uses* the
#: dependency (a usage site), as opposed to declaring it.
USAGE_EVIDENCE: frozenset[EvidenceType] = frozenset(
    {
        EvidenceType.IMPORT,
        EvidenceType.SDK_CONSTRUCTOR,
        EvidenceType.SDK_CALL,
        EvidenceType.HTTP_ENDPOINT,
        EvidenceType.ENV_VAR_NAME,
        EvidenceType.OPENAPI_PATH_REFERENCE,
        EvidenceType.GENERATED_CLIENT,
    }
)


class DetectionConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ContractSourceType(StrEnum):
    #: The consumed surface of an SDK, derived statically from usage
    #: (package, version, SDK call chains) -- no upstream fetch.
    SDK_STATIC = "sdk/static"
    #: A repository-local OpenAPI/Swagger document.
    OPENAPI_LOCAL = "openapi/local"
    #: Directly-called HTTP endpoints observed in source.
    HTTP_OBSERVED = "http/observed"
    #: Declared/resolved package version only.
    PACKAGE_MANIFEST = "package/manifest"


@dataclass(frozen=True, slots=True)
class DependencyProvider:
    """``key`` is stable and machine-facing (``"openai"``);
    ``display_name`` is for humans (``"OpenAI"``)."""

    key: str
    display_name: str


@dataclass(frozen=True, slots=True)
class DependencyVersion:
    declared: str | None = None
    resolved: str | None = None
    declared_in: str | None = None
    resolved_in: str | None = None

    @property
    def display(self) -> str | None:
        if self.resolved and self.declared and self.resolved != self.declared:
            return f"{self.declared} (resolved {self.resolved})"
        return self.resolved or self.declared


@dataclass(frozen=True, slots=True)
class DiscoveryEvidence:
    """One deterministic observation. ``token`` is always a controlled
    value (see module docstring) -- never a source line."""

    provider_key: str
    evidence_type: EvidenceType
    file_path: str
    line: int | None
    token: str


@dataclass(frozen=True, slots=True)
class DependencyUsageSite:
    file_path: str
    line: int | None
    #: Enclosing symbol, e.g. ``generate_reply`` or ``Client.send`` --
    #: the same qualified-name form the repository index uses.
    symbol: str | None
    evidence_type: EvidenceType
    token: str
    confidence: DetectionConfidence

    @property
    def location(self) -> str:
        return f"{self.file_path}::{self.symbol}" if self.symbol else self.file_path


@dataclass(frozen=True, slots=True)
class ContractSource:
    type: ContractSourceType
    #: e.g. ``pypi:openai@1.40.0`` or ``openapi.yaml`` -- never a URL
    #: with credentials.
    ref: str


@dataclass(frozen=True, slots=True)
class ContractFingerprint:
    value: str
    algorithm: str = "sha256"
    normalization_version: int = CONTRACT_NORMALIZATION_VERSION


@dataclass(frozen=True, slots=True)
class DependencyContract:
    source: ContractSource
    fingerprint: ContractFingerprint
    #: Canonical, order-insensitive structure the fingerprint is computed
    #: over (JSON-compatible). Bounded; never contains examples,
    #: descriptions or literal values.
    normalized: Mapping[str, object]
    #: Small count summary for reports (e.g. ``{"paths": 4}``).
    summary: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True, slots=True)
class ExternalDependency:
    key: str
    provider: DependencyProvider
    kind: ExternalDependencyKind
    ecosystem: Ecosystem
    package_name: str | None
    version: DependencyVersion
    usage_sites: tuple[DependencyUsageSite, ...]
    evidence: tuple[DiscoveryEvidence, ...]
    confidence: DetectionConfidence
    contract: DependencyContract | None = None
    metadata: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def evidence_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.evidence:
            counts[item.evidence_type.value] = counts.get(item.evidence_type.value, 0) + 1
        return dict(sorted(counts.items()))


@dataclass(frozen=True, slots=True)
class PackageDeclaration:
    ecosystem: Ecosystem
    name: str
    spec: str | None
    manifest_path: str
    line: int | None = None


@dataclass(frozen=True, slots=True)
class PackageResolution:
    ecosystem: Ecosystem
    name: str
    version: str
    lockfile_path: str


@dataclass(frozen=True, slots=True)
class DependencyInventory:
    """Everything discovered for one repository snapshot."""

    repository: str
    commit_sha: str | None
    dependencies: tuple[ExternalDependency, ...]
    files_scanned: int
    #: Secret-store-like files deliberately never opened (a count only).
    secret_store_files_skipped: int
    discovery_version: int = DEPENDENCY_DISCOVERY_VERSION

    def by_key(self) -> dict[str, ExternalDependency]:
        return {d.key: d for d in self.dependencies}


__all__ = [
    "CONTRACT_NORMALIZATION_VERSION",
    "DEPENDENCY_DISCOVERY_VERSION",
    "ESTABLISHING_EVIDENCE",
    "USAGE_EVIDENCE",
    "ContractFingerprint",
    "ContractSource",
    "ContractSourceType",
    "DependencyContract",
    "DependencyInventory",
    "DependencyProvider",
    "DependencyUsageSite",
    "DependencyVersion",
    "DetectionConfidence",
    "DiscoveryEvidence",
    "Ecosystem",
    "EvidenceType",
    "ExternalDependency",
    "ExternalDependencyKind",
    "PackageDeclaration",
    "PackageResolution",
]
