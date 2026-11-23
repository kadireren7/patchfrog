"""Typed database-structure discovery model (Milestone 2.4). Pure values.

The ``database`` section of :class:`~patchfrog.scalability.project.domain.ApplicationModel`.
Rules the engine and tests rely on:

* **Provenance is explicit.** Every column, table name, primary key and
  relationship loading strategy says whether it was declared in the source
  (``explicit``), inferred from a documented framework rule (``inferred``:
  ``Mapped[...]`` annotations, SQLModel annotation fields, SQLModel's
  lower-cased class-name table), taken from a mapped parent (``inherited``),
  SQLAlchemy's documented default (``default``: ``lazy="select"``), or is
  unknown.
* **No connection settings are invented.** For engines only the URL *scheme*
  of a literal URL is kept (never host, user, password or database), and only
  allowlisted options with literal values; anything else is ``None``.
* **Dynamic constructs are reported, never modelled.** ``declared_attr``,
  ``column_property``, ``hybrid_property``, ``type(...)`` models, registry
  mapping and attributes from external base classes become
  :class:`DbUnresolved` entries and make the model ``complete = False``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

DATABASE_SCHEMA_VERSION = 1


class OrmFlavor(StrEnum):
    DECLARATIVE = "sqlalchemy_declarative"
    SQLMODEL = "sqlmodel"


class BaseKind(StrEnum):
    DECLARATIVE_BASE_CLASS = "declarative_base_class"  # class Base(DeclarativeBase)
    DECLARATIVE_BASE_FUNCTION = "declarative_base_function"  # Base = declarative_base()


class Provenance(StrEnum):
    EXPLICIT = "explicit"
    INFERRED = "inferred"
    INHERITED = "inherited"
    DEFAULT = "default"
    UNKNOWN = "unknown"


class ColumnDeclaration(StrEnum):
    MAPPED_COLUMN = "mapped_column"
    COLUMN = "column"
    MAPPED_ANNOTATION = "mapped_annotation"  # x: Mapped[int] without a value
    SQLMODEL_FIELD = "sqlmodel_field"  # x: int = Field(...)
    SQLMODEL_ANNOTATION = "sqlmodel_annotation"  # x: int  /  x: int = 3


class InheritanceKind(StrEnum):
    NONE = "none"
    JOINED = "joined"  # own table under a mapped parent
    SINGLE = "single"  # no own table: the parent's table
    UNKNOWN = "unknown"


class TargetResolution(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"


class DbObjectKind(StrEnum):
    ENGINE = "engine"
    ASYNC_ENGINE = "async_engine"
    SESSIONMAKER = "sessionmaker"
    ASYNC_SESSIONMAKER = "async_sessionmaker"
    SCOPED_SESSION = "scoped_session"
    SESSION = "session"
    ASYNC_SESSION = "async_session"
    CONNECTION = "connection"
    ASYNC_CONNECTION = "async_connection"


class ValueSource(StrEnum):
    LITERAL = "literal"  # a string literal or a module constant
    DYNAMIC = "dynamic"  # settings, environment, function results ...
    ABSENT = "absent"


class SessionKind(StrEnum):
    SYNC = "sync"
    ASYNC = "async"
    UNKNOWN = "unknown"


class ProviderShape(StrEnum):
    GENERATOR = "generator"  # yields the session (FastAPI closes it after the response)
    FUNCTION = "function"  # returns the session


class DbUnresolvedKind(StrEnum):
    MODEL_BASE = "model_base"  # a base class that can't be classified, or an external base with attributes
    TABLE_NAME = "table_name"  # computed __tablename__, or none for a mapped class
    RELATIONSHIP_TARGET = "relationship_target"
    DYNAMIC_ATTRIBUTE = "dynamic_attribute"  # declared_attr, column_property, hybrid_property ...
    DYNAMIC_MODEL = "dynamic_model"  # type("X", (Base,), {...})
    UNSUPPORTED = "unsupported"  # registry.mapped, map_imperatively, reflection ...


@dataclass(frozen=True, order=True)
class DbLocation:
    path: str
    line: int


@dataclass(frozen=True)
class DeclarativeBaseInfo:
    base_id: str
    kind: BaseKind
    symbol: str
    location: DbLocation


@dataclass(frozen=True)
class ModelColumn:
    name: str
    #: The database column name when it differs from the attribute (Column("db_name", ...)).
    column_name: str | None
    declaration: ColumnDeclaration
    provenance: Provenance
    #: The class whose body declares it (a mixin, the base, a SQLModel parent, or the model).
    declared_in: str
    #: None when primary_key= is present but not a literal.
    primary_key: bool | None
    nullable: bool | None
    foreign_keys: tuple[str, ...]
    type_expression: str | None
    location: DbLocation


@dataclass(frozen=True)
class ModelRelationship:
    name: str
    target_model: str | None
    target_expression: str
    resolution: TargetResolution
    declared_in: str
    lazy: str | None
    lazy_provenance: Provenance
    #: True for list/set annotations, False for scalar ones, None when unknown.
    collection: bool | None
    uselist: bool | None
    back_populates: str | None
    backref: str | None
    secondary: str | None
    location: DbLocation


@dataclass(frozen=True)
class OrmModel:
    model_id: str
    name: str
    symbol: str
    flavor: OrmFlavor
    #: The declarative base (None for SQLModel, whose registry is SQLModel's).
    base_id: str | None
    location: DbLocation
    table_name: str | None
    table_name_provenance: Provenance
    parents: tuple[str, ...]
    inheritance: InheritanceKind
    columns: tuple[ModelColumn, ...]
    relationships: tuple[ModelRelationship, ...]
    primary_key: tuple[str, ...]
    primary_key_provenance: Provenance
    #: False when something about this model is dynamic or unresolved.
    complete: bool


@dataclass(frozen=True)
class DbOption:
    name: str
    #: A literal value, a dotted external name, or None when not static.
    value: str | None


@dataclass(frozen=True)
class DbObject:
    object_id: str
    kind: DbObjectKind
    symbol: str
    location: DbLocation
    #: For factories and sessions: the engine they are bound to, when static.
    bound_to: str | None
    url_source: ValueSource
    #: Only the scheme of a literal URL (e.g. "postgresql+asyncpg"), never the rest.
    url_scheme: str | None
    options: tuple[DbOption, ...]


@dataclass(frozen=True)
class SessionProvider:
    symbol: str
    location: DbLocation
    shape: ProviderShape
    session_kind: SessionKind
    #: explicit: the body yields/returns a resolved session; inferred: only the return annotation says so.
    provenance: Provenance
    #: The factory or session object id the provider gets its session from, when static.
    source: str | None


@dataclass(frozen=True)
class RouteSession:
    route_id: str
    provider: str
    #: The endpoint parameter (or None for decorator/router/app-level dependencies).
    parameter: str | None
    #: Dependency functions from the route down to the provider (inclusive).
    chain: tuple[str, ...]


@dataclass(frozen=True)
class DbUnresolved:
    kind: DbUnresolvedKind
    location: DbLocation
    expression: str
    reason: str


@dataclass(frozen=True)
class DatabaseModel:
    schema_version: int
    bases: tuple[DeclarativeBaseInfo, ...]
    models: tuple[OrmModel, ...]
    objects: tuple[DbObject, ...]
    session_providers: tuple[SessionProvider, ...]
    route_sessions: tuple[RouteSession, ...]
    unresolved: tuple[DbUnresolved, ...]

    @staticmethod
    def empty() -> DatabaseModel:
        return DatabaseModel(DATABASE_SCHEMA_VERSION, (), (), (), (), (), ())
