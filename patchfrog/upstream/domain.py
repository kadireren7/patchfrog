"""Provider-agnostic domain model for upstream API/SDK changes (M6).

An :class:`ExternalChangeEvent` records one observed change in something
the repository depends on but does not own -- an SDK release, an OpenAPI
contract revision, a GitHub release, or a manually supplied contract
pair -- together with the deterministic contract diff and its
compatibility classification.

Everything here is pure data: no I/O, no provider calls. Events are
built by :mod:`patchfrog.upstream.events`; the contract diff by
:mod:`patchfrog.upstream.openapi_diff`, :mod:`patchfrog.upstream.sdk_surface`
and :mod:`patchfrog.upstream.package_version`.

**Direction.** PatchFrog is always the *consumer* of the external
contract. "Breaking" therefore means "code that worked against the old
contract can fail against the new one": a new required request field is
breaking, a new response field is not; a removed response field is
breaking, a removed request field usually is not.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from patchfrog.dependencies.domain import Ecosystem

#: Bumped when diff/classification semantics change such that a
#: previously-recorded event's diff or fingerprint is no longer what
#: re-analysis would produce.
UPSTREAM_CHANGE_VERSION = 1

#: Old/new representations stored on a diff item are bounded.
MAX_REPRESENTATION_CHARS = 1000
#: A single analysis never emits more than this many diff items; the
#: event records that it was truncated.
MAX_DIFF_ITEMS = 500


class ExternalChangeSource(StrEnum):
    """Where the evidence for a change came from."""

    PACKAGE_VERSION = "package_version"
    OPENAPI_REVISION = "openapi_revision"
    SDK_SURFACE = "sdk_surface"
    GITHUB_RELEASE = "github_release"
    CHANGELOG = "changelog"
    MANUAL_CONTRACT_PAIR = "manual_contract_pair"
    #: The old side is a contract snapshot from the M5 registry.
    REGISTRY_SNAPSHOT = "registry_snapshot"


class ExternalChangeKind(StrEnum):
    #: A structural contract (OpenAPI document or SDK surface) changed.
    CONTRACT_REVISION = "contract_revision"
    #: Only a version changed -- no structural contract on both sides.
    VERSION_UPDATE = "version_update"


class CompatibilityClass(StrEnum):
    NON_BREAKING = "non_breaking"
    POTENTIALLY_BREAKING = "potentially_breaking"
    BREAKING = "breaking"
    #: The evidence cannot tell (e.g. a composed-schema change).
    UNKNOWN = "unknown"


_COMPAT_RANK = {
    CompatibilityClass.NON_BREAKING: 0,
    CompatibilityClass.UNKNOWN: 1,
    CompatibilityClass.POTENTIALLY_BREAKING: 2,
    CompatibilityClass.BREAKING: 3,
}


def most_severe(*classes: CompatibilityClass) -> CompatibilityClass:
    return max(classes, key=lambda c: _COMPAT_RANK[c]) if classes else CompatibilityClass.NON_BREAKING


def compat_rank(value: CompatibilityClass) -> int:
    return _COMPAT_RANK[value]


class ChangeRisk(StrEnum):
    """Event-level outcome of :mod:`patchfrog.upstream.classify`."""

    SAFE = "safe"
    LOW_RISK = "low_risk"
    REVIEW_REQUIRED = "review_required"
    BREAKING = "breaking"


class SubjectKind(StrEnum):
    """What a diff item is *about* -- the key the consumer mapper uses to
    find the code that could be affected."""

    OPERATION = "operation"  # method + path
    SCHEMA = "schema"  # a named component schema
    SDK_SYMBOL = "sdk_symbol"  # an SDK call chain / class, e.g. ``chat.create``
    MODULE = "module"  # an importable module path
    PACKAGE = "package"  # the package as a whole (version-level evidence)
    AUTH_GLOBAL = "auth_global"  # contract-wide security requirement
    SECURITY_SCHEME = "security_scheme"


class DiffItemKind(StrEnum):
    # -- endpoints / operations
    ENDPOINT_ADDED = "endpoint_added"
    ENDPOINT_REMOVED = "endpoint_removed"
    OPERATION_ADDED = "operation_added"
    OPERATION_REMOVED = "operation_removed"
    ENDPOINT_PATH_CHANGED = "endpoint_path_changed"
    OPERATION_DEPRECATED = "operation_deprecated"
    # -- parameters
    PARAMETER_ADDED_REQUIRED = "parameter_added_required"
    PARAMETER_ADDED_OPTIONAL = "parameter_added_optional"
    PARAMETER_REMOVED = "parameter_removed"
    PARAMETER_RENAMED = "parameter_renamed"
    PARAMETER_BECAME_REQUIRED = "parameter_became_required"
    PARAMETER_BECAME_OPTIONAL = "parameter_became_optional"
    PARAMETER_TYPE_CHANGED = "parameter_type_changed"
    PARAMETER_ENUM_NARROWED = "parameter_enum_narrowed"
    PARAMETER_ENUM_EXPANDED = "parameter_enum_expanded"
    PARAMETER_SCHEMA_CHANGED = "parameter_schema_changed"
    # -- request bodies
    REQUEST_BODY_ADDED_REQUIRED = "request_body_added_required"
    REQUEST_BODY_ADDED_OPTIONAL = "request_body_added_optional"
    REQUEST_BODY_REMOVED = "request_body_removed"
    REQUEST_BODY_BECAME_REQUIRED = "request_body_became_required"
    REQUEST_BODY_BECAME_OPTIONAL = "request_body_became_optional"
    REQUEST_MEDIA_TYPE_ADDED = "request_media_type_added"
    REQUEST_MEDIA_TYPE_REMOVED = "request_media_type_removed"
    REQUEST_FIELD_ADDED_REQUIRED = "request_field_added_required"
    REQUEST_FIELD_ADDED_OPTIONAL = "request_field_added_optional"
    REQUEST_FIELD_REMOVED = "request_field_removed"
    REQUEST_FIELD_RENAMED = "request_field_renamed"
    REQUEST_FIELD_BECAME_REQUIRED = "request_field_became_required"
    REQUEST_FIELD_BECAME_OPTIONAL = "request_field_became_optional"
    REQUEST_TYPE_CHANGED = "request_type_changed"
    REQUEST_ENUM_NARROWED = "request_enum_narrowed"
    REQUEST_ENUM_EXPANDED = "request_enum_expanded"
    REQUEST_SCHEMA_CHANGED = "request_schema_changed"
    # -- responses
    RESPONSE_STATUS_ADDED = "response_status_added"
    RESPONSE_STATUS_REMOVED = "response_status_removed"
    RESPONSE_MEDIA_TYPE_ADDED = "response_media_type_added"
    RESPONSE_MEDIA_TYPE_REMOVED = "response_media_type_removed"
    RESPONSE_FIELD_ADDED = "response_field_added"
    RESPONSE_FIELD_REMOVED = "response_field_removed"
    RESPONSE_FIELD_BECAME_REQUIRED = "response_field_became_required"
    RESPONSE_FIELD_BECAME_OPTIONAL = "response_field_became_optional"
    RESPONSE_TYPE_CHANGED = "response_type_changed"
    RESPONSE_ENUM_NARROWED = "response_enum_narrowed"
    RESPONSE_ENUM_EXPANDED = "response_enum_expanded"
    RESPONSE_SCHEMA_CHANGED = "response_schema_changed"
    # -- authentication
    AUTH_REQUIREMENT_ADDED = "auth_requirement_added"
    AUTH_REQUIREMENT_REMOVED = "auth_requirement_removed"
    AUTH_SCHEME_CHANGED = "auth_scheme_changed"
    AUTH_SCOPE_ADDED = "auth_scope_added"
    AUTH_SCOPE_REMOVED = "auth_scope_removed"
    SECURITY_SCHEME_ADDED = "security_scheme_added"
    SECURITY_SCHEME_REMOVED = "security_scheme_removed"
    SECURITY_SCHEME_CHANGED = "security_scheme_changed"
    # -- components / schemas
    SCHEMA_ADDED = "schema_added"
    SCHEMA_REMOVED = "schema_removed"
    SCHEMA_PROPERTY_ADDED_REQUIRED = "schema_property_added_required"
    SCHEMA_PROPERTY_ADDED_OPTIONAL = "schema_property_added_optional"
    SCHEMA_PROPERTY_REMOVED = "schema_property_removed"
    SCHEMA_PROPERTY_BECAME_REQUIRED = "schema_property_became_required"
    SCHEMA_PROPERTY_BECAME_OPTIONAL = "schema_property_became_optional"
    SCHEMA_TYPE_CHANGED = "schema_type_changed"
    SCHEMA_ENUM_NARROWED = "schema_enum_narrowed"
    SCHEMA_ENUM_EXPANDED = "schema_enum_expanded"
    SCHEMA_CHANGED = "schema_changed"
    # -- SDK surface
    SDK_SYMBOL_ADDED = "sdk_symbol_added"
    SDK_SYMBOL_REMOVED = "sdk_symbol_removed"
    SDK_SYMBOL_RENAMED = "sdk_symbol_renamed"
    SDK_PARAMETER_ADDED_REQUIRED = "sdk_parameter_added_required"
    SDK_PARAMETER_ADDED_OPTIONAL = "sdk_parameter_added_optional"
    SDK_PARAMETER_REMOVED = "sdk_parameter_removed"
    SDK_PARAMETER_RENAMED = "sdk_parameter_renamed"
    SDK_PARAMETER_BECAME_REQUIRED = "sdk_parameter_became_required"
    SDK_PARAMETER_BECAME_OPTIONAL = "sdk_parameter_became_optional"
    SDK_PARAMETER_TYPE_CHANGED = "sdk_parameter_type_changed"
    SDK_ENUM_NARROWED = "sdk_enum_narrowed"
    SDK_ENUM_VALUE_REPLACED = "sdk_enum_value_replaced"
    SDK_RETURN_FIELD_RENAMED = "sdk_return_field_renamed"
    SDK_RETURN_SHAPE_CHANGED = "sdk_return_shape_changed"
    SDK_MODULE_REMOVED = "sdk_module_removed"
    SDK_MODULE_MOVED = "sdk_module_moved"
    # -- package version
    PACKAGE_MAJOR_BUMP = "package_major_bump"
    PACKAGE_MINOR_BUMP = "package_minor_bump"
    PACKAGE_PATCH_BUMP = "package_patch_bump"
    PACKAGE_PRERELEASE_CHANGE = "package_prerelease_change"
    PACKAGE_DOWNGRADE = "package_downgrade"
    PACKAGE_VERSION_UNPARSEABLE = "package_version_unparseable"


@dataclass(frozen=True, slots=True)
class DiffSubject:
    kind: SubjectKind
    #: ``OPERATION``: HTTP method (lower-case).
    method: str | None = None
    #: ``OPERATION``: the OpenAPI path template.
    path: str | None = None
    #: ``SCHEMA``/``SDK_SYMBOL``/``MODULE``/``PACKAGE``/``SECURITY_SCHEME``:
    #: the *old* (consumer-visible) name.
    name: str | None = None
    #: A member of the subject: parameter / field / property name.
    member: str | None = None

    def describe(self) -> str:
        if self.kind is SubjectKind.OPERATION:
            head = f"{(self.method or '').upper()} {self.path}"
        elif self.kind is SubjectKind.AUTH_GLOBAL:
            head = "contract-wide security"
        else:
            head = self.name or self.kind.value
        return f"{head} [{self.member}]" if self.member else head

    def as_dict(self) -> dict[str, str]:
        return {
            k: v
            for k, v in (
                ("kind", self.kind.value), ("method", self.method), ("path", self.path),
                ("name", self.name), ("member", self.member),
            )
            if v is not None
        }


@dataclass(frozen=True, slots=True)
class ContractDiffItem:
    kind: DiffItemKind
    #: Stable dotted location in the normalized contract, e.g.
    #: ``paths[/v1/charges].post.parameters[query:limit]``.
    location: str
    subject: DiffSubject
    compatibility: CompatibilityClass
    #: Canonical JSON of the old/new node (bounded), ``None`` when absent.
    old: str | None
    new: str | None
    explanation: str
    #: Deterministic, machine-checkable facts the classification rests on.
    evidence: tuple[str, ...]
    #: Known replacement (new symbol / path / parameter / module / enum
    #: value) -- only ever set from contract identity or explicit hints.
    replacement: str | None = None

    @property
    def key(self) -> str:
        """Stable identity within one event (kind + location)."""

        return hashlib.sha256(f"{self.kind.value}|{self.location}".encode()).hexdigest()[:16]

    @property
    def affects_consumers(self) -> bool:
        return self.compatibility is not CompatibilityClass.NON_BREAKING


@dataclass(frozen=True, slots=True)
class DependencyTarget:
    """Which dependency a change is about. Any subset may be known;
    :func:`patchfrog.upstream.consumers.match_dependency` uses the
    strongest available identity."""

    provider_key: str | None = None
    ecosystem: Ecosystem | None = None
    package_name: str | None = None
    dependency_key: str | None = None
    #: Exact API hostnames (from the contract's servers) -- a match
    #: signal for HTTP/OpenAPI dependencies.
    api_hosts: tuple[str, ...] = ()
    #: Importable modules an SDK surface declares -- lets discovery track
    #: call chains of an SDK PatchFrog has no built-in adapter for.
    modules: tuple[str, ...] = ()
    display_name: str | None = None

    @property
    def label(self) -> str:
        if self.display_name:
            return self.display_name
        if self.package_name:
            prefix = f"{self.ecosystem.value}:" if self.ecosystem else ""
            return f"{prefix}{self.package_name}"
        return self.dependency_key or self.provider_key or "unknown dependency"

    def identity(self) -> dict[str, object]:
        return {
            "provider_key": self.provider_key,
            "ecosystem": self.ecosystem.value if self.ecosystem else None,
            "package_name": normalize_package_name(self.package_name) if self.package_name else None,
            "dependency_key": self.dependency_key,
            "api_hosts": sorted(self.api_hosts),
        }


def normalize_package_name(name: str) -> str:
    return name.strip().lower().replace("_", "-")


@dataclass(frozen=True, slots=True)
class ExternalContractRevision:
    """One side of a change. ``normalized`` is the in-memory structure the
    diff ran over; only its fingerprint is ever persisted (M5's contract
    snapshots own full contract storage)."""

    version: str | None
    fingerprint: str | None
    source_ref: str | None
    normalized: Mapping[str, object] | None = None
    #: ``openapi`` | ``sdk_surface`` | ``package`` | None.
    contract_format: str | None = None


@dataclass(frozen=True, slots=True)
class DependencyRelease:
    """Release metadata when available (local metadata only -- no fetch)."""

    version: str
    tag: str | None = None
    published_at: str | None = None
    #: A sanitized URL (no credentials/query) or ``None``.
    url: str | None = None
    #: Only a digest of the notes is kept -- never the notes text.
    notes_sha256: str | None = None
    prerelease: bool = False


@dataclass(frozen=True, slots=True)
class ChangeClassification:
    risk: ChangeRisk
    compatibility: CompatibilityClass
    #: Machine-readable reason codes, sorted, e.g.
    #: ``breaking:endpoint_removed``, ``major_version_bump_without_structural_proof``.
    reasons: tuple[str, ...]
    counts: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    has_structural_evidence: bool = False


@dataclass(frozen=True, slots=True)
class ExternalChangeEvent:
    target: DependencyTarget
    source: ExternalChangeSource
    kind: ExternalChangeKind
    source_ref: str
    old: ExternalContractRevision
    new: ExternalContractRevision
    diff: tuple[ContractDiffItem, ...]
    classification: ChangeClassification
    fingerprint: str
    observed_at: datetime
    release: DependencyRelease | None = None
    #: sha256 of the normalized change hints that shaped this diff (part
    #: of the fingerprint), or ``None`` when no hints were supplied.
    hints_fingerprint: str | None = None
    #: Provenance line from the hints document, if any (bounded).
    hints_provenance: str | None = None
    truncated: bool = False
    #: Non-fatal notes, e.g. a hint that referenced a symbol absent from
    #: the new contract and was therefore ignored.
    notes: tuple[str, ...] = ()
    engine_version: int = UPSTREAM_CHANGE_VERSION

    def items_by_key(self) -> dict[str, ContractDiffItem]:
        return {item.key: item for item in self.diff}

    @property
    def consumer_affecting_items(self) -> tuple[ContractDiffItem, ...]:
        return tuple(item for item in self.diff if item.affects_consumers)


__all__ = [
    "MAX_DIFF_ITEMS",
    "MAX_REPRESENTATION_CHARS",
    "UPSTREAM_CHANGE_VERSION",
    "ChangeClassification",
    "ChangeRisk",
    "CompatibilityClass",
    "ContractDiffItem",
    "DependencyRelease",
    "DependencyTarget",
    "DiffItemKind",
    "DiffSubject",
    "ExternalChangeEvent",
    "ExternalChangeKind",
    "ExternalChangeSource",
    "ExternalContractRevision",
    "SubjectKind",
    "compat_rank",
    "most_severe",
    "normalize_package_name",
]
