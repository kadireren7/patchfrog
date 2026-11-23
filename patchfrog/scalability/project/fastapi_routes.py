"""Static FastAPI route discovery over stdlib ``ast`` (Milestone 2.3, ADR-004).

Parse only: no module is imported, nothing is executed, and the only input
is source bytes handed in by the caller (already verified against the
revision). The analysis evaluates a small *symbolic* value domain over name
bindings:

* repository modules and names imported from them (absolute imports resolve
  against every ancestor directory of the importing file, so ``src/`` and
  monorepo layouts work; relative imports resolve against the package);
* external names (``fastapi.FastAPI``, ``fastapi.Depends``, ...);
* router objects created by ``FastAPI(...)``/``APIRouter(...)`` and bound to
  a name (module or function scope);
* local functions and classes, string constants (including ``+`` of
  constants), ``typing.Annotated[...]`` aliases;
* everything else is either a known non-router (ignored) or *unknown*.

An unknown value never becomes a route: it becomes an :class:`Unresolved`
entry with the reason. Resolution depth, include depth, dependency depth,
scope nesting, result counts and the number of unresolved entries are
bounded (:class:`DiscoveryLimits`); hitting a bound is recorded as a
``limit_reached`` limitation, never silent truncation.

Semantics follow FastAPI: a route's path is the concatenation of every
``include_router(prefix=...)`` and ``APIRouter(prefix=...)`` from the app
down, then the route path; ``api_route``/``add_api_route`` default to
``GET``. Registration *order* is not modelled (a route registered on a
router after that router was included is still reported as included).
"""

from __future__ import annotations

import ast
import builtins
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from patchfrog.scalability.project.domain import (
    DependencyFunction,
    DependencyResolution,
    DependencySource,
    DependencyUse,
    Endpoint,
    EndpointKind,
    IncludeEdge,
    Limitation,
    LimitationCode,
    Location,
    Route,
    RouteKind,
    Router,
    RouterKind,
    Unresolved,
    UnresolvedKind,
    route_identity,
)

HTTP_METHODS: tuple[str, ...] = ("delete", "get", "head", "options", "patch", "post", "put", "trace")
_ROUTE_DECORATORS = frozenset({*HTTP_METHODS, "api_route", "websocket"})
_ROUTER_CALLS = frozenset({"include_router", "add_api_route", "add_api_websocket_route", "mount"})
_UNSUPPORTED_ROUTER_CALLS = frozenset({"route", "add_route", "add_websocket_route", "websocket_route"})
_APP_CONSTRUCTORS = frozenset({"fastapi.FastAPI", "fastapi.applications.FastAPI"})
_ROUTER_CONSTRUCTORS = frozenset({"fastapi.APIRouter", "fastapi.routing.APIRouter"})
_DEPENDS = frozenset(
    f"fastapi.{module}{name}"
    for module in ("", "params.", "param_functions.")
    for name in ("Depends", "Security")
)
_ANNOTATED = frozenset({"typing.Annotated", "typing_extensions.Annotated"})
_MAX_EXPRESSION_CHARS = 120

#: A file can only register routes or includes if it contains one of these
#: (decorator form, router calls) or imports FastAPI. Other files are parsed
#: only when a resolution needs one of their names.
_CANDIDATE = re.compile(
    rb"(?m)^[ \t]*@.*?\.[ \t]*(?:" + b"|".join(m.encode() for m in sorted(_ROUTE_DECORATORS | _UNSUPPORTED_ROUTER_CALLS)) + rb")\b"
    rb"|\.[ \t\\\r\n]*(?:" + b"|".join(c.encode() for c in sorted(_ROUTER_CALLS | _UNSUPPORTED_ROUTER_CALLS)) + rb")\b"
    rb"|\bfastapi\b"
)


