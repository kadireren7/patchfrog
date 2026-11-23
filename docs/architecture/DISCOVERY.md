# Application Discovery (Milestones 2.3–2.4)

Status: **implemented**: FastAPI discovery in 2.3 (2026-11-22), SQLAlchemy/SQLModel structure in
2.4 (2026-11-23, §9).
- **Engine:** `patchfrog/scalability/project/` (`domain.py`, `db_domain.py`, `frameworks.py`,
  `fastapi_routes.py`, `sqlalchemy_models.py`, `discovery.py`).
- **Use case:** `patchfrog/app/discovery.py` (`run_discovery`, one `JobRunner` job for both
  parts).
- **Semantics:** ADR-004 (stdlib `ast`).
- **Corpora:** `tests/fixtures/scalability/discovery/cases/` and
  `tests/fixtures/scalability/database/cases/`.

Neither milestone adds detection rules, a CLI command or an MCP tool.

## 1. What discovery does, and what it never does
Discovery reads the files of one `RepositoryRevision` and reports:
- framework evidence from Python manifests and lockfiles;
- the FastAPI apps and routers;
- the include graph;
- every route whose methods and full path are statically known, with its endpoint function and
  sync/async kind;
- every route's effective `Depends`/`Security` relationships;
- everything it could **not** resolve, with a reason.

It never does any of the following:
- imports, executes or `exec`s analysed code (no `setup.py`, `conftest.py`, startup hooks or
  project commands);
- installs anything or touches the network;
- follows symlinks;
- reads a byte that isn't part of the revision. Every read goes through
  `JobContext.read_file` → `read_revision_file`, which opens the file component by component with
  `O_NOFOLLOW` and requires the size and sha256 to equal the revision entry. A file that changed
  after capture is skipped as `changed_since_capture`, and the run is PARTIAL.

Tested by: a module that writes a marker file and raises `SystemExit` at import stays inert;
symlinked files and directories pointing outside the root are ignored; a symlink swap with
identical bytes is refused; and an empty-environment subprocess loads no FastAPI, Starlette,
SQLAlchemy, Celery, Redis, httpx, LLM or server modules.

## 2. Framework evidence (`frameworks.py`)
- **Read:** `requirements*.txt`/`requirements*.in`, `pyproject.toml` (PEP 621 and Poetry),
  `poetry.lock`, `uv.lock`, `Pipfile.lock`, through `patchfrog.dependencies.manifests`.
- **Not read:** `setup.py` (learning what it declares would mean running it), `setup.cfg`,
  `Pipfile`, `-r`/`-c` includes in requirements files, and non-Python manifests.
- **Tracked packages:** `fastapi`, `starlette`, `pydantic`, `sqlalchemy`, `sqlmodel`.

| `version_status` | Meaning | `resolved_version` |
|---|---|---|
| `resolved` | every lockfile that mentions the package names the same version | that version |
| `conflicting` | lockfiles disagree | `None` (no version is claimed) |
| `declared_unresolved` | declared in a manifest, no lockfile evidence | `None`, **even for `==` pins** (a pin says what was requested, not what is installed) |
| `import_only` | imported by analysed code, never declared or locked | `None` |

Declarations keep their manifest path, line and sanitized spec. URL specs are reduced to
`url:<host>`, so credentials are never kept. `importing_files` counts files that contain an
`import`/`from` statement line for the package (at any indentation).

