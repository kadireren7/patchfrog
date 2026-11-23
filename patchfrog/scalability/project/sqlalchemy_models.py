"""Static SQLAlchemy / SQLModel structure discovery (Milestone 2.4, ADR-004).

Parse only, like route discovery, on the same parsed modules and the same
symbolic resolver (:class:`DatabaseDiscovery` extends
:class:`~patchfrog.scalability.project.fastapi_routes.RouteDiscovery`
through its hooks). Nothing is imported, no model class is created, no
migration runs, and no database is contacted.

What is recognised (DISCOVERY.md §9 has the full list):

* declarative bases: ``class Base(DeclarativeBase)`` (and ``DeclarativeBaseNoMeta``),
  ``Base = declarative_base()``; SQLModel classes with ``table=True``;
* models: any repository class whose bases resolve to a declarative base or
  a mapped class, with mixins, abstract classes and inheritance;
* columns: ``mapped_column``, ``Column``, annotation-only ``Mapped[...]``,
  SQLModel ``Field`` and annotation fields; ``__tablename__``/``__table__``;
* relationships: ``relationship``/SQLModel ``Relationship``, targets by
  class, string or ``Mapped[...]`` annotation, ``lazy``, ``back_populates``,
  ``backref``, ``secondary``, ``uselist``;
* engines, session factories and sessions bound to names, inline sessions,
  ``with``-bound sessions and connections, and functions that yield or
  return sessions (session providers), linked to the routes that depend on
  them.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace

from patchfrog.scalability.project.db_domain import (
    DATABASE_SCHEMA_VERSION,
    BaseKind,
    ColumnDeclaration,
    DatabaseModel,
    DbLocation,
    DbObject,
    DbObjectKind,
    DbOption,
    DbUnresolved,
    DbUnresolvedKind,
    DeclarativeBaseInfo,
    InheritanceKind,
    ModelColumn,
    ModelRelationship,
    OrmFlavor,
    OrmModel,
    Provenance,
    ProviderShape,
    RouteSession,
    SessionKind,
    SessionProvider,
    TargetResolution,
    ValueSource,
)
from patchfrog.scalability.project.domain import (
    DependencyFunction,
    DependencyResolution,
    Location,
    Route,
)
from patchfrog.scalability.project.fastapi_routes import (
    _CANDIDATE,
    _MAX_EXPRESSION_CHARS,
    DiscoveryLimits,
    RouteDiscovery,
    _Class,
    _Ext,
    _ExtInstance,
    _Object,
    _Other,
    _own_nodes,
    _Scope,
    _Str,
    _Unknown,
    _Value,
)

_DECLARATIVE_BASE_CLASSES = frozenset(
    f"sqlalchemy.orm.{m}{n}" for m in ("", "decl_api.") for n in ("DeclarativeBase", "DeclarativeBaseNoMeta")
)
_DECLARATIVE_BASE_FUNCTIONS = frozenset(
    {"sqlalchemy.orm.declarative_base", "sqlalchemy.orm.decl_api.declarative_base", "sqlalchemy.ext.declarative.declarative_base"}
)
_SQLMODEL = frozenset({"sqlmodel.SQLModel", "sqlmodel.main.SQLModel"})
_MAPPED = frozenset({"sqlalchemy.orm.Mapped", "sqlalchemy.orm.base.Mapped"})
_MAPPED_COLUMN = frozenset({"sqlalchemy.orm.mapped_column", "sqlalchemy.orm._orm_constructors.mapped_column"})
_COLUMN = frozenset({"sqlalchemy.Column", "sqlalchemy.schema.Column", "sqlalchemy.sql.schema.Column", "sqlmodel.Column"})
_RELATIONSHIP = frozenset(
    {"sqlalchemy.orm.relationship", "sqlalchemy.orm.relationships.relationship", "sqlalchemy.orm._orm_constructors.relationship"}
)
_SQLMODEL_FIELD = frozenset({"sqlmodel.Field", "sqlmodel.main.Field"})
_SQLMODEL_RELATIONSHIP = frozenset({"sqlmodel.Relationship", "sqlmodel.main.Relationship"})
_FOREIGN_KEY = frozenset({"sqlalchemy.ForeignKey", "sqlalchemy.schema.ForeignKey", "sqlalchemy.sql.schema.ForeignKey", "sqlmodel.ForeignKey"})
_TABLE = frozenset({"sqlalchemy.Table", "sqlalchemy.schema.Table", "sqlalchemy.sql.schema.Table", "sqlmodel.Table"})
_DYNAMIC_CALLS = frozenset(
    {
        "sqlalchemy.orm.column_property", "sqlalchemy.orm.synonym", "sqlalchemy.orm.composite",
        "sqlalchemy.orm.deferred", "sqlalchemy.orm.query_expression", "sqlalchemy.orm.dynamic_loader",
        "sqlalchemy.ext.associationproxy.association_proxy",
    }
)
_DYNAMIC_DECORATORS = frozenset(
    {
        "sqlalchemy.orm.declared_attr", "sqlalchemy.orm.declared_attr.directive", "sqlalchemy.orm.declared_attr.cascading",
        "sqlalchemy.ext.declarative.declared_attr", "sqlalchemy.ext.hybrid.hybrid_property", "sqlalchemy.ext.hybrid.hybrid_method",
    }
)
_AS_DECLARATIVE = frozenset({"sqlalchemy.orm.as_declarative", "sqlalchemy.ext.declarative.as_declarative"})
#: External bases that add no mapped attributes.
_HARMLESS_BASES = frozenset(
    {"sqlalchemy.orm.MappedAsDataclass", "sqlalchemy.ext.asyncio.AsyncAttrs", "typing.Generic", "abc.ABC", "enum.Enum"}
)
_ENGINES = {
    "sqlalchemy.create_engine": DbObjectKind.ENGINE,
    "sqlalchemy.engine.create_engine": DbObjectKind.ENGINE,
    "sqlmodel.create_engine": DbObjectKind.ENGINE,
    "sqlalchemy.ext.asyncio.create_async_engine": DbObjectKind.ASYNC_ENGINE,
    "sqlalchemy.ext.asyncio.engine.create_async_engine": DbObjectKind.ASYNC_ENGINE,
}
_FACTORIES = {
    "sqlalchemy.orm.sessionmaker": DbObjectKind.SESSIONMAKER,
    "sqlalchemy.orm.session.sessionmaker": DbObjectKind.SESSIONMAKER,
    "sqlalchemy.ext.asyncio.async_sessionmaker": DbObjectKind.ASYNC_SESSIONMAKER,
    "sqlalchemy.ext.asyncio.session.async_sessionmaker": DbObjectKind.ASYNC_SESSIONMAKER,
    "sqlalchemy.orm.scoped_session": DbObjectKind.SCOPED_SESSION,
}
_SESSIONS = {
    "sqlalchemy.orm.Session": DbObjectKind.SESSION,
    "sqlalchemy.orm.session.Session": DbObjectKind.SESSION,
    "sqlmodel.Session": DbObjectKind.SESSION,
    "sqlmodel.orm.session.Session": DbObjectKind.SESSION,
    "sqlalchemy.ext.asyncio.AsyncSession": DbObjectKind.ASYNC_SESSION,
    "sqlalchemy.ext.asyncio.session.AsyncSession": DbObjectKind.ASYNC_SESSION,
    "sqlmodel.ext.asyncio.session.AsyncSession": DbObjectKind.ASYNC_SESSION,
}
_CONSTRUCTORS = {**_ENGINES, **_FACTORIES, **_SESSIONS}
_SESSION_LIKE = frozenset({DbObjectKind.SESSION, DbObjectKind.ASYNC_SESSION, DbObjectKind.SCOPED_SESSION})
_ENTERABLE = _SESSION_LIKE | {DbObjectKind.CONNECTION, DbObjectKind.ASYNC_CONNECTION}
_FACTORY_PRODUCT = {
    DbObjectKind.SESSIONMAKER: DbObjectKind.SESSION,
    DbObjectKind.ASYNC_SESSIONMAKER: DbObjectKind.ASYNC_SESSION,
    DbObjectKind.SCOPED_SESSION: DbObjectKind.SESSION,
}
#: Allowlisted options; values are kept only when literal. Never the URL, credentials or connect_args values.
_ENGINE_OPTIONS = frozenset(
    {"echo", "echo_pool", "future", "isolation_level", "max_overflow", "pool_pre_ping", "pool_recycle", "pool_size", "pool_timeout", "pool_use_lifo", "poolclass"}
)
_FACTORY_OPTIONS = frozenset({"autobegin", "autocommit", "autoflush", "class_", "expire_on_commit"})
_PRESENCE_ONLY = frozenset({"connect_args"})
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]{0,40})://")
_COLLECTIONS = frozenset({"list", "List", "set", "Set", "Sequence", "tuple", "Tuple", "Collection", "Iterable", "MutableSequence"})
_SPECIAL = frozenset({"__tablename__", "__table__", "__abstract__", "__table_args__", "__mapper_args__", "model_config", "__allow_unmapped__"})
_PROVIDER_WRAPPERS = frozenset({"Generator", "Iterator", "Iterable", "AsyncGenerator", "AsyncIterator", "AsyncIterable"})

#: Route-discovery candidates, plus files with ORM or session construction tokens.
_DB_CANDIDATE = re.compile(
    _CANDIDATE.pattern
    + rb"|\bsqlalchemy\b|\bsqlmodel\b|__tablename__|__abstract__|__table__|\bMapped\[|mapped_column|relationship\s*\("
    + rb"|Relationship\s*\(|sessionmaker|create_(?:async_)?engine|\b\w*[sS]ession\w*\s*\("
)


@dataclass(frozen=True)
class _BaseRef(_Object):
    base_id: str


@dataclass(frozen=True)
class _DbRef(_Object):
    kind: DbObjectKind
    object_id: str | None  # None for anonymous (inline) objects
    source: str | None  # the factory or engine it came from, when static


@dataclass(frozen=True)
class _DbMethod(_Object):
    ref: _DbRef
    name: str


@dataclass
class _ClassInfo:
    role: str  # base | model | abstract | sqlmodel_data | plain | unknown
    flavor: OrmFlavor | None = None
    base_id: str | None = None
    parents: list[str] = field(default_factory=list)  # mapped (non-abstract) model symbols
    contributors: list[str] = field(default_factory=list)  # classes whose bodies contribute attributes
    external: list[tuple[ast.expr, str]] = field(default_factory=list)
    unknown: list[tuple[ast.expr, str]] = field(default_factory=list)


@dataclass
class _Attr:
    kind: str  # column | relationship | dynamic | tablename | table
    owner: str
    node: ast.AST
    scope: _Scope
    column: ModelColumn | None = None
    relationship: tuple[ModelRelationship, ast.expr | None, str] | None = None  # (draft, target node, target text)
    table_name: str | None = None
    reason: str = ""


class DatabaseDiscovery(RouteDiscovery):
    """Routes plus database structure, in one pass over shared parsed modules."""

    _candidate_pattern = _DB_CANDIDATE

    def __init__(
        self,
        python_paths: Sequence[str],
        read: Callable[[str], bytes | None],
        limits: DiscoveryLimits,
        checkpoint: Callable[[], None],
    ) -> None:
        super().__init__(python_paths, read, limits, checkpoint)
        self._bases: dict[str, DeclarativeBaseInfo] = {}
        self._objects: dict[str, DbObject] = {}
        self._class_info: dict[str, _ClassInfo] = {}
        self._classifying: set[str] = set()
        self._db_unresolved: list[DbUnresolved] = []
        self._incomplete_models: set[str] = set()
        self.omitted_db_unresolved = 0

    # --- resolver hooks ------------------------------------------------------------------

    def _construct(self, func: _Value, call: ast.Call, scope: _Scope, name: str, depth: int) -> _Value | None:
        routed = super()._construct(func, call, scope, name, depth)
        if routed is not None or not isinstance(func, _Ext):
            return routed
        module = scope.module
        symbol = f"{module.name}:{scope.qualname}{name}"
        if func.dotted in _DECLARATIVE_BASE_FUNCTIONS:
            base_id = f"base:{symbol}"
            self._bases.setdefault(
                base_id, DeclarativeBaseInfo(base_id, BaseKind.DECLARATIVE_BASE_FUNCTION, symbol, self._dbloc(module.path, call))
            )
            return _BaseRef(base_id)
        kind = self._constructed_kind(func.dotted, call, scope)
        if kind is None:
            return None
        object_id = f"{kind.value}:{symbol}"
        if object_id not in self._objects:
            self._objects[object_id] = self._db_object(object_id, kind, symbol, call, scope)
        return _DbRef(kind, object_id, self._objects[object_id].bound_to)

    def _call_value(self, func: _Value, call: ast.Call, scope: _Scope, depth: int) -> _Value | None:
        called = super()._call_value(func, call, scope, depth)
        if called is not None:
            return called
        if isinstance(func, _Ext):
            kind = self._constructed_kind(func.dotted, call, scope)
            if kind is not None:
                return _DbRef(kind, None, self._bound_engine(call, scope))
        if isinstance(func, _DbRef) and func.kind in _FACTORY_PRODUCT:
            return _DbRef(_FACTORY_PRODUCT[func.kind], None, func.object_id)
        if isinstance(func, _DbMethod):
            ref = func.ref
            if func.name == "begin" and ref.kind in _FACTORY_PRODUCT:
                return _DbRef(_FACTORY_PRODUCT[ref.kind], None, ref.object_id)
            if func.name in ("connect", "begin") and ref.kind in (DbObjectKind.ENGINE, DbObjectKind.ASYNC_ENGINE):
                kind = DbObjectKind.ASYNC_CONNECTION if ref.kind is DbObjectKind.ASYNC_ENGINE else DbObjectKind.CONNECTION
                return _DbRef(kind, None, ref.object_id)
        return None

    def _attribute_value(self, base: _Value, attr: str) -> _Value | None:
        if isinstance(base, _DbRef) and attr in ("begin", "connect"):
            return _DbMethod(base, attr)
        return super()._attribute_value(base, attr)

    def _enter(self, value: _Value) -> _Value | None:
        if isinstance(value, _DbRef) and value.kind in _ENTERABLE:
            return value
        return super()._enter(value)

    def _returnable(self, value: _Value) -> bool:
        return super()._returnable(value) or isinstance(value, _DbRef | _BaseRef)

    # --- engines, factories, sessions --------------------------------------------------------

    def _constructed_kind(self, dotted: str, call: ast.Call, scope: _Scope) -> DbObjectKind | None:
        kind = _CONSTRUCTORS.get(dotted)
        if kind is DbObjectKind.SESSIONMAKER:
            class_ = _keyword(call, "class_")
            if class_ is not None:
                value = self._eval(class_, scope, 0)
                if isinstance(value, _Ext) and _SESSIONS.get(value.dotted) is DbObjectKind.ASYNC_SESSION:
                    return DbObjectKind.ASYNC_SESSIONMAKER
        return kind

    def _bound_engine(self, call: ast.Call, scope: _Scope) -> str | None:
        node = _keyword(call, "bind") or (call.args[0] if call.args else None)
        if node is None:
            return None
        value = self._eval(node, scope, 0)
        if isinstance(value, _DbRef) and value.kind in (DbObjectKind.ENGINE, DbObjectKind.ASYNC_ENGINE):
            return value.object_id
        if isinstance(value, _DbRef) and value.kind in _FACTORY_PRODUCT:  # scoped_session(sessionmaker(...))
            return value.object_id
        return None

    def _db_object(self, object_id: str, kind: DbObjectKind, symbol: str, call: ast.Call, scope: _Scope) -> DbObject:
        url_source, scheme = ValueSource.ABSENT, None
        bound = None
        if kind in (DbObjectKind.ENGINE, DbObjectKind.ASYNC_ENGINE):
            allowed = _ENGINE_OPTIONS
            url = _keyword(call, "url") or (call.args[0] if call.args else None)
            if url is not None:
                value = self._eval(url, scope, 0)
                if isinstance(value, _Str):
                    url_source = ValueSource.LITERAL
                    match = _SCHEME.match(value.value)
                    scheme = match.group(1) if match else None
                else:
                    url_source = ValueSource.DYNAMIC
        else:
            allowed = _FACTORY_OPTIONS
            bound = self._bound_engine(call, scope)
        options = []
        for keyword in call.keywords:
            if keyword.arg in _PRESENCE_ONLY:
                options.append(DbOption(keyword.arg, "<set>"))
            elif keyword.arg in allowed:
                options.append(DbOption(keyword.arg, self._option_value(keyword.value, scope)))
        return DbObject(
            object_id, kind, symbol, self._dbloc(scope.module.path, call), bound, url_source, scheme,
            tuple(sorted(options, key=lambda o: o.name)),
        )

    def _option_value(self, node: ast.expr, scope: _Scope) -> str | None:
        if (isinstance(node, ast.Constant) and isinstance(node.value, bool | int | float)) or (
            isinstance(node, ast.Constant) and node.value is None
        ):
            return repr(node.value)
        value = self._eval(node, scope, 0)
        if isinstance(value, _Str):
            return value.value[:64]
        if isinstance(value, _Ext):
            return value.dotted
        return None

    # --- recording -------------------------------------------------------------------------------

    def _loc_of(self, scope: _Scope, node: ast.AST) -> Location:
        return Location(scope.module.path, int(getattr(node, "lineno", 1) or 1))

    def _dbloc(self, path: str, node: ast.AST) -> DbLocation:
        return DbLocation(path, int(getattr(node, "lineno", 1) or 1))

    def _db_unresolved_entry(self, kind: DbUnresolvedKind, scope: _Scope, node: ast.AST, reason: str, model: str | None = None) -> None:
        if model is not None:
            self._incomplete_models.add(model)
        if len(self._db_unresolved) >= self._limits.max_unresolved:
            self.omitted_db_unresolved += 1
            return
        text = " ".join(_unparse(node).split())
        if len(text) > _MAX_EXPRESSION_CHARS:
            text = text[: _MAX_EXPRESSION_CHARS - 1] + "…"
        self._db_unresolved.append(DbUnresolved(kind, self._dbloc(scope.module.path, node), text, reason))

    # --- class classification ---------------------------------------------------------------------

    def _classify(self, symbol: str, depth: int = 0) -> _ClassInfo:
        if symbol in self._class_info:
            return self._class_info[symbol]
        if symbol in self._classifying or depth > self._limits.max_resolution_depth:
            return _ClassInfo("unknown")
        node, scope = self._classes[symbol]
        self._classifying.add(symbol)
        try:
            info = self._compute_class(symbol, node, scope, depth)
        finally:
            self._classifying.discard(symbol)
        self._class_info[symbol] = info
        return info

    def _compute_class(self, symbol: str, node: ast.ClassDef, scope: _Scope, depth: int) -> _ClassInfo:
        info = _ClassInfo("plain")
        is_base = sqlmodel = False
        for base in node.bases:
            value = self._eval(base, scope, 0)
            if isinstance(value, _Ext):
                if value.dotted in _DECLARATIVE_BASE_CLASSES:
                    is_base = True
                elif value.dotted in _SQLMODEL:
                    sqlmodel = True
                elif value.dotted not in _HARMLESS_BASES:
                    info.external.append((base, value.dotted))
            elif isinstance(value, _BaseRef):
                info.base_id = info.base_id or value.base_id
            elif isinstance(value, _Class):
                parent = self._classify(value.symbol, depth + 1)
                if parent.role == "base":
                    info.base_id = info.base_id or f"base:{value.symbol}"
                    info.contributors.extend(parent.contributors)
                elif parent.role == "model":
                    info.base_id = info.base_id or parent.base_id
                    if parent.flavor is OrmFlavor.SQLMODEL:
                        sqlmodel = True
                    info.parents.append(value.symbol)
                elif parent.role == "abstract":
                    info.base_id = info.base_id or parent.base_id
                    info.contributors.extend(parent.contributors)
                elif parent.role == "sqlmodel_data":
                    sqlmodel = True
                    info.contributors.extend(parent.contributors)
                elif parent.role == "plain":
                    info.contributors.extend(parent.contributors)
                    info.external.extend(parent.external)
                else:
                    info.unknown.append((base, "base class could not be classified"))
            elif isinstance(value, _Unknown | _ExtInstance):
                info.unknown.append((base, value.reason if isinstance(value, _Unknown) else f"object returned by {value.dotted}"))
        info.contributors = _dedupe([*info.contributors, symbol])
        abstract = _class_flag(node, "__abstract__")
        if is_base:
            info.role, info.flavor, info.base_id = "base", OrmFlavor.DECLARATIVE, f"base:{symbol}"
            self._bases.setdefault(
                info.base_id, DeclarativeBaseInfo(info.base_id, BaseKind.DECLARATIVE_BASE_CLASS, symbol, self._dbloc(scope.module.path, node))
            )
        elif sqlmodel:
            info.flavor = OrmFlavor.SQLMODEL
            table = _keyword_class(node, "table")
            info.role = "model" if table else "sqlmodel_data"
            if info.parents and table:
                # SQLModel table models inheriting other table models aren't supported by SQLModel itself.
                self._db_unresolved_entry(DbUnresolvedKind.UNSUPPORTED, scope, node, "SQLModel table model inheriting a table model", symbol)
        elif info.base_id is not None:
            info.flavor = OrmFlavor.DECLARATIVE
            info.role = "abstract" if abstract else "model"
        elif info.unknown:
            info.role = "unknown"
        return info

    # --- model extraction -------------------------------------------------------------------------

    def analyse(self, routes: Sequence[Route], dependency_functions: Sequence[DependencyFunction]) -> DatabaseModel:
        self._evaluate_constructions()
        for symbol in sorted(self._classes):
            self._checkpoint()
            self._classify(symbol)
        models: dict[str, tuple[OrmModel, list[_Attr]]] = {}
        for symbol in sorted(self._classes):
            info = self._class_info.get(symbol)
            node, scope = self._classes[symbol]
            self._check_class_decorators(symbol, node, scope)
            if info is None:
                continue
            if info.role == "unknown" and _looks_mapped(node, strict=False):
                for base, reason in info.unknown:
                    self._db_unresolved_entry(DbUnresolvedKind.MODEL_BASE, scope, base, f"possible model with an unresolvable base: {reason}", symbol)
            elif info.role == "plain" and info.external and _looks_mapped(node, strict=True):
                for base, dotted in info.external:
                    self._db_unresolved_entry(
                        DbUnresolvedKind.MODEL_BASE, scope, base, f"possible model: base {dotted} is outside the repository (not analysed)", symbol
                    )
            if info.role != "model":
                continue
            if len(models) >= self._limits.max_models:
                self._limit("max_models", self._loc_of(scope, node))
                break
            models[symbol] = self._build_model(symbol, node, scope, info)
        self._check_dynamic_models()
        finished = self._resolve_relationships(models)
        providers = self._session_providers(dependency_functions)
        if self.omitted_db_unresolved:
            self._limit(f"max_unresolved: {self.omitted_db_unresolved} further database unresolved references omitted")
        return DatabaseModel(
            schema_version=DATABASE_SCHEMA_VERSION,
            bases=tuple(sorted(self._bases.values(), key=lambda b: b.base_id)),
            models=tuple(sorted(finished, key=lambda m: m.model_id)),
            objects=tuple(sorted(self._objects.values(), key=lambda o: o.object_id)),
            session_providers=tuple(sorted(providers.values(), key=lambda p: p.symbol)),
            route_sessions=self._route_sessions(routes, dependency_functions, providers),
            unresolved=tuple(sorted(set(self._db_unresolved), key=lambda u: (u.location, u.kind.value, u.expression, u.reason))),
        )

    def _build_model(self, symbol: str, node: ast.ClassDef, scope: _Scope, info: _ClassInfo) -> tuple[OrmModel, list[_Attr]]:
        assert info.flavor is not None
        attrs: dict[str, _Attr] = {}
        for contributor in info.contributors:  # base classes first, the model itself last (it wins)
            c_node, c_scope = self._classes[contributor]
            for name, attr in self._class_attributes(contributor, c_node, c_scope, info.flavor, symbol):
                attrs[name] = attr
        for base, dotted in info.external:
            self._db_unresolved_entry(DbUnresolvedKind.MODEL_BASE, scope, base, f"external base {dotted} may contribute mapped attributes (not analysed)", symbol)
        for base, reason in info.unknown:
            self._db_unresolved_entry(DbUnresolvedKind.MODEL_BASE, scope, base, reason, symbol)

        columns = [a.column for a in attrs.values() if a.kind == "column" and a.column is not None]
        # __table__ wins: SQLAlchemy ignores __tablename__ when __table__ is set.
        table_attr = attrs.get("__table__") or attrs.get("__tablename__")
        parents = list(info.parents)
        if table_attr is not None and table_attr.kind in ("tablename", "table") and table_attr.table_name is not None:
            table, table_prov = table_attr.table_name, Provenance.EXPLICIT
            inheritance = InheritanceKind.JOINED if parents else InheritanceKind.NONE
        elif table_attr is not None:
            table, table_prov, inheritance = None, Provenance.UNKNOWN, InheritanceKind.UNKNOWN if parents else InheritanceKind.NONE
            self._db_unresolved_entry(DbUnresolvedKind.TABLE_NAME, table_attr.scope, table_attr.node, table_attr.reason or "table name is not static", symbol)
        elif parents:
            table, table_prov, inheritance = None, Provenance.INHERITED, InheritanceKind.SINGLE
        elif info.flavor is OrmFlavor.SQLMODEL:
            table, table_prov, inheritance = node.name.lower(), Provenance.INFERRED, InheritanceKind.NONE
        else:
            table, table_prov, inheritance = None, Provenance.UNKNOWN, InheritanceKind.NONE
            self._db_unresolved_entry(DbUnresolvedKind.TABLE_NAME, scope, node, "mapped class has no __tablename__ or __table__", symbol)

        if any(c.primary_key is None for c in columns):
            primary_key: tuple[str, ...] = ()
            pk_prov = Provenance.UNKNOWN
        elif any(c.primary_key for c in columns):
            primary_key, pk_prov = tuple(c.name for c in columns if c.primary_key), Provenance.EXPLICIT
        elif parents:
            primary_key, pk_prov = (), Provenance.INHERITED
        else:
            primary_key, pk_prov = (), Provenance.UNKNOWN
        model = OrmModel(
            model_id=f"model:{symbol}",
            name=node.name,
            symbol=symbol,
            flavor=info.flavor,
            base_id=info.base_id if info.flavor is OrmFlavor.DECLARATIVE else None,
            location=self._dbloc(scope.module.path, node),
            table_name=table,
            table_name_provenance=table_prov,
            parents=tuple(f"model:{p}" for p in parents),
            inheritance=inheritance,
            columns=tuple(sorted(columns, key=lambda c: (c.location, c.name))),
            relationships=(),
            primary_key=primary_key,
            primary_key_provenance=pk_prov,
            complete=True,
        )
        return model, [a for a in attrs.values() if a.kind == "relationship"]

    def _class_attributes(
        self, owner: str, node: ast.ClassDef, scope: _Scope, flavor: OrmFlavor, model: str
    ) -> Iterable[tuple[str, _Attr]]:
        class_scope = scope  # names in a class body resolve in the enclosing scope (bases and imports)
        for stmt in node.body:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                for decorator in stmt.decorator_list:
                    target = decorator.func if isinstance(decorator, ast.Call) else decorator
                    decorated = self._eval(target, class_scope, 0)
                    if isinstance(decorated, _Ext) and decorated.dotted in _DYNAMIC_DECORATORS:
                        kind = "tablename" if stmt.name == "__tablename__" else "dynamic"
                        reason = f"{stmt.name} is computed by @{decorated.dotted.rsplit('.', 1)[-1]}"
                        if kind == "dynamic":
                            self._db_unresolved_entry(DbUnresolvedKind.DYNAMIC_ATTRIBUTE, class_scope, stmt, reason, model)
                        yield stmt.name, _Attr(kind, owner, stmt, class_scope, reason=reason)
                continue
            annotation: ast.expr | None
            value: ast.expr | None
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                name, annotation, value = stmt.targets[0].id, None, stmt.value
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                name, annotation, value = stmt.target.id, stmt.annotation, stmt.value
            else:
                continue
            parsed = self._attribute(owner, name, annotation, value, stmt, class_scope, flavor, model)
            if parsed is not None:
                yield name, parsed

    def _attribute(
        self,
        owner: str,
        name: str,
        annotation: ast.expr | None,
        value: ast.expr | None,
        stmt: ast.stmt,
        scope: _Scope,
        flavor: OrmFlavor,
        model: str,
    ) -> _Attr | None:
        location = self._dbloc(scope.module.path, stmt)
        if name == "__tablename__":
            literal = self._eval(value, scope, 0) if value is not None else None
            return _Attr("tablename", owner, stmt, scope, table_name=literal.value if isinstance(literal, _Str) else None, reason="__tablename__ is not a static string")
        if name == "__table__" and isinstance(value, ast.Call):
            table_func = self._eval(value.func, scope, 0)
            if isinstance(table_func, _Ext) and table_func.dotted in _TABLE:
                if _keyword(value, "autoload_with") is not None or _keyword(value, "autoload") is not None:
                    self._db_unresolved_entry(DbUnresolvedKind.UNSUPPORTED, scope, value, "reflected table: columns come from the database", model)
                first = self._eval(value.args[0], scope, 0) if value.args else None
                return _Attr("table", owner, stmt, scope, table_name=first.value if isinstance(first, _Str) else None, reason="Table() name is not static")
            return _Attr("table", owner, stmt, scope, reason="__table__ is not a Table() call")
        if name in _SPECIAL:
            return None
        func = self._eval(value.func, scope, 0) if isinstance(value, ast.Call) else None
        dotted = func.dotted if isinstance(func, _Ext) else None
        assert value is None or isinstance(value, ast.expr)
        if dotted in _MAPPED_COLUMN or dotted in _COLUMN:
            assert isinstance(value, ast.Call)
            declaration = ColumnDeclaration.MAPPED_COLUMN if dotted in _MAPPED_COLUMN else ColumnDeclaration.COLUMN
            return _Attr("column", owner, stmt, scope, column=self._column(name, owner, declaration, Provenance.EXPLICIT, value, annotation, scope, location))
        if dotted in _RELATIONSHIP or dotted in _SQLMODEL_RELATIONSHIP:
            assert isinstance(value, ast.Call)
            return _Attr("relationship", owner, stmt, scope, relationship=self._relationship(name, owner, value, annotation, scope, location, dotted in _SQLMODEL_RELATIONSHIP))
        if dotted in _DYNAMIC_CALLS:
            assert dotted is not None
            reason = f"{name} is a {dotted.rsplit('.', 1)[-1]}()"
            self._db_unresolved_entry(DbUnresolvedKind.DYNAMIC_ATTRIBUTE, scope, stmt, reason, model)
            return _Attr("dynamic", owner, stmt, scope, reason=reason)
        if flavor is OrmFlavor.SQLMODEL and annotation is not None and not name.startswith("_") and not _is_classvar(annotation, self, scope):
            if dotted in _SQLMODEL_FIELD:
                assert isinstance(value, ast.Call)
                return _Attr("column", owner, stmt, scope, column=self._column(name, owner, ColumnDeclaration.SQLMODEL_FIELD, Provenance.EXPLICIT, value, annotation, scope, location))
            return _Attr("column", owner, stmt, scope, column=self._column(name, owner, ColumnDeclaration.SQLMODEL_ANNOTATION, Provenance.INFERRED, None, annotation, scope, location))
        if value is None and annotation is not None and isinstance(annotation, ast.Subscript):
            base = self._eval(annotation.value, scope, 0)
            if isinstance(base, _Ext) and base.dotted in _MAPPED:
                return _Attr("column", owner, stmt, scope, column=self._column(name, owner, ColumnDeclaration.MAPPED_ANNOTATION, Provenance.INFERRED, None, annotation, scope, location))
        return None

    def _column(
        self,
        name: str,
        owner: str,
        declaration: ColumnDeclaration,
        provenance: Provenance,
        call: ast.Call | None,
        annotation: ast.expr | None,
        scope: _Scope,
        location: DbLocation,
    ) -> ModelColumn:
        primary_key: bool | None = False
        nullable: bool | None = None
        foreign_keys: list[str] = []
        column_name = None
        type_expression = None
        if annotation is not None:
            inner = annotation.slice if isinstance(annotation, ast.Subscript) and declaration is not ColumnDeclaration.SQLMODEL_FIELD and declaration is not ColumnDeclaration.SQLMODEL_ANNOTATION else annotation
            type_expression = _short(_unparse(inner))
        calls = [call] if call is not None else []
        if call is not None and declaration is ColumnDeclaration.SQLMODEL_FIELD:
            sa_column = _keyword(call, "sa_column")
            if isinstance(sa_column, ast.Call):
                calls.append(sa_column)
            fk = _keyword(call, "foreign_key")
            fk_value = self._eval(fk, scope, 0) if fk is not None else None
            if isinstance(fk_value, _Str):
                foreign_keys.append(fk_value.value)
        pk_unknown = False
        for item in calls:
            pk_node = _keyword(item, "primary_key")
            if pk_node is not None:
                if isinstance(pk_node, ast.Constant) and isinstance(pk_node.value, bool):
                    primary_key = primary_key or pk_node.value
                else:
                    pk_unknown = True
            null_node = _keyword(item, "nullable")
            if isinstance(null_node, ast.Constant) and isinstance(null_node.value, bool):
                nullable = null_node.value
            for index, arg in enumerate(item.args):
                value = self._eval(arg, scope, 0)
                if index == 0 and isinstance(value, _Str) and declaration is ColumnDeclaration.COLUMN:
                    column_name = value.value
                    continue
                if isinstance(arg, ast.Call):
                    fk_func = self._eval(arg.func, scope, 0)
                    if isinstance(fk_func, _Ext) and fk_func.dotted in _FOREIGN_KEY and arg.args:
                        target = self._eval(arg.args[0], scope, 0)
                        if isinstance(target, _Str):
                            foreign_keys.append(target.value)
                        continue
                if type_expression is None and not isinstance(value, _Str):
                    type_expression = _short(_unparse(arg))
        return ModelColumn(
            name=name,
            column_name=column_name if column_name != name else None,
            declaration=declaration,
            provenance=provenance,
            declared_in=owner,
            primary_key=None if pk_unknown else primary_key,
            nullable=nullable,
            foreign_keys=tuple(foreign_keys),
            type_expression=type_expression,
            location=location,
        )

    def _relationship(
        self,
        name: str,
        owner: str,
        call: ast.Call,
        annotation: ast.expr | None,
        scope: _Scope,
        location: DbLocation,
        sqlmodel: bool,
    ) -> tuple[ModelRelationship, ast.expr | None, str]:
        options: dict[str, ast.expr] = {k.arg: k.value for k in call.keywords if k.arg}
        lazy_node: ast.expr | None = options.get("lazy")
        lazy_known = True
        if sqlmodel:
            sa_rel = options.get("sa_relationship")
            kwargs = options.get("sa_relationship_kwargs")
            if isinstance(sa_rel, ast.Call):
                options = {**options, **{k.arg: k.value for k in sa_rel.keywords if k.arg}}
                lazy_node = options.get("lazy")
            elif isinstance(kwargs, ast.Dict):
                for key, item in zip(kwargs.keys, kwargs.values, strict=True):
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        options[key.value] = item
                lazy_node = options.get("lazy")
            elif kwargs is not None:
                lazy_known = False
        target_node: ast.expr | None = None if sqlmodel else (call.args[0] if call.args else options.get("argument"))
        collection: bool | None = None
        if target_node is None and annotation is not None:
            target_node, collection = _annotation_target(annotation, mapped=not sqlmodel)
        elif annotation is not None:
            _, collection = _annotation_target(annotation, mapped=not sqlmodel)
        target_text = _short(_unparse(target_node)) if target_node is not None else "?"
        if isinstance(target_node, ast.Constant) and isinstance(target_node.value, str):
            target_text = target_node.value
        if not lazy_known:
            lazy, lazy_prov = None, Provenance.UNKNOWN
        elif lazy_node is None:
            lazy, lazy_prov = "select", Provenance.DEFAULT  # SQLAlchemy's documented default
        elif isinstance(lazy_node, ast.Constant) and isinstance(lazy_node.value, str):
            lazy, lazy_prov = lazy_node.value, Provenance.EXPLICIT
        elif isinstance(lazy_node, ast.Constant) and isinstance(lazy_node.value, bool):
            lazy, lazy_prov = str(lazy_node.value), Provenance.EXPLICIT
        else:
            lazy, lazy_prov = None, Provenance.UNKNOWN
        uselist_node = options.get("uselist")
        uselist = uselist_node.value if isinstance(uselist_node, ast.Constant) and isinstance(uselist_node.value, bool) else None
        draft = ModelRelationship(
            name=name,
            target_model=None,
            target_expression=target_text,
            resolution=TargetResolution.UNRESOLVED,
            declared_in=owner,
            lazy=lazy,
            lazy_provenance=lazy_prov,
            collection=collection,
            uselist=uselist,
            back_populates=self._literal(options.get("back_populates"), scope),
            backref=self._literal(options.get("backref"), scope),
            secondary=_short(_unparse(options["secondary"])) if "secondary" in options else None,
            location=location,
        )
        return draft, target_node, target_text

    def _literal(self, node: ast.expr | None, scope: _Scope) -> str | None:
        if node is None:
            return None
        value = self._eval(node, scope, 0)
        return value.value if isinstance(value, _Str) else None

    def _resolve_relationships(self, models: dict[str, tuple[OrmModel, list[_Attr]]]) -> list[OrmModel]:
        by_registry: dict[str, list[OrmModel]] = {}
        for model, _attrs in models.values():
            by_registry.setdefault(_registry(model), []).append(model)
        finished = []
        for symbol, (model, attrs) in sorted(models.items()):
            relationships = []
            for attr in attrs:
                assert attr.relationship is not None
                draft, target_node, target_text = attr.relationship
                candidates: list[OrmModel] = []
                reason = ""
                if isinstance(target_node, ast.Constant) and isinstance(target_node.value, str):
                    text = target_node.value.strip()
                    pool = by_registry.get(_registry(model), [])
                    if "." in text:
                        candidates = [m for m in pool if _dotted_name(m) == text or _dotted_name(m).endswith("." + text)]
                    else:
                        candidates = [m for m in pool if m.name == text]
                    reason = f"no model named {text!r} in the same registry" if not candidates else ""
                elif isinstance(target_node, ast.Name | ast.Attribute):
                    value = self._eval(target_node, attr.scope, 0)
                    if isinstance(value, _Class) and value.symbol in models:
                        candidates = [models[value.symbol][0]]
                    else:
                        reason = value.reason if isinstance(value, _Unknown | _Other) else "target is not a discovered model"
                else:
                    reason = "callable or computed relationship target" if target_node is not None else "no target and no Mapped[...] annotation"
                if len(candidates) == 1:
                    relationships.append(_replace_rel(draft, candidates[0].model_id, TargetResolution.RESOLVED))
                    continue
                resolution = TargetResolution.AMBIGUOUS if len(candidates) > 1 else TargetResolution.UNRESOLVED
                if len(candidates) > 1:
                    reason = f"{len(candidates)} models match: {', '.join(sorted(m.symbol for m in candidates))}"
                self._db_unresolved_entry(DbUnresolvedKind.RELATIONSHIP_TARGET, attr.scope, attr.node, reason or target_text, symbol)
                relationships.append(_replace_rel(draft, None, resolution))
            finished.append(
                replace(
                    model,
                    relationships=tuple(sorted(relationships, key=lambda r: (r.location, r.name))),
                    complete=symbol not in self._incomplete_models,
                )
            )
        return finished

    def _all_scopes(self) -> list[_Scope]:
        scopes = [m.scope for _, m in sorted(self._modules.items()) if m is not None and m.scope is not None]
        return scopes + [f.body_scope for _, f in sorted(self._functions.items())]

    def _evaluate_constructions(self) -> None:
        """Objects are otherwise recorded lazily, when a name is resolved. An
        engine or factory that nothing references is still structure, so
        every named binding whose callee *looks* like a constructor is
        evaluated once (the evaluation decides; the name is only a filter)."""

        for scope in self._all_scopes():
            for bindings in scope.bindings.values():
                for binding in bindings:
                    node = binding.node
                    if binding.kind == "value" and isinstance(node, ast.Call) and _callee_name(node) in _CONSTRUCTOR_NAMES:
                        self._checkpoint()
                        self._eval_binding(binding, 0)

    def _check_class_decorators(self, symbol: str, node: ast.ClassDef, scope: _Scope) -> None:
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            value = self._eval(target, scope, 0)
            registry_method = isinstance(target, ast.Attribute) and target.attr in ("mapped", "mapped_as_dataclass")
            base_value = self._eval(target.value, scope, 0) if isinstance(target, ast.Attribute) else None
            if (isinstance(value, _Ext) and value.dotted in _AS_DECLARATIVE) or (
                registry_method and isinstance(base_value, _ExtInstance) and base_value.dotted.endswith("registry")
            ):
                self._db_unresolved_entry(DbUnresolvedKind.UNSUPPORTED, scope, decorator, "registry/as_declarative decorator mapping is not analysed", symbol)

    def _check_dynamic_models(self) -> None:
        for scope in self._all_scopes():
            for bindings in scope.bindings.values():
                for binding in bindings:
                    node = binding.node
                    if binding.kind != "value" or not isinstance(node, ast.Call):
                        continue
                    if isinstance(node.func, ast.Name) and node.func.id == "type" and len(node.args) == 3:
                        bases = node.args[1].elts if isinstance(node.args[1], ast.Tuple) else []
                        if any(self._is_mapped_base(b, scope) for b in bases):
                            self._db_unresolved_entry(DbUnresolvedKind.DYNAMIC_MODEL, scope, node, "model class created with type(...)")
            for registration, reg_scope in (scope.module.registrations if scope.parent is None else []):
                if isinstance(registration, ast.Call) and isinstance(registration.func, ast.Attribute) and registration.func.attr == "map_imperatively":
                    self._db_unresolved_entry(DbUnresolvedKind.UNSUPPORTED, reg_scope, registration, "imperative mapping is not analysed")

    def _is_mapped_base(self, node: ast.expr, scope: _Scope) -> bool:
        value = self._eval(node, scope, 0)
        if isinstance(value, _BaseRef):
            return True
        if isinstance(value, _Ext):
            return value.dotted in _DECLARATIVE_BASE_CLASSES | _SQLMODEL
        if isinstance(value, _Class):
            return self._classify(value.symbol).role in ("base", "model", "abstract", "sqlmodel_data")
        return False

    # --- session providers ---------------------------------------------------------------------------

    def _session_providers(self, dependency_functions: Sequence[DependencyFunction]) -> dict[str, SessionProvider]:
        candidates = {d.symbol for d in dependency_functions}
        for symbol, function in self._functions.items():
            if function.scope.module.imported_roots & {"sqlalchemy", "sqlmodel"}:
                candidates.add(symbol)
        providers: dict[str, SessionProvider] = {}
        for symbol in sorted(candidates):
            info = self._functions.get(symbol)
            if info is None:
                continue
            self._checkpoint()
            # One walk of the body (measured hot spot): yields and returns together.
            yields: list[ast.expr] = []
            returns: list[ast.expr] = []
            generator = False
            for n in _own_nodes(info.node.body):
                if isinstance(n, ast.Yield | ast.YieldFrom):
                    generator = True
                    if isinstance(n, ast.Yield) and n.value is not None:
                        yields.append(n.value)
                elif isinstance(n, ast.Return) and n.value is not None:
                    returns.append(n.value)
            nodes = yields if generator else returns
            refs = [v for v in (self._eval(n, info.body_scope, 0) for n in nodes) if isinstance(v, _DbRef) and v.kind in _SESSION_LIKE]
            shape = ProviderShape.GENERATOR if generator else ProviderShape.FUNCTION
            location = self._dbloc(info.scope.module.path, info.node)
            if refs:
                kinds = {SessionKind.ASYNC if r.kind is DbObjectKind.ASYNC_SESSION else SessionKind.SYNC for r in refs}
                sources = {r.object_id or r.source for r in refs}
                providers[symbol] = SessionProvider(
                    symbol, location, shape, kinds.pop() if len(kinds) == 1 else SessionKind.UNKNOWN, Provenance.EXPLICIT,
                    sources.pop() if len(sources) == 1 else None,
                )
                continue
            kind = self._annotated_session(info.node.returns, info.scope)
            if kind is not None:
                providers[symbol] = SessionProvider(symbol, location, shape, kind, Provenance.INFERRED, None)
        return providers

    def _annotated_session(self, annotation: ast.expr | None, scope: _Scope) -> SessionKind | None:
        if annotation is None:
            return None
        node = annotation
        if isinstance(node, ast.Subscript):
            wrapper = self._eval(node.value, scope, 0)
            if isinstance(wrapper, _Ext) and wrapper.dotted.rsplit(".", 1)[-1] in _PROVIDER_WRAPPERS:
                node = node.slice.elts[0] if isinstance(node.slice, ast.Tuple) else node.slice
            else:
                return None
        value = self._eval(node, scope, 0)
        if isinstance(value, _Ext) and value.dotted in _SESSIONS:
            return SessionKind.ASYNC if _SESSIONS[value.dotted] is DbObjectKind.ASYNC_SESSION else SessionKind.SYNC
        return None

    def _route_sessions(
        self, routes: Sequence[Route], dependency_functions: Sequence[DependencyFunction], providers: dict[str, SessionProvider]
    ) -> tuple[RouteSession, ...]:
        uses = {d.symbol: d.uses for d in dependency_functions}
        found: set[RouteSession] = set()
        for route in routes:
            for use in route.dependencies:
                if use.resolution is not DependencyResolution.LOCAL_FUNCTION or use.target is None:
                    continue
                stack: list[tuple[str, tuple[str, ...]]] = [(use.target, (use.target,))]
                while stack:
                    symbol, chain = stack.pop()
                    if symbol in providers:
                        found.add(RouteSession(route.route_id, symbol, use.parameter, chain))
                        continue
                    if len(chain) > self._limits.max_dependency_depth:
                        continue
                    for inner in uses.get(symbol, ()):
                        if inner.resolution is DependencyResolution.LOCAL_FUNCTION and inner.target and inner.target not in chain:
                            stack.append((inner.target, (*chain, inner.target)))
        return tuple(sorted(found, key=lambda s: (s.route_id, s.provider, s.parameter or "", s.chain)))


# --- helpers ---------------------------------------------------------------------------------------


_CONSTRUCTOR_NAMES = frozenset(
    {d.rsplit(".", 1)[-1] for d in _CONSTRUCTORS} | {d.rsplit(".", 1)[-1] for d in _DECLARATIVE_BASE_FUNCTIONS}
)


def _callee_name(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


def _unparse(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except (ValueError, RecursionError):  # pragma: no cover - defensive
        return type(node).__name__


def _short(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _keyword_class(node: ast.ClassDef, name: str) -> bool:
    return any(k.arg == name and isinstance(k.value, ast.Constant) and k.value.value is True for k in node.keywords)


def _class_flag(node: ast.ClassDef, name: str) -> bool:
    for stmt in node.body:
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target] if isinstance(stmt, ast.AnnAssign) else []
        value = stmt.value if isinstance(stmt, ast.Assign | ast.AnnAssign) else None
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return isinstance(value, ast.Constant) and value.value is True
    return False


def _looks_mapped(node: ast.ClassDef, *, strict: bool) -> bool:
    """Whether a class body *structurally* uses ORM declarations: a
    ``__tablename__`` assignment, a ``Mapped[...]`` annotation, or a call to
    ``mapped_column``/``relationship`` (``Column`` too unless ``strict``, since
    other libraries use that name). Structural on purpose: a text search
    flags string values such as ``MAPPED_COLUMN = "mapped_column"``."""

    calls = {"mapped_column", "relationship"} | (set() if strict else {"Column"})
    for stmt in node.body:
        if not isinstance(stmt, ast.Assign | ast.AnnAssign):
            continue
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        if any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in targets):
            return True
        annotation = stmt.annotation if isinstance(stmt, ast.AnnAssign) else None
        if isinstance(annotation, ast.Subscript) and _tail(annotation.value) == "Mapped":
            return True
        if isinstance(stmt.value, ast.Call) and _tail(stmt.value.func) in calls:
            return True
    return False


def _tail(node: ast.expr) -> str:
    return node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else ""


def _is_classvar(annotation: ast.expr, discovery: DatabaseDiscovery, scope: _Scope) -> bool:
    node = annotation.value if isinstance(annotation, ast.Subscript) else annotation
    value = discovery._eval(node, scope, 0)
    return isinstance(value, _Ext) and value.dotted in ("typing.ClassVar", "typing_extensions.ClassVar")


def _annotation_target(annotation: ast.expr, *, mapped: bool) -> tuple[ast.expr | None, bool | None]:
    """(target node, is_collection) from Mapped[list["X"]] / list["X"] / Optional["X"] / "X" | None."""

    node: ast.expr | None = annotation
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            node = ast.parse(node.value, mode="eval").body  # a string annotation: parsed, never evaluated
        except SyntaxError:
            return annotation, None
    if mapped:
        if not (isinstance(node, ast.Subscript)):
            return None, None
        node = node.slice
    collection: bool | None = False
    for _ in range(4):
        if isinstance(node, ast.Subscript):
            head = node.value.attr if isinstance(node.value, ast.Attribute) else node.value.id if isinstance(node.value, ast.Name) else ""
            if head in _COLLECTIONS:
                collection = True
                node = node.slice.elts[0] if isinstance(node.slice, ast.Tuple) else node.slice
                continue
            if head in ("Optional", "Union"):
                elements = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
                real = [e for e in elements if not (isinstance(e, ast.Constant) and e.value is None)]
                node = real[0] if len(real) == 1 else None
                continue
            return None, None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            sides = [s for s in (node.left, node.right) if not (isinstance(s, ast.Constant) and s.value is None)]
            node = sides[0] if len(sides) == 1 else None
            continue
        break
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            parsed = ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return node, collection
        if isinstance(parsed, ast.Subscript | ast.BinOp):
            inner, inner_collection = _annotation_target(parsed, mapped=False)
            return inner, (collection or bool(inner_collection))
    return node, collection


def _registry(model: OrmModel) -> str:
    return model.base_id or "sqlmodel"


def _dotted_name(model: OrmModel) -> str:
    module, _, qual = model.symbol.partition(":")
    return f"{module}.{qual}"


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _replace_rel(draft: ModelRelationship, target: str | None, resolution: TargetResolution) -> ModelRelationship:
    return replace(draft, target_model=target, resolution=resolution)


def discover_database(
    python_paths: Sequence[str],
    read: Callable[[str], bytes | None],
    limits: DiscoveryLimits,
    checkpoint: Callable[[], None] = lambda: None,
) -> DatabaseDiscovery:
    """Run route discovery and prepare database analysis (call :meth:`DatabaseDiscovery.analyse`
    after :meth:`assemble`)."""

    discovery = DatabaseDiscovery(python_paths, read, limits, checkpoint)
    candidates = discovery.candidates()
    for name in candidates:
        discovery._module(name)
    discovery.scan(candidates)
    return discovery


__all__ = ["DatabaseDiscovery", "discover_database"]
