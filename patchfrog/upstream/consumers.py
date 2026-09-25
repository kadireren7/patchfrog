"""Consumer mapping (M6.5): external change -> the code that consumes it.

For one dependency and one change event, every M5 usage site is checked
against every *consumer-affecting* diff item (non-breaking items never
create consumers). A site becomes an affected consumer only through a
specific evidence path:

=====================  ==========================================  ==========
match                  evidence                                    confidence
=====================  ==========================================  ==========
SDK_SYMBOL             SDK call/constructor token == changed        high
                       symbol (argument-level items additionally
                       require the call to use / omit the argument)
SDK_NAMESPACE          token is inside a changed symbol namespace   medium
SDK_VIA_OPERATION      hinted operation -> SDK method bridge        high
HTTP_OPERATION         endpoint/path literal matches the changed    high/medium
                       operation's path (medium when the literal
                       cannot tell which HTTP method is used)
SCHEMA_REFERENCE       path matches an operation using the changed  medium
                       schema
IMPORT_MODULE          import of a moved/removed module             high
AUTH                   a call covered by a changed auth requirement medium
CREDENTIAL_CONFIG      client construction (direct) or a            medium/low
                       credential env-var *name* (potential)
PACKAGE_LEVEL          version-only evidence, no structure          low
=====================  ==========================================  ==========

Only ``PACKAGE_LEVEL`` and env-var matches are ``POTENTIAL``; everything
else is ``DIRECT``. When structural evidence exists, a version bump in
the same event never widens the consumer set. Sites that match nothing
are reported as unaffected -- never silently dropped, never counted as
impact.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from patchfrog.dependencies.domain import (
    DependencyUsageSite,
    DetectionConfidence,
    EvidenceType,
    ExternalDependency,
    ExternalDependencyKind,
)
from patchfrog.dependencies.files import is_secret_store_path
from patchfrog.upstream.calls import LocatedCall, locate_calls
from patchfrog.upstream.domain import (
    ContractDiffItem,
    DependencyTarget,
    DiffItemKind,
    ExternalChangeEvent,
    ExternalChangeKind,
    SubjectKind,
    normalize_package_name,
)
from patchfrog.upstream.hints import EMPTY_HINTS, ChangeHints
from patchfrog.upstream.openapi_diff import (
    operations_using_schema,
    operations_using_scheme,
    template_key,
)

HIGH = DetectionConfidence.HIGH
MEDIUM = DetectionConfidence.MEDIUM
LOW = DetectionConfidence.LOW
_CONFIDENCE_RANK = {LOW: 0, MEDIUM: 1, HIGH: 2}


class ImpactKind(StrEnum):
    DIRECT = "direct"
    TRANSITIVE = "transitive"
    POTENTIAL = "potential"


class ConsumerMatchType(StrEnum):
    SDK_SYMBOL = "sdk_symbol"
    SDK_NAMESPACE = "sdk_namespace"
    SDK_VIA_OPERATION = "sdk_via_operation"
    HTTP_OPERATION = "http_operation"
    SCHEMA_REFERENCE = "schema_reference"
    IMPORT_MODULE = "import_module"
    AUTH = "auth"
    CREDENTIAL_CONFIG = "credential_config"
    PACKAGE_LEVEL = "package_level"


# -- dependency identity / matching ------------------------------------------------


@dataclass(frozen=True, slots=True)
class DependencyIdentity:
    """The fields matching needs -- built from a discovered
    :class:`ExternalDependency` or from an M5 registry row alike."""

    key: str
    provider_key: str
    kind: str
    ecosystem: str
    package_name: str | None
    contract_fingerprint: str | None
    hosts: tuple[str, ...]
    declared_version: str | None = None
    resolved_version: str | None = None

    @classmethod
    def of(cls, dependency: ExternalDependency) -> DependencyIdentity:
        metadata = dict(dependency.metadata)
        hosts = {h for v in (metadata.get("api_hosts"), metadata.get("server_hosts")) if v for h in v.split(",") if h}
        return cls(
            key=dependency.key,
            provider_key=dependency.provider.key,
            kind=dependency.kind.value,
            ecosystem=dependency.ecosystem.value,
            package_name=dependency.package_name,
            contract_fingerprint=dependency.contract.fingerprint.value if dependency.contract else None,
            hosts=tuple(sorted(hosts)),
            declared_version=dependency.version.declared,
            resolved_version=dependency.version.resolved,
        )

    @property
    def current_version(self) -> str | None:
        return self.resolved_version or self.declared_version


def match_dependency(
    target: DependencyTarget, identity: DependencyIdentity, *, old_contract_fingerprint: str | None = None
) -> str | None:
    """Why ``identity`` is the dependency ``target`` refers to, or ``None``.

    Strongest identity first; never a fuzzy/name-similarity match."""

    if target.dependency_key and target.dependency_key == identity.key:
        return "dependency_key"
    if target.dependency_key:
        return None  # an explicit key excludes every other dependency
    if (
        target.package_name and identity.package_name
        and normalize_package_name(target.package_name) == normalize_package_name(identity.package_name)
        and (target.ecosystem is None or target.ecosystem.value == identity.ecosystem)
    ):
        return "package"
    if (
        old_contract_fingerprint and identity.contract_fingerprint == old_contract_fingerprint
        and identity.kind == ExternalDependencyKind.OPENAPI_CONTRACT.value
    ):
        return "contract_fingerprint"
    if target.api_hosts and set(target.api_hosts) & set(identity.hosts):
        return "api_host"
    if (
        target.provider_key and target.provider_key not in ("openapi",) and not target.package_name
        and target.provider_key == identity.provider_key
    ):
        return "provider"
    return None


# -- results ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AffectedConsumer:
    repository: str
    dependency_key: str
    site: DependencyUsageSite
    match_types: tuple[ConsumerMatchType, ...]
    confidence: DetectionConfidence
    impact: ImpactKind
    diff_item_keys: tuple[str, ...]
    reasons: tuple[str, ...]

    @property
    def file_path(self) -> str:
        return self.site.file_path

    @property
    def symbol(self) -> str | None:
        return self.site.symbol

    @property
    def location(self) -> str:
        return self.site.location

    @property
    def site_key(self) -> str:
        return site_key(self.site)


def site_key(site: DependencyUsageSite) -> str:
    return f"{site.file_path}:{site.line or 0}:{site.evidence_type.value}:{site.token}"


@dataclass(frozen=True, slots=True)
class ConsumerImpact:
    repository: str
    dependency_key: str
    match_reason: str
    affected: tuple[AffectedConsumer, ...]
    #: Sites of the dependency the change does not reach.
    unaffected_sites: tuple[DependencyUsageSite, ...]
    #: Sites that call the changed symbol but provably do not use the
    #: changed argument (the mapper looked at the call).
    ruled_out_sites: tuple[DependencyUsageSite, ...] = ()
    #: Consumer-affecting items that no site could be mapped to (e.g. an
    #: OpenAPI operation change against SDK-only usage with no bridge).
    unmapped_item_keys: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def direct(self) -> tuple[AffectedConsumer, ...]:
        return tuple(c for c in self.affected if c.impact is ImpactKind.DIRECT)

    @property
    def potential(self) -> tuple[AffectedConsumer, ...]:
        return tuple(c for c in self.affected if c.impact is ImpactKind.POTENTIAL)

    @property
    def unaffected_locations(self) -> tuple[str, ...]:
        affected = {c.location for c in self.affected}
        return tuple(sorted({s.location for s in self.unaffected_sites + self.ruled_out_sites} - affected))


# -- argument inspection ---------------------------------------------------------


CallLocator = Callable[[DependencyUsageSite], "list[LocatedCall] | None"]


def source_call_locator(root: Path) -> CallLocator:
    """Reads repository files (never secret stores) to locate calls; a
    ``None`` result means "not inspectable" (no source, unparseable)."""

    cache: dict[str, str | None] = {}
    resolved_root = root.resolve()

    def read(path: str) -> str | None:
        if path not in cache:
            candidate = (resolved_root / path).resolve()
            text: str | None = None
            if (
                not is_secret_store_path(path) and candidate.is_relative_to(resolved_root)
                and candidate.is_file() and not candidate.is_symlink()
            ):
                try:
                    text = candidate.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    text = None
            cache[path] = text
        return cache[path]

    def locate(site: DependencyUsageSite) -> list[LocatedCall] | None:
        if site.line is None:
            return None
        text = read(site.file_path)
        if text is None:
            return None
        return locate_calls(text, site.file_path, site.line, site.token)

    return locate


def no_call_locator(site: DependencyUsageSite) -> list[LocatedCall] | None:
    return None


#: Argument-level items affect a call only when the argument is present ...
_NEEDS_ARGUMENT = frozenset({
    DiffItemKind.SDK_PARAMETER_RENAMED, DiffItemKind.SDK_PARAMETER_REMOVED, DiffItemKind.SDK_PARAMETER_TYPE_CHANGED,
    DiffItemKind.SDK_ENUM_NARROWED, DiffItemKind.SDK_ENUM_VALUE_REPLACED,
    DiffItemKind.PARAMETER_RENAMED, DiffItemKind.PARAMETER_REMOVED, DiffItemKind.PARAMETER_TYPE_CHANGED,
    DiffItemKind.PARAMETER_ENUM_NARROWED, DiffItemKind.PARAMETER_SCHEMA_CHANGED,
    DiffItemKind.REQUEST_FIELD_RENAMED, DiffItemKind.REQUEST_FIELD_REMOVED,
})
#: ... or only when it is absent.
_NEEDS_ABSENCE = frozenset({
    DiffItemKind.SDK_PARAMETER_ADDED_REQUIRED, DiffItemKind.SDK_PARAMETER_BECAME_REQUIRED,
    DiffItemKind.PARAMETER_ADDED_REQUIRED, DiffItemKind.PARAMETER_BECAME_REQUIRED,
    DiffItemKind.REQUEST_FIELD_ADDED_REQUIRED, DiffItemKind.REQUEST_FIELD_BECAME_REQUIRED,
})


def _argument_verdict(
    item: ContractDiffItem, calls: list[LocatedCall] | None
) -> tuple[bool, DetectionConfidence, str]:
    """(affected?, confidence, reason) for an argument-level item."""

    member = item.subject.member
    if member is None or (item.kind not in _NEEDS_ARGUMENT and item.kind not in _NEEDS_ABSENCE):
        return True, HIGH, "call of the changed symbol"
    if member.startswith("returns."):
        return True, MEDIUM, "result of the changed symbol is consumed here (field use not verified)"
    if calls is None:
        return True, MEDIUM, "call arguments not inspectable"
    if not calls:
        return True, MEDIUM, "call not located at the recorded line"
    top_level = member.split(".")[0]
    verdicts: list[tuple[bool, DetectionConfidence, str]] = []
    for call in calls:
        keyword = call.keyword(top_level)
        if item.kind in _NEEDS_ABSENCE:
            if keyword is not None:
                verdicts.append((False, HIGH, f"call already passes '{top_level}'"))
            elif call.indirect_keywords:
                verdicts.append((True, MEDIUM, "arguments passed indirectly; cannot confirm"))
            else:
                verdicts.append((True, HIGH, f"call does not pass newly required '{top_level}'"))
            continue
        if keyword is None:
            if call.indirect_keywords:
                verdicts.append((True, MEDIUM, "arguments passed indirectly; cannot confirm"))
            else:
                verdicts.append((False, HIGH, f"call does not pass '{top_level}'"))
            continue
        if item.kind is DiffItemKind.SDK_ENUM_VALUE_REPLACED:
            if keyword.is_literal:
                old_value = item.old.strip('"') if item.old else None
                if str(keyword.literal) == old_value:
                    verdicts.append((True, HIGH, f"call passes '{top_level}={old_value}'"))
                else:
                    verdicts.append((False, HIGH, f"call passes a different '{top_level}' value"))
            else:
                verdicts.append((True, MEDIUM, f"'{top_level}' value not a literal; cannot confirm"))
            continue
        verdicts.append((True, HIGH, f"call passes '{top_level}'"))
    affected = [v for v in verdicts if v[0]]
    if affected:
        return max(affected, key=lambda v: _CONFIDENCE_RANK[v[1]])
    return verdicts[0]


# -- path matching ---------------------------------------------------------------


def site_path(site: DependencyUsageSite) -> str | None:
    if site.evidence_type is EvidenceType.OPENAPI_PATH_REFERENCE:
        return site.token
    if site.evidence_type is EvidenceType.HTTP_ENDPOINT:
        index = site.token.find("/")
        return site.token[index:] if index >= 0 else "/"
    return None


def _path_regex(spec_path: str) -> re.Pattern[str]:
    segments = [s for s in spec_path.strip("/").split("/") if s]
    parts = [r"(?:\{[^}/]*\}|[^/]+)" if s.startswith("{") and s.endswith("}") else re.escape(s) for s in segments]
    body = "/" + "/".join(parts) if parts else "/"
    # A server base path (``/v1``) may prefix a spec path in a literal; only
    # allowed for specific (2+ segment) spec paths.
    prefix = r"(?:/[^/]+){0,2}" if len(segments) >= 2 else ""
    return re.compile(f"^{prefix}{body}/?$")


def path_matches(literal: str, spec_path: str) -> bool:
    return bool(_path_regex(spec_path).match(literal.split("?")[0]))


# -- the mapper ------------------------------------------------------------------------


_CALL_EVIDENCE = frozenset({EvidenceType.SDK_CALL, EvidenceType.SDK_CONSTRUCTOR})
_HTTP_EVIDENCE = frozenset({EvidenceType.HTTP_ENDPOINT, EvidenceType.OPENAPI_PATH_REFERENCE})
_AUTH_KINDS = frozenset({
    DiffItemKind.AUTH_REQUIREMENT_ADDED, DiffItemKind.AUTH_SCHEME_CHANGED, DiffItemKind.AUTH_SCOPE_ADDED,
    DiffItemKind.SECURITY_SCHEME_CHANGED, DiffItemKind.SECURITY_SCHEME_REMOVED,
})


@dataclass
class _Match:
    match_type: ConsumerMatchType
    confidence: DetectionConfidence
    impact: ImpactKind
    reason: str


@dataclass
class _Mapper:
    event: ExternalChangeEvent
    hints: ChangeHints
    locate: CallLocator
    old_contract: Mapping[str, Any]
    _calls: dict[str, list[LocatedCall] | None] = field(default_factory=dict)

    def calls(self, site: DependencyUsageSite) -> list[LocatedCall] | None:
        key = site_key(site)
        if key not in self._calls:
            self._calls[key] = self.locate(site) if site.evidence_type in _CALL_EVIDENCE else None
        return self._calls[key]

    def methods_at(self, path: str) -> int:
        paths = self.old_contract.get("paths") or {}
        return sum(len(ops) for p, ops in paths.items() if template_key(p) == template_key(path))

    def match(self, item: ContractDiffItem, site: DependencyUsageSite) -> _Match | None:
        subject = item.subject
        if subject.kind is SubjectKind.PACKAGE:
            if self.event.kind is ExternalChangeKind.CONTRACT_REVISION:
                return None  # structural evidence supersedes version-level evidence
            if site.evidence_type is EvidenceType.ENV_VAR_NAME:
                return None
            return _Match(ConsumerMatchType.PACKAGE_LEVEL, LOW, ImpactKind.POTENTIAL,
                          "version change only; no structural evidence about this usage")
        if subject.kind is SubjectKind.SDK_SYMBOL and subject.name:
            return self._sdk(item, site, subject.name, ConsumerMatchType.SDK_SYMBOL)
        if subject.kind is SubjectKind.MODULE and subject.name:
            if site.evidence_type is EvidenceType.IMPORT and (
                site.token == subject.name or site.token.startswith(subject.name + ".")
            ):
                return _Match(ConsumerMatchType.IMPORT_MODULE, HIGH, ImpactKind.DIRECT,
                              f"imports {site.token} from changed module {subject.name}")
            return None
        if subject.kind is SubjectKind.OPERATION and subject.method and subject.path:
            if item.kind in _AUTH_KINDS or item.kind is DiffItemKind.AUTH_SCOPE_ADDED:
                env = self._credential(site)
                if env is not None:
                    return env
            return self._operation(item, site, subject.method, subject.path, ConsumerMatchType.HTTP_OPERATION)
        if subject.kind is SubjectKind.SCHEMA and subject.name:
            for method, path in operations_using_schema(self.old_contract, subject.name):
                found = self._operation(item, site, method, path, ConsumerMatchType.SCHEMA_REFERENCE)
                if found is not None:
                    found.confidence = MEDIUM if found.confidence is HIGH else found.confidence
                    found.reason = f"{found.reason} (uses schema {subject.name})"
                    return found
            return None
        if subject.kind is SubjectKind.AUTH_GLOBAL:
            if site.evidence_type is EvidenceType.SDK_CALL or self._is_operation_request(site):
                return _Match(ConsumerMatchType.AUTH, MEDIUM, ImpactKind.DIRECT,
                              "request covered by the changed contract-wide auth requirement")
            return self._credential(site)
        if subject.kind is SubjectKind.SECURITY_SCHEME and subject.name:
            if item.kind not in _AUTH_KINDS:
                return None
            for method, path in operations_using_scheme(self.old_contract, subject.name):
                found = self._operation(item, site, method, path, ConsumerMatchType.AUTH)
                if found is not None:
                    found.confidence = MEDIUM
                    return found
            return self._credential(site)
        return None

    def _is_operation_request(self, site: DependencyUsageSite) -> bool:
        """An HTTP site is a request only if it targets a contract operation
        -- a bare base-URL constant is configuration, not a call."""

        literal = site_path(site)
        if literal is None or site.evidence_type not in _HTTP_EVIDENCE:
            return False
        return any(path_matches(literal, path) for path in (self.old_contract.get("paths") or {}))

    def _credential(self, site: DependencyUsageSite) -> _Match | None:
        if site.evidence_type is EvidenceType.SDK_CONSTRUCTOR:
            return _Match(ConsumerMatchType.CREDENTIAL_CONFIG, MEDIUM, ImpactKind.DIRECT,
                          "client construction configures the credentials whose requirements changed")
        if site.evidence_type is EvidenceType.ENV_VAR_NAME:
            return _Match(ConsumerMatchType.CREDENTIAL_CONFIG, LOW, ImpactKind.POTENTIAL,
                          f"credential configuration name {site.token} may need updating")
        return None

    def _sdk(
        self, item: ContractDiffItem, site: DependencyUsageSite, symbol: str, match_type: ConsumerMatchType
    ) -> _Match | None:
        if site.evidence_type not in _CALL_EVIDENCE:
            return None
        if site.token == symbol:
            affected, confidence, reason = _argument_verdict(item, self.calls(site))
            if not affected:
                return _Match(match_type, confidence, ImpactKind.DIRECT, f"RULED_OUT:{reason}")
            # An argument-level change the call cannot be shown to touch
            # (indirect/uninspectable arguments) is a possibility, not proof.
            unconfirmed = item.kind in _NEEDS_ARGUMENT | _NEEDS_ABSENCE and confidence is not HIGH
            return _Match(match_type, confidence, ImpactKind.POTENTIAL if unconfirmed else ImpactKind.DIRECT,
                          f"{reason} ({symbol})")
        if site.token.startswith(symbol + ".") and item.subject.member is None:
            return _Match(ConsumerMatchType.SDK_NAMESPACE, MEDIUM, ImpactKind.DIRECT,
                          f"{site.token} is inside changed namespace {symbol}")
        return None

    def _operation(
        self, item: ContractDiffItem, site: DependencyUsageSite, method: str, path: str, match_type: ConsumerMatchType
    ) -> _Match | None:
        literal = site_path(site)
        if literal is not None and site.evidence_type in _HTTP_EVIDENCE:
            if not path_matches(literal, path):
                return None
            ambiguous = self.methods_at(path) > 1
            return _Match(
                match_type, MEDIUM if ambiguous else HIGH, ImpactKind.DIRECT,
                f"path literal matches {method.upper()} {path}"
                + ("; HTTP method not determinable from the literal" if ambiguous else ""),
            )
        for symbol in self.hints.symbols_for_operation(method, path):
            found = self._sdk(item, site, symbol, ConsumerMatchType.SDK_VIA_OPERATION)
            if found is not None:
                found.reason = f"{found.reason} via {method.upper()} {path} -> {symbol}"
                return found
        return None


def map_consumers(
    event: ExternalChangeEvent,
    *,
    repository: str,
    dependency_key: str,
    match_reason: str,
    usage_sites: Sequence[DependencyUsageSite],
    hints: ChangeHints = EMPTY_HINTS,
    locate: CallLocator = no_call_locator,
) -> ConsumerImpact:
    mapper = _Mapper(event=event, hints=hints, locate=locate, old_contract=event.old.normalized or {})
    items = event.consumer_affecting_items
    affected: list[AffectedConsumer] = []
    unaffected: list[DependencyUsageSite] = []
    ruled_out: list[DependencyUsageSite] = []
    mapped_items: set[str] = set()
    for site in usage_sites:
        matches: list[tuple[ContractDiffItem, _Match]] = []
        excluded = False
        for item in items:
            found = mapper.match(item, site)
            if found is None:
                continue
            if found.reason.startswith("RULED_OUT:"):
                excluded = True
                continue
            matches.append((item, found))
        if not matches:
            (ruled_out if excluded else unaffected).append(site)
            continue
        mapped_items.update(item.key for item, _ in matches)
        impact = ImpactKind.DIRECT if any(m.impact is ImpactKind.DIRECT for _, m in matches) else ImpactKind.POTENTIAL
        relevant = [m for _, m in matches if m.impact is impact]
        affected.append(
            AffectedConsumer(
                repository=repository,
                dependency_key=dependency_key,
                site=site,
                match_types=tuple(sorted({m.match_type for _, m in matches}, key=lambda t: t.value)),
                confidence=max((m.confidence for m in relevant), key=lambda c: _CONFIDENCE_RANK[c]),
                impact=impact,
                diff_item_keys=tuple(sorted({item.key for item, _ in matches})),
                reasons=tuple(sorted({m.reason for _, m in matches})),
            )
        )
    unmapped = tuple(sorted(
        item.key for item in items
        if item.key not in mapped_items
        and not (item.subject.kind is SubjectKind.PACKAGE and event.kind is ExternalChangeKind.CONTRACT_REVISION)
    ))
    notes: list[str] = []
    if unmapped and not any(s.evidence_type in _HTTP_EVIDENCE for s in usage_sites) and any(
        i.subject.kind is SubjectKind.OPERATION for i in items if i.key in unmapped
    ):
        notes.append("operation-level changes cannot be mapped to SDK calls without an operation_symbols hint")
    return ConsumerImpact(
        repository=repository,
        dependency_key=dependency_key,
        match_reason=match_reason,
        affected=tuple(sorted(affected, key=lambda c: (c.file_path, c.site.line or 0, c.site.token))),
        unaffected_sites=tuple(unaffected),
        ruled_out_sites=tuple(ruled_out),
        unmapped_item_keys=unmapped,
        notes=tuple(notes),
    )


__all__ = [
    "AffectedConsumer",
    "CallLocator",
    "ConsumerImpact",
    "ConsumerMatchType",
    "DependencyIdentity",
    "ImpactKind",
    "map_consumers",
    "match_dependency",
    "no_call_locator",
    "path_matches",
    "site_key",
    "site_path",
    "source_call_locator",
]