@dataclass(frozen=True)
class DiscoveryLimits:
    """Bounds for one discovery. They shape the result, so they belong in the
    job config (and therefore in the idempotency key)."""

    max_python_files: int = 5_000
    max_file_bytes: int = 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024
    max_resolution_depth: int = 16
    max_include_depth: int = 16
    max_dependency_depth: int = 8
    max_scope_depth: int = 32
    max_routes: int = 10_000
    max_unresolved: int = 1_000
    max_models: int = 10_000

    def __post_init__(self) -> None:
        for name in (
            "max_python_files", "max_file_bytes", "max_total_bytes", "max_resolution_depth",
            "max_include_depth", "max_dependency_depth", "max_scope_depth", "max_routes", "max_unresolved", "max_models",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")


# --- symbolic values ------------------------------------------------------------------


@dataclass(frozen=True)
class _Mod:
    name: str  # a repository module


@dataclass(frozen=True)
class _Ext:
    dotted: str  # an external name


@dataclass(frozen=True)
class _Object:
    """Base of tracked object values (routers here; ORM objects in DatabaseDiscovery)."""


@dataclass(frozen=True)
class _RouterRef(_Object):
    router_id: str


@dataclass(frozen=True)
class _Func:
    symbol: str


@dataclass(frozen=True)
class _Class:
    symbol: str
    router_subclass: bool


@dataclass(frozen=True)
class _Str:
    value: str


@dataclass(frozen=True, eq=False)
class _AnnotatedValue:
    origin: ast.expr
    metadata: tuple[ast.expr, ...]
    scope: _Scope


@dataclass(frozen=True)
class _ExtInstance:
    dotted: str  # the result of calling an external callable


@dataclass(frozen=True)
class _Other:
    reason: str  # a known non-router value


@dataclass(frozen=True)
class _Unknown:
    reason: str


_Value = _Mod | _Ext | _ExtInstance | _Object | _Func | _Class | _Str | _AnnotatedValue | _Other | _Unknown


# --- scopes ------------------------------------------------------------------------------


@dataclass(eq=False)
class _Binding:
    kind: str  # value | import | from | def | class | dynamic
    node: ast.AST | None
    scope: _Scope
    detail: str = ""


@dataclass(eq=False)
class _Scope:
    module: _ModuleInfo
    qualname: str  # "" for the module, "create_app.<locals>." for a function body
    parent: _Scope | None
    bindings: dict[str, list[_Binding]] = field(default_factory=dict)

    def bind(self, name: str, binding: _Binding) -> None:
        self.bindings.setdefault(name, []).append(binding)


@dataclass(eq=False)
class _FuncInfo:
    symbol: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    scope: _Scope  # the scope the def statement is in
    body_scope: _Scope


@dataclass(eq=False)
class _ModuleInfo:
    name: str
    path: str
    is_package: bool
    imports_fastapi: bool = False
    imported_roots: set[str] = field(default_factory=set)
    star_imports: list[tuple[ast.ImportFrom, int]] = field(default_factory=list)
    scope: _Scope | None = None
    registrations: list[tuple[ast.AST, _Scope]] = field(default_factory=list)


def module_name_for(path: str) -> tuple[str, bool] | None:
    pure = PurePosixPath(path)
    if pure.suffix != ".py":
        return None
    parts = list(pure.with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    if not parts:
        return None
    return ".".join(parts), is_package


@dataclass
class _RouterInfo:
    router_id: str
    kind: RouterKind
    module: str
    binding: str
    location: Location
    prefix: str | None
    dependencies: tuple[DependencyUse, ...]


@dataclass
class _RouteDecl:
    router_id: str
    kind: RouteKind
    methods: tuple[str, ...]
    path: str
    endpoint: Endpoint
    registration: Location
    dependencies: tuple[DependencyUse, ...]


class RouteDiscovery:
    """One discovery over a set of Python files: the shared symbolic resolver
    (parsing, scopes, imports, evaluation) plus FastAPI registrations. Use
    :func:`discover_routes`; :class:`~patchfrog.scalability.project.sqlalchemy_models.DatabaseDiscovery`
    extends it through the ``_construct``/``_call_value``/``_attribute_value``/
    ``_enter``/``_returnable`` hooks."""

    _candidate_pattern: re.Pattern[bytes] = _CANDIDATE

    def __init__(
        self,
        python_paths: Sequence[str],
        read: Callable[[str], bytes | None],
        limits: DiscoveryLimits,
        checkpoint: Callable[[], None],
    ) -> None:
        self._read = read
        self._limits = limits
        self._checkpoint = checkpoint
        self._paths: dict[str, str] = {}  # module -> path
        self._package: dict[str, bool] = {}
        for path in sorted(python_paths):
            named = module_name_for(path)
            if named is None or named[0] in self._paths:
                continue
            self._paths[named[0]] = path
            self._package[named[0]] = named[1]
        # Every dotted prefix of a module name: namespace-package lookups in O(1).
        self._prefixes = {name.rsplit(".", i)[0] for name in self._paths for i in range(1, name.count(".") + 1)}
        self._modules: dict[str, _ModuleInfo | None] = {}  # parsed (None = failed)
        self._pending: dict[str, bytes] = {}  # candidate bytes read by candidates(), parsed next
        self._functions: dict[str, _FuncInfo] = {}
        self._classes: dict[str, tuple[ast.ClassDef, _Scope]] = {}
        self._routers: dict[str, _RouterInfo] = {}
        self._routes: list[_RouteDecl] = []
        self._includes: list[IncludeEdge] = []
        self._unresolved: list[Unresolved] = []
        self._limitations: list[Limitation] = []
        self._dependency_functions: dict[str, DependencyFunction | None] = {}
        self._binding_memo: dict[int, _Value] = {}
        self._in_progress: set[int] = set()
        self._omitted_unresolved = 0
        self._assembled_limitations = 0
        self._assembled_unresolved = 0
        self.limit_reached = False
        self.uses_fastapi = False
        self.scanned_files = 0
        self.importing: dict[str, int] = {}

    # --- recording -------------------------------------------------------------------

    def _limit(self, detail: str, location: Location | None = None) -> None:
        self.limit_reached = True
        self._limitations.append(Limitation(LimitationCode.LIMIT_REACHED, location, detail))

    def _unresolved_entry(self, kind: UnresolvedKind, module: _ModuleInfo, node: ast.AST, reason: str) -> None:
        if len(self._unresolved) >= self._limits.max_unresolved:
            self._omitted_unresolved += 1
            return
        try:
            text = ast.unparse(node)
        except (ValueError, RecursionError):  # pragma: no cover - defensive
            text = type(node).__name__
        text = " ".join(text.split())
        if len(text) > _MAX_EXPRESSION_CHARS:
            text = text[: _MAX_EXPRESSION_CHARS - 1] + "…"
        self._unresolved.append(Unresolved(kind, _loc(module, node), text, reason))

    # --- parsing and scope collection --------------------------------------------------

    def _module(self, name: str) -> _ModuleInfo | None:
        if name in self._modules:
            return self._modules[name]
        path = self._paths.get(name)
        if path is None:
            return None
        self._checkpoint()
        self._modules[name] = None  # failed until proven otherwise (also guards re-entry)
        data = self._pending.pop(name, None)
        if data is None:
            data = self._read(path)
        if data is None:
            return None
        try:
            tree = ast.parse(data, filename=path)
        except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
            line = getattr(exc, "lineno", None) or 1
            self._limitations.append(
                Limitation(LimitationCode.PARSE_ERROR, Location(path, int(line)), type(exc).__name__)
            )
            return None
        info = _ModuleInfo(name=name, path=path, is_package=self._package[name])
        info.scope = _Scope(info, "", None)
        self._modules[name] = info
        self._collect(tree.body, info.scope, info.scope, depth=0)
        if info.imports_fastapi:
            self.uses_fastapi = True
        return info

    def _collect(self, body: Iterable[ast.stmt], scope: _Scope, lookup: _Scope, depth: int) -> None:
        """Record bindings in ``scope`` and registration statements. ``lookup``
        is the scope nested function bodies resolve free names in (class
        bodies are skipped, as in Python)."""

        module = scope.module
        for stmt in body:
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    self._bind_target(target, stmt.value, scope)
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                self._bind_target(stmt.target, stmt.value, scope)
            elif isinstance(stmt, ast.AugAssign) and isinstance(stmt.target, ast.Name):
                scope.bind(stmt.target.id, _Binding("dynamic", stmt, scope, "augmented assignment"))
            elif isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    module.imported_roots.add(alias.name.split(".")[0])
                    if alias.name.split(".")[0] == "fastapi":
                        module.imports_fastapi = True
                    if alias.asname:
                        scope.bind(alias.asname, _Binding("import", stmt, scope, alias.name))
                    else:
                        root = alias.name.split(".")[0]
                        scope.bind(root, _Binding("import", stmt, scope, root))
            elif isinstance(stmt, ast.ImportFrom):
                if stmt.level == 0 and stmt.module:
                    module.imported_roots.add(stmt.module.split(".")[0])
                if stmt.level == 0 and stmt.module and stmt.module.split(".")[0] == "fastapi":
                    module.imports_fastapi = True
                for alias in stmt.names:
                    if alias.name == "*":
                        if scope.parent is None:
                            module.star_imports.append((stmt, len(module.star_imports)))
                        continue
                    scope.bind(alias.asname or alias.name, _Binding("from", stmt, scope, alias.name))
            elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                symbol = f"{module.name}:{scope.qualname}{stmt.name}"
                scope.bind(stmt.name, _Binding("def", stmt, scope, symbol))
                module.registrations.append((stmt, scope))
                if depth >= self._limits.max_scope_depth:
                    self._limit("max_scope_depth: nested function not analysed", _loc(module, stmt))
                    continue
                body_scope = _Scope(module, f"{scope.qualname}{stmt.name}.<locals>.", lookup)
                for arg in _all_args(stmt.args):
                    body_scope.bind(arg.arg, _Binding("dynamic", arg, body_scope, "function parameter"))
                self._functions.setdefault(symbol, _FuncInfo(symbol, stmt, scope, body_scope))
                self._collect(stmt.body, body_scope, body_scope, depth + 1)
            elif isinstance(stmt, ast.ClassDef):
                symbol = f"{module.name}:{scope.qualname}{stmt.name}"
                scope.bind(stmt.name, _Binding("class", stmt, scope, symbol))
                self._classes.setdefault(symbol, (stmt, scope))
                if depth >= self._limits.max_scope_depth:
                    self._limit("max_scope_depth: nested class not analysed", _loc(module, stmt))
                    continue
                class_scope = _Scope(module, f"{scope.qualname}{stmt.name}.", lookup)
                self._collect(stmt.body, class_scope, lookup, depth + 1)
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                module.registrations.append((stmt.value, scope))
            elif isinstance(stmt, ast.For | ast.AsyncFor):
                for name in _target_names(stmt.target):
                    scope.bind(name, _Binding("dynamic", stmt, scope, "loop variable"))
                self._collect(stmt.body, scope, lookup, depth)
                self._collect(stmt.orelse, scope, lookup, depth)
            elif isinstance(stmt, ast.While | ast.If):
                self._collect(stmt.body, scope, lookup, depth)
                self._collect(stmt.orelse, scope, lookup, depth)
            elif isinstance(stmt, ast.With | ast.AsyncWith):
                for item in stmt.items:
                    if isinstance(item.optional_vars, ast.Name):
                        scope.bind(item.optional_vars.id, _Binding("with", item.context_expr, scope, "with target"))
                    elif item.optional_vars is not None:
                        for name in _target_names(item.optional_vars):
                            scope.bind(name, _Binding("dynamic", stmt, scope, "with target"))
                self._collect(stmt.body, scope, lookup, depth)
            elif isinstance(stmt, ast.Try | ast.TryStar):
                self._collect(stmt.body, scope, lookup, depth)
                for handler in stmt.handlers:
                    if handler.name:
                        scope.bind(handler.name, _Binding("dynamic", handler, scope, "exception variable"))
                    self._collect(handler.body, scope, lookup, depth)
                self._collect(stmt.orelse, scope, lookup, depth)
                self._collect(stmt.finalbody, scope, lookup, depth)
            elif isinstance(stmt, ast.Match):
                for case in stmt.cases:
                    self._collect(case.body, scope, lookup, depth)

    def _bind_target(self, target: ast.expr, value: ast.expr, scope: _Scope) -> None:
        if isinstance(target, ast.Name):
            scope.bind(target.id, _Binding("value", value, scope, target.id))
        else:
            for name in _target_names(target):
                scope.bind(name, _Binding("dynamic", target, scope, "unpacking assignment"))

    # --- module resolution --------------------------------------------------------------

    def _resolve_absolute(self, target: str, importer: _ModuleInfo, node: ast.AST) -> str | None:
        """The repository module ``target`` names for ``importer``, or None
        (external). Ambiguous matches are recorded and treated as unknown."""

        candidates = []
        if target in self._paths:
            candidates.append(target)
        directories = PurePosixPath(importer.path).parent.parts
        for i in range(1, len(directories) + 1):
            candidate = ".".join((*directories[:i], target))
            if candidate in self._paths and candidate not in candidates:
                candidates.append(candidate)
        if len(candidates) > 1:
            self._unresolved_entry(
                UnresolvedKind.IMPORT, importer, node, f"matches {len(candidates)} repository modules: {', '.join(sorted(candidates))}"
            )
            return "\0ambiguous"
        return candidates[0] if candidates else None

    def _package_of(self, module: _ModuleInfo) -> str:
        if module.is_package:
            return module.name
        return module.name.rpartition(".")[0]

    def _from_base(self, stmt: ast.ImportFrom, module: _ModuleInfo) -> tuple[str | None, bool]:
        """(base module name, is_repository). For relative imports the base
        is always a repository path (it may not exist as a file)."""

        if stmt.level == 0:
            assert stmt.module is not None
            resolved = self._resolve_absolute(stmt.module, module, stmt)
            if resolved == "\0ambiguous":
                return None, True
            if resolved is not None:
                return resolved, True
            namespace = self._namespace_candidates(stmt.module, module)
            if namespace:  # a namespace package: no file for the base, but submodules exist
                return namespace[0], True
            return stmt.module, False
        parts = self._package_of(module).split(".") if self._package_of(module) else []
        up = stmt.level - 1
        if up > len(parts):
            return None, True
        base_parts = parts[: len(parts) - up] if up else parts
        base = ".".join(base_parts)
        if stmt.module:
            base = f"{base}.{stmt.module}" if base else stmt.module
        return base, True

    def _namespace_candidates(self, target: str, importer: _ModuleInfo) -> list[str]:
        prefixes = [target]
        directories = PurePosixPath(importer.path).parent.parts
        prefixes += [".".join((*directories[:i], target)) for i in range(1, len(directories) + 1)]
        found = [p for p in prefixes if p in self._prefixes]
        return found[:1] if len(found) == 1 else []

    def _module_attr(self, module_name: str, attr: str, depth: int) -> _Value:
        # A submodule wins over a same-named attribute of the package. Python
        # prefers the attribute, but in practice the attribute *is* the
        # submodule (``from . import users`` in ``__init__``), and preferring
        # it avoids evaluating that import through itself.
        if f"{module_name}.{attr}" in self._paths:
            return _Mod(f"{module_name}.{attr}")
        info = self._module(module_name)
        if info is None:
            if module_name in self._paths:
                return _Unknown(f"module {module_name} could not be parsed or read")
            return _Unknown(f"no repository module {module_name}")
        assert info.scope is not None
        if attr in info.scope.bindings:
            return self._eval_bindings(info.scope.bindings[attr], depth + 1)
        for stmt, _index in info.star_imports:
            base, in_repo = self._from_base(stmt, info)
            if base is None or not in_repo or (base not in self._paths):
                continue
            value = self._module_attr(base, attr, depth + 1)
            if not isinstance(value, _Unknown):
                return value
        if f"{module_name}.{attr}" in self._paths:
            return _Mod(f"{module_name}.{attr}")
        return _Unknown(f"{attr!r} is not defined in module {module_name}")

    # --- evaluation ------------------------------------------------------------------------

    def _lookup(self, name: str, scope: _Scope, depth: int) -> _Value:
        current: _Scope | None = scope
        while current is not None:
            if name in current.bindings:
                return self._eval_bindings(current.bindings[name], depth + 1)
            if current.parent is None:
                module = current.module
                for stmt, _index in module.star_imports:
                    base, in_repo = self._from_base(stmt, module)
                    if base is None:
                        continue
                    if not in_repo or base not in self._paths:
                        continue
                    value = self._module_attr(base, name, depth + 1)
                    if not isinstance(value, _Unknown):
                        return value
            current = current.parent
        if name in _BUILTINS:
            return _Other("builtin")
        return _Unknown(f"name {name!r} is not defined")

    def _eval_bindings(self, bindings: list[_Binding], depth: int) -> _Value:
        values = [self._eval_binding(b, depth) for b in bindings]
        distinct = set(values) if all(not isinstance(v, _AnnotatedValue) for v in values) else values
        if len(distinct) == 1:
            return values[0]
        return _Unknown(f"name has {len(bindings)} conflicting definitions")

    def _eval_binding(self, binding: _Binding, depth: int) -> _Value:
        key = id(binding)
        if key in self._binding_memo:
            return self._binding_memo[key]
        if key in self._in_progress:
            return _Unknown("cyclic reference")
        if depth > self._limits.max_resolution_depth:
            self._limit("max_resolution_depth", None)
            return _Unknown("resolution depth limit reached")
        self._in_progress.add(key)
        try:
            value = self._compute_binding(binding, depth)
        finally:
            self._in_progress.discard(key)
        self._binding_memo[key] = value
        return value

    def _compute_binding(self, binding: _Binding, depth: int) -> _Value:
        scope, module = binding.scope, binding.scope.module
        if binding.kind == "dynamic":
            return _Unknown(binding.detail)
        if binding.kind == "with":
            assert isinstance(binding.node, ast.expr)
            entered = self._enter(self._eval(binding.node, scope, depth + 1))
            return entered if entered is not None else _Unknown("with target")
        if binding.kind == "def":
            return _Func(binding.detail)
        if binding.kind == "class":
            node = binding.node
            assert isinstance(node, ast.ClassDef)
            bases = [self._eval(b, scope, depth + 1) for b in node.bases]
            subclass = any(isinstance(b, _Ext) and b.dotted in _ROUTER_CONSTRUCTORS | _APP_CONSTRUCTORS for b in bases)
            subclass = subclass or any(isinstance(b, _Class) and b.router_subclass for b in bases)
            return _Class(binding.detail, subclass)
        if binding.kind == "import":
            resolved = self._resolve_absolute(binding.detail, module, binding.node or ast.Pass())
            if resolved == "\0ambiguous":
                return _Unknown(f"import {binding.detail} is ambiguous")
            if resolved is not None:
                return _Mod(resolved)
            if self._namespace_candidates(binding.detail, module):
                return _Mod(self._namespace_candidates(binding.detail, module)[0])
            return _Ext(binding.detail)
        if binding.kind == "from":
            stmt = binding.node
            assert isinstance(stmt, ast.ImportFrom)
            base, in_repo = self._from_base(stmt, module)
            if base is None:
                return _Unknown("import resolves outside the repository or is ambiguous")
            if not in_repo:
                return _Ext(f"{base}.{binding.detail}")
            if f"{base}.{binding.detail}" in self._paths:
                return _Mod(f"{base}.{binding.detail}")
            if base in self._paths:
                return self._module_attr(base, binding.detail, depth)
            if stmt.level > 0:
                return _Unknown(f"relative import target {base} is not in the analysed files")
            return _Ext(f"{base}.{binding.detail}")
        # value binding
        node = binding.node
        assert isinstance(node, ast.expr)
        if isinstance(node, ast.Call):
            func = self._eval(node.func, scope, depth + 1)
            name = binding.detail
            if len(scope.bindings.get(name, ())) > 1:
                # The name is (re)bound more than once: each construction is
                # its own object, and uses of the name are ambiguous.
                name = f"{name}@{node.lineno}"
            constructed = self._construct(func, node, scope, name, depth)
            if constructed is not None:
                return constructed
        return self._eval(node, scope, depth + 1)

    # --- extension hooks (DatabaseDiscovery overrides them and calls super) -------------

    def _construct(self, func: _Value, call: ast.Call, scope: _Scope, name: str, depth: int) -> _Value | None:
        """A named object created by ``name = func(...)``, or None."""

        if isinstance(func, _Ext) and func.dotted in _APP_CONSTRUCTORS | _ROUTER_CONSTRUCTORS:
            kind = RouterKind.APP if func.dotted in _APP_CONSTRUCTORS else RouterKind.ROUTER
            return _RouterRef(self._make_router(kind, call, scope, name, depth))
        return None

    def _call_value(self, func: _Value, call: ast.Call, scope: _Scope, depth: int) -> _Value | None:
        """The result of calling ``func`` inline (not bound by name), or None."""

        return None

    def _attribute_value(self, base: _Value, attr: str) -> _Value | None:
        """``base.attr`` for object values the subclass knows, or None."""

        return None

    def _enter(self, value: _Value) -> _Value | None:
        """What ``with value as x`` binds to ``x``, or None (unknown)."""

        return None

    def _returnable(self, value: _Value) -> bool:
        """Whether a function's single return value is propagated to callers."""

        return isinstance(value, _RouterRef)

    def _eval(self, node: ast.expr, scope: _Scope, depth: int) -> _Value:
        if depth > self._limits.max_resolution_depth:
            self._limit("max_resolution_depth", _loc(scope.module, node))
            return _Unknown("resolution depth limit reached")
        if isinstance(node, ast.Name):
            return self._lookup(node.id, scope, depth)
        if isinstance(node, ast.Attribute):
            base = self._eval(node.value, scope, depth + 1)
            if isinstance(base, _Mod):
                return self._module_attr(base.name, node.attr, depth + 1)
            if isinstance(base, _Ext):
                return _Ext(f"{base.dotted}.{node.attr}")
            if isinstance(base, _RouterRef):
                info = self._routers[base.router_id]
                if node.attr == "router" and info.kind is RouterKind.APP:
                    return base  # app.router is the app's own router
                return _Other(f"attribute {node.attr} of a router")
            if isinstance(base, _Unknown):
                return base
            known = self._attribute_value(base, node.attr)
            if known is not None:
                return known
            if isinstance(base, _ExtInstance):
                return _Unknown(f"attribute of an object returned by {base.dotted}")
            return _Other("attribute of a non-router value")
        if isinstance(node, ast.Constant):
            return _Str(node.value) if isinstance(node.value, str) else _Other("constant")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self._eval(node.left, scope, depth + 1), self._eval(node.right, scope, depth + 1)
            if isinstance(left, _Str) and isinstance(right, _Str):
                return _Str(left.value + right.value)
            return _Unknown("non-constant string expression")
        if isinstance(node, ast.JoinedStr):
            return _Unknown("f-string")
        if isinstance(node, ast.Call):
            func = self._eval(node.func, scope, depth + 1)
            called = self._call_value(func, node, scope, depth)
            if called is not None:
                return called
            if isinstance(func, _Func):
                return self._call_result(func, depth)
            if isinstance(func, _Class):
                if func.router_subclass:
                    return _Unknown("instance of a FastAPI/APIRouter subclass (unsupported)")
                return _Other("instance of a repository class")
            if isinstance(func, _Ext):
                if func.dotted in _APP_CONSTRUCTORS | _ROUTER_CONSTRUCTORS:
                    return _Other("router constructed inline without a name")
                return _ExtInstance(func.dotted)
            if isinstance(func, _Unknown):
                return _Unknown(f"result of unresolved call ({func.reason})")
            return _Unknown("result of a call")
        if isinstance(node, ast.Subscript):
            base = self._eval(node.value, scope, depth + 1)
            if isinstance(base, _Ext) and base.dotted in _ANNOTATED:
                elements = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
                return _AnnotatedValue(elements[0], tuple(elements[1:]), scope)
            return _Other("subscript")
        if isinstance(node, ast.List | ast.Tuple | ast.Set | ast.Dict | ast.Lambda | ast.ListComp | ast.DictComp):
            return _Other(type(node).__name__.lower())
        return _Unknown(f"unsupported expression ({type(node).__name__})")

    def _call_result(self, func: _Func, depth: int) -> _Value:
        info = self._functions.get(func.symbol)
        if info is None:
            return _Unknown("result of a call to an unknown function")
        values = {
            self._eval(ret.value, info.body_scope, depth + 1)
            for ret in _returns(info.node.body)
            if ret.value is not None
        }
        if len(values) == 1:
            (value,) = values
            if self._returnable(value):
                return value
        return _Unknown("call result is not one statically known object")

    def _str(self, node: ast.expr | None, scope: _Scope, default: str | None) -> str | None:
        if node is None:
            return default
        value = self._eval(node, scope, 0)
        return value.value if isinstance(value, _Str) else None

    # --- routers -------------------------------------------------------------------------------

    def _make_router(self, kind: RouterKind, call: ast.Call, scope: _Scope, name: str, depth: int) -> str:
        module = scope.module
        router_id = f"{kind.value}:{module.name}:{scope.qualname}{name}"
        if router_id in self._routers:
            return router_id
        keywords = {k.arg: k.value for k in call.keywords if k.arg}
        prefix: str | None = ""
        if kind is RouterKind.ROUTER:
            prefix_node = keywords.get("prefix")  # APIRouter's parameters are keyword-only
            prefix = self._str(prefix_node, scope, "")
            if prefix is None:
                self._unresolved_entry(UnresolvedKind.PREFIX, module, prefix_node or call, "router prefix is not a static string")
        self._routers[router_id] = _RouterInfo(router_id, kind, module.name, f"{scope.qualname}{name}", _loc(module, call), prefix, ())
        source = DependencySource.APP if kind is RouterKind.APP else DependencySource.ROUTER
        deps = self._dependency_list(keywords.get("dependencies"), scope, source, 0)
        self._routers[router_id].dependencies = deps
        if "routes" in keywords:
            self._unresolved_entry(UnresolvedKind.UNSUPPORTED, module, keywords["routes"], "routes= argument is not analysed")
        return router_id

    # --- registrations --------------------------------------------------------------------------

    def scan(self, candidates: Sequence[str]) -> None:
        for name in candidates:
            module = self._module(name)
            if module is None:
                continue
            for node, scope in module.registrations:
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    for decorator in node.decorator_list:
                        self._decorator(decorator, node, scope)
                elif isinstance(node, ast.Call):
                    self._router_call(node, scope)

    def _receiver(self, func: ast.expr, scope: _Scope) -> tuple[_Value, str] | None:
        if not isinstance(func, ast.Attribute):
            return None
        return self._eval(func.value, scope, 0), func.attr

    def _check_receiver(self, value: _Value, node: ast.AST, scope: _Scope) -> _RouterInfo | None:
        if isinstance(value, _RouterRef):
            return self._routers[value.router_id]
        if self.uses_fastapi and isinstance(value, _Unknown | _ExtInstance):
            self._unresolved_entry(UnresolvedKind.ROUTER_RECEIVER, scope.module, node, _reason(value))
        return None

    def _decorator(self, decorator: ast.expr, func: ast.FunctionDef | ast.AsyncFunctionDef, scope: _Scope) -> None:
        if not isinstance(decorator, ast.Call):
            return
        target = self._receiver(decorator.func, scope)
        if target is None:
            return
        value, attr = target
        if attr in _UNSUPPORTED_ROUTER_CALLS:
            if isinstance(value, _RouterRef):
                self._unresolved_entry(UnresolvedKind.UNSUPPORTED, scope.module, decorator, f"Starlette-style .{attr}() registration is not analysed")
            return
        if attr not in _ROUTE_DECORATORS:
            return
        router = self._check_receiver(value, decorator, scope)
        if router is None:
            return
        endpoint = self._endpoint_for_def(func, scope)
        self._register(router, attr, decorator, scope, endpoint, func)

    def _endpoint_for_def(self, func: ast.FunctionDef | ast.AsyncFunctionDef, scope: _Scope) -> Endpoint:
        kind = EndpointKind.ASYNC if isinstance(func, ast.AsyncFunctionDef) else EndpointKind.SYNC
        return Endpoint(f"{scope.module.name}:{scope.qualname}{func.name}", kind, _loc(scope.module, func))

    def _register(
        self,
        router: _RouterInfo,
        attr: str,
        call: ast.Call,
        scope: _Scope,
        endpoint: Endpoint,
        func: ast.FunctionDef | ast.AsyncFunctionDef | None,
    ) -> None:
        module = scope.module
        keywords = {k.arg: k.value for k in call.keywords if k.arg}
        path_node = keywords.get("path") or (call.args[0] if call.args else None)
        path = self._str(path_node, scope, None)
        if path is None:
            self._unresolved_entry(UnresolvedKind.ROUTE_PATH, module, path_node or call, "route path is not a static string")
            return
        kind = RouteKind.WEBSOCKET if attr in ("websocket", "add_api_websocket_route") else RouteKind.HTTP
        if kind is RouteKind.WEBSOCKET:
            methods: tuple[str, ...] = ("WEBSOCKET",)
        elif attr in HTTP_METHODS:
            methods = (attr.upper(),)
        else:
            methods_node = keywords.get("methods")
            if methods_node is None:
                methods = ("GET",)
            else:
                literal = _literal_strings(methods_node)
                if literal is None:
                    self._unresolved_entry(UnresolvedKind.METHODS, module, methods_node, "methods are not a literal list of strings")
                    return
                methods = tuple(sorted({m.upper() for m in literal}))
        deps = list(self._dependency_list(keywords.get("dependencies"), scope, DependencySource.DECORATOR, 0))
        if func is not None:
            deps += self._parameter_dependencies(func, scope, 0)
        self._routes.append(_RouteDecl(router.router_id, kind, methods, path, endpoint, _loc(module, call), tuple(deps)))

    def _router_call(self, call: ast.Call, scope: _Scope) -> None:
        # Call form of a decorator: router.get("/x")(handler)
        if isinstance(call.func, ast.Call) and isinstance(call.func.func, ast.Attribute) and call.func.func.attr in _ROUTE_DECORATORS:
            target = self._receiver(call.func.func, scope)
            assert target is not None
            router = self._check_receiver(target[0], call.func, scope)
            if router is not None and call.args:
                endpoint, func = self._endpoint_for_expr(call.args[0], scope, call)
                self._register(router, call.func.func.attr, call.func, scope, endpoint, func)
            return
        target = self._receiver(call.func, scope)
        if target is None:
            return
        value, attr = target
        if attr in _UNSUPPORTED_ROUTER_CALLS:
            if isinstance(value, _RouterRef):
                self._unresolved_entry(UnresolvedKind.UNSUPPORTED, scope.module, call, f"Starlette-style .{attr}() registration is not analysed")
            return
        if attr not in _ROUTER_CALLS:
            return
        router = self._check_receiver(value, call, scope)
        if router is None:
            return
        module = scope.module
        keywords = {k.arg: k.value for k in call.keywords if k.arg}
        if attr == "include_router":
            child_node = keywords.get("router") or (call.args[0] if call.args else None)
            if child_node is None:
                return
            child = self._eval(child_node, scope, 0)
            if not isinstance(child, _RouterRef):
                self._unresolved_entry(UnresolvedKind.INCLUDE_TARGET, module, child_node, _reason(child))
                return
            prefix_node = keywords.get("prefix")
            prefix = self._str(prefix_node, scope, "")
            if prefix is None:
                assert prefix_node is not None
                self._unresolved_entry(UnresolvedKind.PREFIX, module, prefix_node, "include prefix is not a static string")
            deps = self._dependency_list(keywords.get("dependencies"), scope, DependencySource.INCLUDE, 0)
            self._includes.append(IncludeEdge(router.router_id, child.router_id, prefix, _loc(module, call), deps))
        elif attr == "mount":
            path = self._str(keywords.get("path") or (call.args[0] if call.args else None), scope, None)
            mounted = keywords.get("app") or (call.args[1] if len(call.args) > 1 else None)
            what = ""
            if mounted is not None:
                value = self._eval(mounted, scope, 0)
                what = f" ({value.router_id})" if isinstance(value, _RouterRef) else ""
            self._limitations.append(
                Limitation(LimitationCode.MOUNT_NOT_TRAVERSED, _loc(module, call), f"mount at {path if path is not None else '?'}{what}")
            )
        else:  # add_api_route / add_api_websocket_route
            endpoint_node = keywords.get("endpoint") or (call.args[1] if len(call.args) > 1 else None)
            endpoint, func = self._endpoint_for_expr(endpoint_node, scope, call)
            self._register(router, attr, call, scope, endpoint, func)

    def _endpoint_for_expr(
        self, node: ast.expr | None, scope: _Scope, call: ast.Call
    ) -> tuple[Endpoint, ast.FunctionDef | ast.AsyncFunctionDef | None]:
        if node is not None:
            value = self._eval(node, scope, 0)
            if isinstance(value, _Func) and value.symbol in self._functions:
                info = self._functions[value.symbol]
                return self._endpoint_for_def(info.node, info.scope), info.node
        self._unresolved_entry(UnresolvedKind.ENDPOINT, scope.module, node or call, "endpoint is not a statically known function")
        return Endpoint(None, EndpointKind.UNKNOWN, None), None

    # --- dependencies ----------------------------------------------------------------------------

    def _is_depends(self, node: ast.expr, scope: _Scope) -> bool:
        if not isinstance(node, ast.Call):
            return False
        value = self._eval(node.func, scope, 0)
        return isinstance(value, _Ext) and value.dotted in _DEPENDS

    def _dependency_list(self, node: ast.expr | None, scope: _Scope, source: DependencySource, depth: int) -> tuple[DependencyUse, ...]:
        if node is None:
            return ()
        if not isinstance(node, ast.List | ast.Tuple):
            self._unresolved_entry(UnresolvedKind.DEPENDENCY, scope.module, node, "dependencies= is not a literal list")
            return ()
        uses = []
        for item in node.elts:
            if self._is_depends(item, scope):
                assert isinstance(item, ast.Call)
                uses.append(self._dependency_use(item, scope, source, None, None, depth))
            else:
                self._unresolved_entry(UnresolvedKind.DEPENDENCY, scope.module, item, "dependencies= item is not a Depends()/Security() call")
        return tuple(uses)

    def _parameter_dependencies(self, func: ast.FunctionDef | ast.AsyncFunctionDef, scope: _Scope, depth: int) -> list[DependencyUse]:
        uses: list[DependencyUse] = []
        args = func.args
        positional = [*args.posonlyargs, *args.args]
        defaults: dict[int, ast.expr] = {}
        for offset, positional_default in enumerate(args.defaults):
            defaults[id(positional[len(positional) - len(args.defaults) + offset])] = positional_default
        for kwonly, kw_default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
            if kw_default is not None:
                defaults[id(kwonly)] = kw_default
        for arg in [*positional, *args.kwonlyargs]:
            default = defaults.get(id(arg))
            annotation_value = self._eval(arg.annotation, scope, 0) if arg.annotation is not None else None
            if default is not None and self._is_depends(default, scope):
                assert isinstance(default, ast.Call)
                uses.append(self._dependency_use(default, scope, DependencySource.PARAMETER, arg.arg, annotation_value, depth))
                continue
            if isinstance(annotation_value, _AnnotatedValue):
                for meta in annotation_value.metadata:
                    if self._is_depends(meta, annotation_value.scope):
                        assert isinstance(meta, ast.Call)
                        origin = self._eval(annotation_value.origin, annotation_value.scope, 0)
                        uses.append(
                            self._dependency_use(
                                meta, annotation_value.scope, DependencySource.PARAMETER, arg.arg, origin, depth,
                                at=_loc(scope.module, arg),
                            )
                        )
        return uses

    def _dependency_use(
        self,
        call: ast.Call,
        scope: _Scope,
        source: DependencySource,
        parameter: str | None,
        annotation: _Value | None,
        depth: int,
        at: Location | None = None,
    ) -> DependencyUse:
        location = at or _loc(scope.module, call)
        keywords = {k.arg: k.value for k in call.keywords if k.arg}
        target_node = keywords.get("dependency") or (call.args[0] if call.args else None)
        value = self._eval(target_node, scope, 0) if target_node is not None else annotation
        if isinstance(value, _Func):
            self._dependency_function(value.symbol, depth + 1)
            return DependencyUse(source, DependencyResolution.LOCAL_FUNCTION, value.symbol, parameter, location)
        if isinstance(value, _Class):
            return DependencyUse(source, DependencyResolution.LOCAL_CLASS, value.symbol, parameter, location)
        if isinstance(value, _Ext):
            return DependencyUse(source, DependencyResolution.EXTERNAL, value.dotted, parameter, location)
        if isinstance(value, _ExtInstance):
            return DependencyUse(source, DependencyResolution.EXTERNAL, f"{value.dotted}()", parameter, location)
        reason = _reason(value) if value is not None else "no dependency target"
        if target_node is None and value is None:
            reason = "Depends() without a target and without a resolvable annotation"
        self._unresolved_entry(UnresolvedKind.DEPENDENCY, scope.module, call, reason)
        return DependencyUse(source, DependencyResolution.UNRESOLVED, None, parameter, location)

    def _dependency_function(self, symbol: str, depth: int) -> None:
        if symbol in self._dependency_functions:
            return
        info = self._functions.get(symbol)
        if info is None:
            return
        if depth > self._limits.max_dependency_depth:
            self._limit("max_dependency_depth", _loc(info.scope.module, info.node))
            return
        self._dependency_functions[symbol] = None  # in progress: guards cycles
        uses = self._parameter_dependencies(info.node, info.scope, depth)
        kind = EndpointKind.ASYNC if isinstance(info.node, ast.AsyncFunctionDef) else EndpointKind.SYNC
        self._dependency_functions[symbol] = DependencyFunction(
            symbol, _loc(info.scope.module, info.node), kind, _is_generator(info.node), tuple(uses)
        )

    # --- assembly ------------------------------------------------------------------------------------

    def assemble(self) -> tuple[tuple[Router, ...], tuple[IncludeEdge, ...], tuple[Route, ...], tuple[DependencyFunction, ...], tuple[Unresolved, ...], tuple[Limitation, ...]]:
        children: dict[str, list[IncludeEdge]] = {}
        for edge in sorted(self._includes, key=lambda e: (e.parent_id, e.location, e.child_id)):
            children.setdefault(edge.parent_id, []).append(edge)
        declared: dict[str, list[_RouteDecl]] = {}
        for decl in self._routes:
            declared.setdefault(decl.router_id, []).append(decl)
        routes: dict[str, Route] = {}
        reached: set[str] = set()
        has_parent = {e.child_id for e in self._includes}

        def visit(owner: str | None, router_id: str, acc: str | None, chain: tuple[str, ...], deps: tuple[DependencyUse, ...]) -> None:
            if len(routes) >= self._limits.max_routes:
                return
            self._checkpoint()
            info = self._routers[router_id]
            reached.add(router_id)
            own = None if acc is None or info.prefix is None else acc + info.prefix
            inherited = deps + info.dependencies
            for decl in declared.get(router_id, []):
                if own is None:
                    continue  # an unresolved prefix above: the path is unknown (recorded already)
                if len(routes) >= self._limits.max_routes:
                    self._limit("max_routes", decl.registration)
                    return
                path = own + decl.path
                route_id = route_identity(owner, chain[0], decl.methods, path, decl.endpoint.symbol)
                routes.setdefault(
                    route_id,
                    Route(route_id, owner, router_id, decl.kind, decl.methods, path, decl.endpoint, decl.registration, chain, inherited + decl.dependencies),
                )
            for edge in children.get(router_id, []):
                if edge.child_id in chain:
                    self._limitations.append(Limitation(LimitationCode.INCLUDE_CYCLE, edge.location, f"{router_id} -> {edge.child_id}"))
                    continue
                if len(chain) >= self._limits.max_include_depth:
                    self._limit("max_include_depth", edge.location)
                    continue
                next_acc = None if own is None or edge.prefix is None else own + edge.prefix
                visit(owner, edge.child_id, next_acc, (*chain, edge.child_id), inherited + edge.dependencies)

        for router_id in sorted(self._routers):
            if self._routers[router_id].kind is RouterKind.APP:
                visit(router_id, router_id, "", (router_id,), ())
        attached = set(reached)
        for router_id in sorted(self._routers):
            if router_id not in attached and router_id not in has_parent:
                visit(None, router_id, "", (router_id,), ())
        for router_id in sorted(set(self._routers) - reached):
            # Only reachable through a cycle of unattached routers.
            self._limitations.append(Limitation(LimitationCode.INCLUDE_CYCLE, self._routers[router_id].location, f"{router_id} is only included from a cycle"))

        if self._omitted_unresolved:
            self._limit(f"max_unresolved: {self._omitted_unresolved} further unresolved references omitted")
        self._assembled_limitations = len(self._limitations)
        self._assembled_unresolved = len(self._unresolved)
        routers = tuple(
            Router(r.router_id, r.kind, r.module, r.binding, r.location, r.prefix, r.dependencies)
            for r in sorted(self._routers.values(), key=lambda r: r.router_id)
        )
        dependency_functions = tuple(
            d for _, d in sorted(self._dependency_functions.items()) if d is not None
        )
        return (
            routers,
            tuple(sorted(self._includes, key=lambda e: (e.parent_id, e.child_id, e.location))),
            tuple(sorted(routes.values(), key=lambda r: (r.app_id or "", r.path, r.methods, r.route_id))),
            dependency_functions,
            tuple(sorted(set(self._unresolved), key=lambda u: (u.location, u.kind.value, u.expression, u.reason))),
            tuple(sorted(set(self._limitations), key=lambda x: (x.code.value, x.location or Location("", 0), x.detail))),
        )

    def unresolved_after_assembly(self) -> list[Unresolved]:
        """Unresolved references recorded after :meth:`assemble` (e.g. an ambiguous
        import met first by the database pass)."""

        return self._unresolved[self._assembled_unresolved :]

    def limitations_after_assembly(self) -> list[Limitation]:
        """Limitations recorded after :meth:`assemble` returned (later analysis passes)."""

        return self._limitations[self._assembled_limitations :]

    @property
    def parsed_files(self) -> int:
        return sum(1 for m in self._modules.values() if m is not None)

    def candidates(self) -> list[str]:
        """Modules whose source can contain a registration or imports FastAPI."""

        found = []
        for name in sorted(self._paths):
            self._checkpoint()
            data = self._read(self._paths[name])
            if data is None:
                continue
            self.scanned_files += 1
            for root in ("fastapi", "pydantic", "sqlalchemy", "sqlmodel", "starlette"):
                if re.search(rb"(?m)^\s*(?:from|import)\s+" + root.encode() + rb"\b", data):
                    self.importing[root] = self.importing.get(root, 0) + 1
            if self._candidate_pattern.search(data):
                found.append(name)
                self._pending[name] = data
        return found


# --- helpers ------------------------------------------------------------------------------------------

_BUILTINS = frozenset(dir(builtins))


def _reason(value: _Value) -> str:
    if isinstance(value, _Unknown | _Other):
        return value.reason
    if isinstance(value, _ExtInstance):
        return f"object returned by external call {value.dotted} (not analysed)"
    if isinstance(value, _Ext):
        return f"external name {value.dotted} (not analysed)"
    return f"not a router ({type(value).__name__.lstrip('_').lower()})"


def _loc(module: _ModuleInfo, node: ast.AST) -> Location:
    return Location(module.path, int(getattr(node, "lineno", 1) or 1))


def _all_args(args: ast.arguments) -> list[ast.arg]:
    found = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg:
        found.append(args.vararg)
    if args.kwarg:
        found.append(args.kwarg)
    return found


def _target_names(target: ast.expr) -> list[str]:
    return [n.id for n in ast.walk(target) if isinstance(n, ast.Name)]


def _literal_strings(node: ast.expr) -> list[str] | None:
    if not isinstance(node, ast.List | ast.Tuple | ast.Set):
        return None
    values = []
    for item in node.elts:
        if not (isinstance(item, ast.Constant) and isinstance(item.value, str)):
            return None
        values.append(item.value)
    return values


def _own_nodes(body: list[ast.stmt]) -> Iterable[ast.AST]:
    """Nodes of a function body, not descending into nested scopes."""

    stack: list[ast.AST] = list(reversed(body))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _returns(body: list[ast.stmt]) -> list[ast.Return]:
    return [n for n in _own_nodes(body) if isinstance(n, ast.Return)]


def _is_generator(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(isinstance(n, ast.Yield | ast.YieldFrom) for n in _own_nodes(func.body))


def discover_routes(
    python_paths: Sequence[str],
    read: Callable[[str], bytes | None],
    limits: DiscoveryLimits,
    checkpoint: Callable[[], None] = lambda: None,
) -> RouteDiscovery:
    discovery = RouteDiscovery(python_paths, read, limits, checkpoint)
    candidates = discovery.candidates()
    # Parse every candidate first, so "is FastAPI used at all" is known before
    # any receiver is judged.
    for name in candidates:
        discovery._module(name)
    discovery.scan(candidates)
    return discovery


__all__ = ["HTTP_METHODS", "DiscoveryLimits", "RouteDiscovery", "discover_routes", "module_name_for"]
