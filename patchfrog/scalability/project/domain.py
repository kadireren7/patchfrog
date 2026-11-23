"""Typed application-discovery model (Milestone 2.3). Pure values, no I/O.

Design rules, all of which the engine and the tests rely on:

* **Facts only in ``routes``.** A route is listed only when its HTTP methods
  and its full path are statically known. A registration whose path,
  methods, prefix or router can't be resolved becomes an
  :class:`Unresolved` entry instead, never a guessed route.
* **Stable identities.** ``router_id`` is ``<kind>:<module>:<binding>``;
  ``route_id`` is a sha256 over (app, methods, path, endpoint symbol) and
  doesn't include line numbers, so moving code doesn't change it. No
  absolute path is ever part of an identity.
* **Explicit uncertainty.** ``Coverage.complete`` is True only when nothing
  was skipped, nothing failed to parse, nothing was left unresolved, and no
  limit was hit. ``incomplete_reasons`` says why otherwise.
* **No version invention.** :class:`FrameworkEvidence` keeps declared
  specs and locked versions apart; ``resolved_version`` is set only from
  lockfile evidence that names exactly one version.
"""

from __future__ import annotations

import hashlib
import json
import types
import typing
from dataclasses import dataclass, fields, is_dataclass
from enum import StrEnum

from patchfrog.scalability.project.db_domain import DatabaseModel

#: 2 (Milestone 2.4) added the ``database`` section; version-1 artifacts still load
#: (with an empty database section).
DISCOVERY_SCHEMA_VERSION = 2


class VersionStatus(StrEnum):
    RESOLVED = "resolved"  # exactly one version in lockfile evidence
    CONFLICTING = "conflicting"  # lockfiles disagree; no version is claimed
    DECLARED_UNRESOLVED = "declared_unresolved"  # declared, no lockfile evidence
    IMPORT_ONLY = "import_only"  # imported by code, neither declared nor locked


class RouterKind(StrEnum):
    APP = "app"  # a FastAPI() object
    ROUTER = "router"  # an APIRouter() object


class RouteKind(StrEnum):
    HTTP = "http"
    WEBSOCKET = "websocket"


class EndpointKind(StrEnum):
    ASYNC = "async"
    SYNC = "sync"
    UNKNOWN = "unknown"  # the endpoint isn't a statically known function


class DependencySource(StrEnum):
    APP = "app"  # FastAPI(dependencies=[...])
    INCLUDE = "include"  # include_router(..., dependencies=[...])
    ROUTER = "router"  # APIRouter(dependencies=[...])
    DECORATOR = "decorator"  # @router.get(..., dependencies=[...])
    PARAMETER = "parameter"  # def endpoint(x=Depends(f)) / Annotated[..., Depends(f)]


class DependencyResolution(StrEnum):
    LOCAL_FUNCTION = "local_function"
    LOCAL_CLASS = "local_class"
    EXTERNAL = "external"  # a name imported from a package outside the repository
    UNRESOLVED = "unresolved"


class UnresolvedKind(StrEnum):
    ROUTER_RECEIVER = "router_receiver"  # @x.get(...) where x can't be resolved
    INCLUDE_TARGET = "include_target"  # include_router(x) where x can't be resolved
    ROUTE_PATH = "route_path"  # a non-literal route path
    PREFIX = "prefix"  # a non-literal prefix
    METHODS = "methods"  # non-literal methods=[...]
    ENDPOINT = "endpoint"  # add_api_route(path, x) where x isn't a known function
    DEPENDENCY = "dependency"  # Depends(x) where x can't be resolved
    IMPORT = "import"  # an import that matches several repository modules
    UNSUPPORTED = "unsupported"  # a recognised but unsupported construct


class LimitationCode(StrEnum):
    PARSE_ERROR = "parse_error"
    FILE_SKIPPED = "file_skipped"
    MOUNT_NOT_TRAVERSED = "mount_not_traversed"
    INCLUDE_CYCLE = "include_cycle"
    LIMIT_REACHED = "limit_reached"


@dataclass(frozen=True, order=True)
class Location:
    path: str
    line: int


@dataclass(frozen=True)
class Declaration:
    manifest_path: str
    line: int | None
    spec: str | None


@dataclass(frozen=True)
class Resolution:
    lockfile_path: str
    version: str


@dataclass(frozen=True)
class FrameworkEvidence:
    package: str
    declarations: tuple[Declaration, ...]
    resolutions: tuple[Resolution, ...]
    importing_files: int
    version_status: VersionStatus
    resolved_version: str | None


@dataclass(frozen=True)
class DependencyUse:
    source: DependencySource
    resolution: DependencyResolution
    #: ``module:qualname`` for local targets, the dotted name for external ones.
    target: str | None
    parameter: str | None
    location: Location


@dataclass(frozen=True)
class DependencyFunction:
    symbol: str
    location: Location
    kind: EndpointKind
    is_generator: bool
    uses: tuple[DependencyUse, ...]