## 3. Supported FastAPI patterns
| Pattern | Notes |
|---|---|
| `FastAPI(...)`, `APIRouter(...)` bound to a name at module or function scope | also `fastapi.FastAPI()`, `fa.FastAPI()` via `import fastapi as fa`, `from fastapi import APIRouter as Router`, `fastapi.routing.APIRouter`, `fastapi.applications.FastAPI` |
| `@x.get/post/put/patch/delete/head/options/trace(path)` | sync (`def`) and async (`async def`); stacked decorators give one route each |
| `@x.api_route(path, methods=[...])` | literal methods, upper-cased and sorted; no `methods` means `GET` (FastAPI's default) |
| `x.add_api_route(path, endpoint, methods=...)`, `x.add_api_websocket_route`, `@x.websocket` | WebSocket routes have the method `WEBSOCKET` |
| call form `x.get(path)(handler)` | in files the scan filter selects (see §6) |
| `app.router.<anything above>` | `app.router` is the app's own router |
| `x.include_router(r, prefix=..., dependencies=[...])`, nested to any depth ≤ `max_include_depth` | path = include prefixes and router prefixes from the app down, then the route path (FastAPI's concatenation); a router included twice gives two routes |
| `APIRouter(prefix=..., dependencies=[...])`, `FastAPI(dependencies=[...])` | |
| static paths and prefixes | string literals, module constants, `+` of those |
| aliases | `alias = router`, `public = app`, re-exports through `__init__`, `from . import users` then `users.router`, `from .x import router as y`, `from pkg import *` from repository modules |
| app factories | `def create_app(): app = FastAPI(); ...; return app`, with nested decorated endpoints; a module-level `app = create_app()` is the same object (single-return resolution) |
| layouts | flat, package, `src/`, monorepo: an absolute import resolves against the repository root **and every ancestor directory of the importing file**; namespace packages (no `__init__.py`) |
| dependencies | `x = Depends(f)`, `Security(f, ...)`, `Annotated[T, Depends(f)]`, `Annotated` type aliases (`CurrentUser = Annotated[...]`), `Depends()` on a class annotation; decorator, router, include and app `dependencies=[...]`; sub-dependencies of local functions (depth ≤ `max_dependency_depth`, cycle-safe); `yield` dependencies flagged `is_generator`; external dependencies kept as dotted names (calls of external callables as `name()`) |
| routers no app provably includes | reported with `app_id = None` ("unattached"), path relative to the topmost router |

## 4. Unsupported or uncertain patterns (always explicit, never guessed)
Each item becomes an `Unresolved` entry (kind in brackets) or a `Limitation`, and makes
`coverage.complete` false. None of them produces a route.

- **Non-static values:** f-string or computed paths [`route_path`], prefixes [`prefix`], methods
  [`methods`]; `dependencies=` that isn't a literal list [`dependency`].
- **Routers that can't be identified:** a function parameter, loop variable, `with`/`except`
  target, unpacking, an object returned by an external call, or a name with several different
  definitions (conditional or try/except rebinding) [`router_receiver`, `include_target`]. A
  rebound name's constructions get distinct ids (`router@<line>`).
- **Imports:** an absolute import that matches more than one repository module [`import`];
  relative imports to missing files; routers from external packages (not analysed).
- **Subclasses and other constructs:** subclasses of `FastAPI`/`APIRouter` [`router_receiver`],
  `APIRouter(routes=[...])` [`unsupported`], Starlette-style `.route()`/`.add_route()`/
  `.websocket_route()` [`unsupported`].
- **Mounts:** `app.mount(path, sub_app)` is a `mount_not_traversed` limitation. A mounted FastAPI
  sub-app's routes are reported under the sub-app, without the mount path.
- **Files and limits:** syntax errors, null bytes, or nesting that overflows the parser
  (`parse_error`; the rest of the repository is still analysed); include cycles
  (`include_cycle`); limits (`limit_reached`).
- **Not modelled at all (documented, not detected):**
  - registration *order*: a route added to a router after that router was included is still
    reported as included;
  - `if`/`else` reachability: both branches count;
  - `getattr`/`setattr`/`importlib`;
  - decorator wrappers that hide the endpoint;
  - class-based views (`fastapi-utils` `cbv`);
  - `Depends` on a class's `__init__` parameters (the class is recorded, its parameters aren't);
  - Python's attribute-over-submodule precedence (discovery prefers a submodule of the same name);
  - a decorator split across lines with a backslash between the receiver and the method;
  - call-form registration in a file that neither imports FastAPI nor has a decorator or router
    call.

Unresolved references only on routers count. A decorator such as `@mock.patch` or `@registry.get`
whose receiver is a known non-router (an external module, a repository class instance) is ignored
silently. A receiver that can't be identified is recorded only when the repository imports FastAPI
somewhere, so a pure Flask project produces no noise.

## 5. Model and identities (`domain.py`, schema version 2)
`ApplicationModel` holds `revision_id`, `frameworks`, `routers` (apps are routers with
`kind=app`), `includes`, `routes`, `dependency_functions`, `unresolved`, `limitations` and
`coverage`, and, since schema 2 (Milestone 2.4), the `database` section (§9). It serializes
canonically (`to_json_bytes`: sorted keys, sorted collections) and round-trips through
`from_dict`. A newer `schema_version` is refused; a version-1 artifact still loads, with an empty
`database` section (tested).
- **Module names are path-derived** (`src/svc/main.py` → `src.svc.main`), never interpreter
  import names. That makes them unique and stable without guessing `sys.path`.
- `router_id = <app|router>:<module>:<binding>`, for example
  `app:service.app:create_app.<locals>.application`.
- `route_id = sha256(app, methods, path, endpoint symbol)[:32]`. Line numbers are not part of it:
  moving code keeps the id, and identical bytes in two locations give byte-identical artifacts
  (tested).
- `Route.dependencies` is the effective list, outermost first: app, includes, routers, decorator,
  parameters.
- `Coverage`: `python_files`, `scanned_files`, `parsed_files`, `complete`,
  `incomplete_reasons` (`unresolved:<kind>`, `limitation:<code>`) and `limit_reached`.

## 6. Execution, budgets and run states
`run_discovery(store, root, engine_version=..., limits=..., budget=..., token=...)` runs
`discovery_job` under the existing `JobRunner`. No second execution system exists.
- **Idempotency key:** revision, config (`discovery_schema_version` + all `DiscoveryLimits`,
  because limits shape the result), engine version, and `DISCOVERY_RULESET_VERSION`
  (`fastapi.1+sqlalchemy.1` since 2.4, one component per analysis, each bumped with any change of
  its semantics; it was `fastapi-discovery.1` in 2.3).
- **Result:** the `discovery.json` artifact (immutable, content-addressed).
- **Scan filter:** every `.py` file is read once and checked with a regex for a decorator line
  `@….<route method>`, a router call (`include_router`, `add_api_route`, `mount`, …) or the word
  `fastapi`. Only matching files are parsed up front; any other file is parsed only when a
  resolution needs one of its names. A file without these tokens can't register a route, so this
  bounds memory without losing routes.

| Limit (`DiscoveryLimits`) | Default | When exceeded |
|---|---|---|
| `max_python_files` | 5,000 (sorted, first N) | rest not analysed, `limit_reached`, run PARTIAL |
| `max_file_bytes` | 1 MiB | file skipped (`max_file_bytes`), run PARTIAL |
| `max_total_bytes` | 64 MiB of read source | further files skipped (`max_total_bytes`), run PARTIAL |
| `max_resolution_depth` | 16 | value unknown, `limit_reached` |
| `max_include_depth` | 16 | deeper routers not composed, `limit_reached` |
| `max_dependency_depth` | 8 | deeper sub-dependencies not walked, `limit_reached` |
| `max_scope_depth` | 32 nested defs/classes | deeper bodies not analysed, `limit_reached` |
| `max_routes` | 10,000 | further routes dropped, `limit_reached` |
| `max_unresolved` | 1,000 (routes and database each) | further entries counted, not listed, `limit_reached` |
| `max_models` | 10,000 (2.4) | further models not built, `limit_reached` |

**Run states:**
- **SUCCEEDED:** the analysis was complete for its inputs. `coverage.complete` can still be false
  because of unresolved code: that is a property of the code, deterministic, so the run is
  reusable.
- **PARTIAL:** a budget or limit stopped work, or a file was skipped.
- **CANCELLED:** no `discovery.json` (tested: cancellation takes effect at the next file
  checkpoint).
- **Wall time:** `JobBudget.max_seconds` → PARTIAL with no result.

## 7. Evaluation corpus (fixtures, not real-world accuracy)
There are 14 cases with hand-written `expected.json` files, defined from FastAPI semantics
independently of the engine's output. Scored: routes (app, methods, path, endpoint, kind) and
effective dependencies. Checked exactly: dependency functions (case 05), unresolved
(kind, file, line), limitations (code, file), framework evidence, and `complete`. Two cases are
negative controls (Flask; decorator look-alikes inside a FastAPI project), and four exercise
uncertainty (unresolved, ambiguous, unsupported, conflicting versions).

| Result (2026-11-22) | Value |
|---|---|
| Routes | 36 expected, **36 TP / 0 FP / 0 FN → precision 1.00, recall 1.00** |
| Dependencies | 12 expected, **12 / 0 / 0 → 1.00 / 1.00** |
| Unresolved, limitations, frameworks, `complete` | exact match in all 14 cases |
| Evaluator sensitivity | 7 of 7 engine mutations detected (no include prefix, no router prefix, external receiver as router, no ambiguity, wrong default method, no `Annotated`, guessing a dynamic path) |

**A perfect score here is agreement with these 14 fixtures only.** It is not evidence of detection
accuracy on real repositories: that is what checkpoints G1/G2 measure (`ROADMAP.md`). One
real-world spot check: on this repository, `apps/api` (an app factory plus 3 routers, stacked
decorators) gives exactly the 5 routes counted by hand, with nothing unresolved.

Reproduce: `.venv/bin/python -m tests.unit.test_discovery_corpus`.

## 8. Measured results
Host: Linux 7.1, Python 3.12.3, 12 CPUs, local NVMe, ~1.4 GB free RAM. Measured with
`scripts/bench_discovery.py --repo .` under `tracemalloc`, which slows execution. These are
single-host measurements, not claims.

| Input | Files (python / parsed) | Routes | First run | Repeat (reused) | Traced peak |
|---|---|---:|---:|---:|---:|
| Synthetic layered FastAPI fixture | 305 / 201 | 200 | 0.32 s | 0.03 s | 4.2 MiB |
| Synthetic layered fixture, ×10 | 3,005 / 2,001 | 2,000 | 3.25 s | 0.24 s | 33.2 MiB |
| This repository (1,116 Python files) | 1,116 / 64 | 41 | 1.22 s | 0.13 s | 20.0 MiB |

Process peak RSS for the whole benchmark: 104 MiB. The roadmap's "~300-file layered fixture
< 10 s" trend target is met at 0.32 s. Before a fix in this session, the 3,005-file case took
12.9 s: namespace-package lookups scanned every module name for each external import. A
precomputed prefix set made it linear.

## 9. SQLAlchemy and SQLModel structure (Milestone 2.4)

Engine: `sqlalchemy_models.py`. `DatabaseDiscovery` extends the route resolver through explicit
hooks (named constructions, inline calls, object attributes, `with` targets, returnable values).
Both analyses therefore share one parse of each file and one symbolic evaluation, and sessions can
be traced from routes. Model: `db_domain.py` (`DatabaseModel`, `DATABASE_SCHEMA_VERSION = 1`),
stored as `ApplicationModel.database`.

### 9.1 Guarantees
The guarantees of §1 hold for this part too:
- Parse only: no model class is created, no metadata is built, no Alembic `env.py` or migration
  runs, no engine is created and no database is contacted (all tested with marker payloads and an
  empty-environment subprocess that loads no SQLAlchemy, SQLModel, Alembic or driver module).
- **Connection settings are never invented or copied.** For an engine, only the *scheme* of a
  **literal** URL is kept (`postgresql+asyncpg`). Host, user, password and database name never
  are; a test searches the whole store for them. A URL from settings or the environment is
  `url_source = dynamic`. Only allowlisted options with literal values are kept (`pool_size`,
  `max_overflow`, `pool_pre_ping`, `pool_recycle`, `pool_timeout`, `pool_use_lifo`, `poolclass`,
  `echo`, `isolation_level`, `future`; for factories `autoflush`, `autocommit`, `autobegin`,
  `expire_on_commit`, `class_`). `connect_args` is recorded as present (`<set>`) without its
  value. Runtime pool behaviour is never inferred.

### 9.2 Supported patterns
| Area | Pattern | Provenance |
|---|---|---|
| Bases | `class Base(DeclarativeBase)` / `DeclarativeBaseNoMeta` (also via `orm.`/`sa.orm.` aliases), `Base = declarative_base()` | — |
| Models | any repository class whose bases resolve to a declarative base or a mapped class; `__abstract__ = True` classes are not models but contribute attributes; plain mixins contribute attributes; SQLModel classes with `table=True` (non-table SQLModel parents contribute fields) | — |
| Table name | `__tablename__ = "literal"`, `__table__ = Table("literal", ...)` (wins over `__tablename__`, as in SQLAlchemy) | explicit |
| | SQLModel default: the lower-cased class name | inferred |
| | subclass of a mapped model without its own table: single-table inheritance | inherited |
| Columns | `x: Mapped[T] = mapped_column(...)`, `x = Column(...)` (a first string argument is the database column name), SQLModel `x: T = Field(...)` (also `sa_column=Column(...)`) | explicit |
| | `x: Mapped[T]` without a value; SQLModel `x: T` / `x: T = default` (not `ClassVar`, not `_private`) | inferred |
| Column facts | `primary_key=` literal (a non-literal flag gives `primary_key = None` and an unknown model primary key, never a guess), `nullable=` literal, `ForeignKey("t.c")` / SQLModel `foreign_key="t.c"` literals, type text | — |
| Inheritance | joined (own table under a mapped parent), single (no own table), with the parents listed | — |
| Primary key | the literal `primary_key=True` columns, including mixins/abstract bases | explicit |
| | from the mapped parent (single-table) | inherited |
| | none found, or a non-literal flag | unknown |
| Relationships | `relationship(...)`, SQLModel `Relationship(...)`; target by class object, by string (`"X"` within the same registry, or a qualified `"pkg.mod.X"`), or from the annotation (`Mapped[list["X"]]`, `Mapped["X"]`, `Mapped[Optional[X]]`, `"X \| None"`, `list["X"]` for SQLModel); `collection` from the annotation | — |
| Loading | `lazy="..."` literal, or SQLModel `sa_relationship_kwargs={"lazy": ...}` / `sa_relationship=relationship(...)` | explicit |
| | no `lazy` given: SQLAlchemy's documented `"select"` | default |
| | non-literal | unknown |
| Other relationship facts | `back_populates`, `backref`, `secondary` (as written), `uselist` literals | — |
| Engines and factories | `create_engine`, `sqlmodel.create_engine`, `create_async_engine`, `sessionmaker`, `async_sessionmaker`, `sessionmaker(..., class_=AsyncSession)` (an async factory), `scoped_session`; `bind=`/first-argument engine links. Every named construction is reported, referenced or not | — |
| Sessions | `Session(...)`/`AsyncSession(...)` (SQLAlchemy and SQLModel), calling a factory, `factory.begin()`, `with`/`async with` targets; connections from `engine.connect()`/`engine.begin()` (not sessions) | — |
| Session providers | functions that yield (generator) or return (function) a resolved session | explicit (with the factory, engine or session they come from) |
| | functions whose return annotation is `Session`/`AsyncSession` or `Iterator`/`Generator`/`AsyncIterator`/`AsyncGenerator` of one | inferred |
| Route → session | for each route, the providers reached through its effective dependencies (parameters, `Annotated` aliases, decorator/router/app/include dependencies, sub-dependencies up to `max_dependency_depth`), with the endpoint parameter and the dependency chain | — |

Providers are looked for among route dependency functions and in modules that import SQLAlchemy or
SQLModel.

### 9.3 Unsupported or uncertain (reported as `DbUnresolved`, model `complete = false`)
- `@declared_attr` (including `.directive`) and `@hybrid_property`/`hybrid_method` attributes;
  `column_property`, `synonym`, `composite`, `deferred`, `query_expression`, `association_proxy`
  [`dynamic_attribute`].
- A computed `__tablename__`, or a mapped class without `__tablename__`/`__table__` and without a
  mapped parent [`table_name`].
- `type("X", (Base,), {...})` [`dynamic_model`].
- `registry().generate_base()`, `@registry.mapped`, `@as_declarative`, `map_imperatively`, and a
  reflected `Table(..., autoload_with=...)` [`unsupported`].
- Relationship targets that are missing, ambiguous (several models with that name in the
  registry), only in another registry, or given as a lambda/callable [`relationship_target`].
- Bases that can't be classified: a conditionally rebound base, an ambiguous import, an
  external-package base on a class that structurally looks mapped (a `__tablename__` assignment,
  a `Mapped[...]` annotation, a `mapped_column`/`relationship` call), or an external base on a
  mapped model that may contribute attributes [`model_base`].
- **Not modelled at all:** `__mapper_args__` details, `__table_args__`; attributes added with
  `setattr` or after class creation; columns from external mixins; `sessionmaker.configure`;
  sessions held on `self`; session use *inside* endpoints and queries (Phase 3a, DB-access
  sites); providers defined in a module that imports neither SQLAlchemy nor SQLModel and that no
  route depends on.

### 9.4 Evaluation corpus (fixtures only; not real-world accuracy)
There are 13 cases, with `expected.json` written by hand from SQLAlchemy/SQLModel semantics:
- declarative 2.0, `declarative_base()`, SQLModel, mixins and inheritance;
- sync and async sessions, imported aliases;
- duplicate names, dynamic constructs, unresolvable/external references;
- a negative control and unsupported styles;
- a conditional base.

Precision and recall are measured only for the supported patterns; unresolved entries, bases and
`complete` are checked exactly.

| Category (2026-11-23) | TP / FP / FN | Precision | Recall |
|---|---|---:|---:|
| Models | 20 / 0 / 0 | 1.00 | 1.00 |
| Columns | 34 / 0 / 0 | 1.00 | 1.00 |
| Relationships | 13 / 0 / 0 | 1.00 | 1.00 |
| Engines, factories, sessions | 7 / 0 / 0 | 1.00 | 1.00 |
| Options | 8 / 0 / 0 | 1.00 | 1.00 |
| Session providers | 6 / 0 / 0 | 1.00 | 1.00 |
| Route → session links | 7 / 0 / 0 | 1.00 | 1.00 |

The first evaluation found a real recall defect (unreferenced engines and factories were not
reported: objects 5/7, options 7/8), which was fixed before the result above. The evaluator detects
10 of 10 engine mutations: no lazy default, a non-literal primary key treated as true, ignored
mixins, first candidate wins, URL kept, no annotation providers, no SQLModel table inference, no
`with` sessions, no dependency chains, no dynamic-attribute reports. The primary-key mutation was
initially missed, which led to a new corpus case and to fixing a defect where ForeignKey and
`nullable` were not read after a non-literal `primary_key`.

**Real-world spot check (this repository, not a metric):** discovery finds the 45 models of
`patchfrog/persistence`, matching the 45 `__tablename__ =` lines counted by hand. The first run
also produced a false "possible model" for a `StrEnum` whose *values* mention ORM names. The check
is now structural, and a regression test covers it.

Reproduce: `.venv/bin/python -m tests.unit.test_database_corpus`.

### 9.5 Measured results (`scripts/bench_discovery.py --repo .`, 2026-11-23)
Under `tracemalloc`, on the host of §8 with only ~0.75 GB of free memory, so times are noisy.
Single-host measurements, not claims.

| Input | Files (python / parsed) | Routes / models / route→session | First run | Repeat (reused) | Traced peak |
|---|---|---|---:|---:|---:|
| Synthetic ORM app | 605 / 602 | 300 / 300 / 300 | 1.43 s | 0.10 s | 13.4 MiB |
| Synthetic ORM app | 4,005 / 4,002 | 2,000 / 2,000 / 2,000 | 9.74 s | 0.77 s | 79.1 MiB |
| Layered FastAPI fixture (as in §8) | 3,005 / 3,001 | 2,000 / 0 / 0 | 4.75 s | 0.30 s | 39.7 MiB |
| This repository | 1,145 / 418 | 50 / 65 / 7 | 7.03 s (3.3–4.0 s without tracemalloc) | 0.18 s | 118.1 MiB |

Process peak RSS: 293 MiB.
- **The database pass parses more files:** the scan filter also selects files with ORM or session
  tokens, and provider and class resolution read referenced modules. On this repository parsed
  files went from 64 (2.3) to 418, and the run (without tracemalloc) from about 1.2 s to 3.3–4.0
  s. Profiling shows `ast.parse` as the main cost, which is inherent.
- **One optimization, rejected:** a second hot spot (walking each candidate function body twice)
  was rewritten as a single walk with no measurable gain, and no further optimization was made.
- **The file limit works:** a 6,005-file synthetic app exceeds the default `max_python_files`
  (5,000) and correctly gives a PARTIAL run with an explicit `max_python_files` limitation. The
  benchmark therefore uses 4,005 files.

## 10. CLI and JSON Schema (Milestone 2.5)

**CLI.** `python -m patchfrog scale analyze|show|schema` (`patchfrog/cli_scale.py`) is the first
user-facing adapter: argument parsing, a `run_discovery` call and rendering, nothing else. User
documentation, exit codes, the JSON output format (`patchfrog.scale.run` v1) and measurements are
in [`docs/scale-cli.md`](../scale-cli.md).
- `python -m patchfrog` (`patchfrog/__main__.py`) dispatches `scale` without importing the full
  `patchfrog.cli`, which imports everything eagerly (ROADMAP S3). The probe test checks that no
  SQLAlchemy, FastAPI, httpx, Celery, Redis, LLM, persistence, review or server-settings module
  is loaded.
- `python -m patchfrog.cli scale ...` registers the same commands.
- A `patchfrog` console script is ADR-018, *Proposed*.

**Schema.** `patchfrog/scalability/project/schema.py` generates the JSON Schema (draft 2020-12)
**from the same dataclass type hints that `ApplicationModel.from_dict` loads with**, so there is
one definition.
- **Committed snapshot:** `docs/schemas/application-model.v2.schema.json`. A test regenerates it
  and compares byte for byte. To regenerate: `python -m patchfrog scale schema > docs/schemas/application-model.v2.schema.json`.
- **Objects:** every object is closed (`additionalProperties: false`) and lists every field as
  required (optional values are nullable, never omitted).
- **Values:** enums are closed; `schema_version` is 1 or 2. Version 1 must not have a `database`
  section; version 2 must.
- **Runtime validation:** the loader (`from_dict`) is the runtime validator, so no new runtime
  dependency is needed (`jsonschema` is used in tests only). It is strict in the same way:
  unknown fields are rejected since 2.5.
- **Agreement:** tests check that every corpus artifact (both corpora) validates and round-trips,
  and that the schema and the loader reject the same 8 kinds of invalid document: future version,
  missing `database`, unknown enum value, null in a non-nullable field, wrong type, missing
  field, and unknown top-level and nested fields.