@dataclass(frozen=True)
class Router:
    router_id: str
    kind: RouterKind
    module: str
    binding: str
    location: Location
    #: None when the prefix couldn't be resolved (an Unresolved entry says why).
    prefix: str | None
    dependencies: tuple[DependencyUse, ...]


@dataclass(frozen=True)
class IncludeEdge:
    parent_id: str
    child_id: str
    prefix: str | None
    location: Location
    dependencies: tuple[DependencyUse, ...]


@dataclass(frozen=True)
class Endpoint:
    symbol: str | None
    kind: EndpointKind
    location: Location | None


@dataclass(frozen=True)
class Route:
    route_id: str
    #: None for a router that no discovered app includes ("unattached").
    app_id: str | None
    router_id: str
    kind: RouteKind
    methods: tuple[str, ...]
    path: str
    endpoint: Endpoint
    registration: Location
    #: Router ids from the app down to the declaring router.
    include_chain: tuple[str, ...]
    #: Effective dependencies, outermost first (app, includes, routers, decorator, parameters).
    dependencies: tuple[DependencyUse, ...]


@dataclass(frozen=True)
class Unresolved:
    kind: UnresolvedKind
    location: Location
    expression: str
    reason: str


@dataclass(frozen=True)
class Limitation:
    code: LimitationCode
    location: Location | None
    detail: str


@dataclass(frozen=True)
class Coverage:
    python_files: int
    scanned_files: int
    parsed_files: int
    complete: bool
    incomplete_reasons: tuple[str, ...]
    #: True when a configured limit (files, bytes, depth, result size) stopped work.
    limit_reached: bool


@dataclass(frozen=True)
class ApplicationModel:
    schema_version: int
    revision_id: str
    frameworks: tuple[FrameworkEvidence, ...]
    routers: tuple[Router, ...]
    includes: tuple[IncludeEdge, ...]
    routes: tuple[Route, ...]
    dependency_functions: tuple[DependencyFunction, ...]
    unresolved: tuple[Unresolved, ...]
    limitations: tuple[Limitation, ...]
    coverage: Coverage
    database: DatabaseModel

    @property
    def apps(self) -> tuple[Router, ...]:
        return tuple(r for r in self.routers if r.kind is RouterKind.APP)

    def to_dict(self) -> dict[str, object]:
        result = _to_plain(self)
        assert isinstance(result, dict)
        return result

    def to_json_bytes(self) -> bytes:
        """Canonical, byte-reproducible serialization."""

        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")

    @staticmethod
    def from_dict(data: typing.Mapping[str, object]) -> ApplicationModel:
        version = data.get("schema_version")
        if not isinstance(version, int) or version > DISCOVERY_SCHEMA_VERSION:
            raise ValueError(f"discovery schema {version!r} is not supported (max {DISCOVERY_SCHEMA_VERSION})")
        if version == 1:
            data = {**data, "database": _to_plain(DatabaseModel.empty())}
        value = _from_plain(ApplicationModel, data)
        assert isinstance(value, ApplicationModel)
        return value


def route_identity(app_id: str | None, router_id: str, methods: tuple[str, ...], path: str, symbol: str | None) -> str:
    owner = app_id if app_id is not None else f"unattached:{router_id}"
    payload = json.dumps([owner, list(methods), path, symbol], separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# --- generic (de)serialization over the frozen dataclasses above ------------------


def _to_plain(value: object) -> object:
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _to_plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, tuple):
        return [_to_plain(item) for item in value]
    return value


_HINTS: dict[type, dict[str, object]] = {}


def _from_plain(annotation: object, value: object) -> object:
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        options = [a for a in typing.get_args(annotation) if a is not type(None)]
        if value is None:
            return None
        return _from_plain(options[0], value)
    if origin is tuple:
        if not isinstance(value, list):
            raise ValueError("expected a list")
        (item_type, _ellipsis) = typing.get_args(annotation)
        return tuple(_from_plain(item_type, item) for item in value)
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        return annotation(str(value))
    if isinstance(annotation, type) and is_dataclass(annotation):
        if not isinstance(value, dict):
            raise ValueError(f"expected an object for {annotation.__name__}")
        hints = _HINTS.get(annotation)
        if hints is None:
            hints = _HINTS[annotation] = typing.get_type_hints(annotation)
        names = [f.name for f in fields(annotation)]
        unknown = set(value) - set(names)
        if unknown:
            # Strict, like the JSON Schema (additionalProperties: false): an artifact
            # of a supported version never has fields its version doesn't define.
            raise ValueError(f"unknown field(s) for {annotation.__name__}: {sorted(unknown)}")
        return annotation(**{name: _from_plain(hints[name], value[name]) for name in names})
    if annotation in (str, int, bool):
        if not isinstance(value, annotation) or (annotation is int and isinstance(value, bool)):
            raise ValueError(f"expected {annotation}")
        return value
    raise ValueError(f"unsupported annotation {annotation!r}")
