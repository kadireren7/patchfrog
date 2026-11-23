# Scalability Detection Rules — MVP Specification

Scope: Python ≥ 3.10 application code, FastAPI ≥ 0.100, SQLAlchemy 2.0 (sync `Session` and
`AsyncSession`, ORM and Core `select()`), plus legacy `session.query()` where noted. Postgres is
assumed only for *verification* (VERIFICATION.md). Detection never needs a database.

Every rule only ever produces `verification_status = suspected`. Static analysis **cannot**
establish actual impact, because impact depends on data volume, concurrency and latency.

## Shared prerequisites (built once, Phase 3a)

Items 1 and 2 (framework evidence, FastAPI entrypoints and their `Depends`) were implemented early,
in Milestone 2.3 ([`DISCOVERY.md`](DISCOVERY.md)). Item 5 (the model index, with relationships and
`lazy=`) and the session part of item 4 were implemented in Milestone 2.4 (`DISCOVERY.md` §9):
sessions, factories, engines, session providers and which routes reach them. Still Phase 3a: item
3 (request-path reachability), and the rest of item 4 (DB-access *call sites* inside endpoints
and the functions they reach).

1. **Project model.** Framework and version come from manifests and lockfiles
   (`dependencies/manifests.py`). Unsupported versions produce a coverage warning, not findings.
2. **Entrypoint discovery.** FastAPI routes: `@app|router.<method>(…)`, `api_route`,
   `add_api_route`, `include_router` prefixes (best effort), and each route's
   `Depends(...)` dependency functions. Each handler is marked `async def` or `def`.
3. **Request-path reachability.** A bounded BFS (default depth 4) over `LocalCodeGraph` callees
   from each handler and its dependencies. Unresolved or ambiguous calls end the path and are
   recorded as `trace_truncated`, never assumed.
4. **DB access sites.** Calls on a receiver that is (a) a parameter annotated
   `Session`/`AsyncSession`/`scoped_session`, (b) a parameter whose default is `Depends(<fn>)`,
   where `<fn>` yields or returns a session, (c) a name bound by `with Session(...)`,
   `async with session_factory() as s`, or `sessionmaker()()`, (d) `self.session`/`self.db`
   attributes typed as such. Methods: `execute, scalar, scalars, get, query, merge, refresh,
   stream, stream_scalars, flush` (`add`, `add_all` and `delete` are not queries). If the receiver
   can't be identified, there is no finding.
5. **Model index.** Classes inheriting `DeclarativeBase`, `declarative_base()` results or
   `SQLModel, table=True`, with `relationship(...)` attributes and their `lazy=` setting.

Confidence reasons are always listed explicitly (e.g. `receiver_typed_session`,
`direct_in_handler`, `via_call_path_depth_2`).

---

## PF-SCALE-001 — Blocking I/O in an async request path

| Aspect | Specification |
|---|---|
| Detection | A call to a catalogued blocking API that is (a) lexically inside an `async def` route handler or dependency (**high**), or (b) inside a sync function reachable from one through the call graph, depth ≤ 3 (**medium**). Catalogue v1: `time.sleep`; `requests.*`; `urllib.request.urlopen`; sync `httpx.Client`/`httpx.get|post|…`; `subprocess.run|call|check_output|Popen(...).wait`; sync SQLAlchemy `Session`/`Engine.connect`/`create_engine(...).execute` used inside `async def`; `psycopg2.connect`/cursor ops; `pymysql`; `smtplib.SMTP`; `boto3` client calls; `socket.create_connection`/`recv`; `open(...).read()` only when the path isn't a module constant (**low**). |
| Exclusions | Call is an argument to `asyncio.to_thread`, `loop.run_in_executor`, `anyio.to_thread.run_sync`, `starlette.concurrency.run_in_threadpool`, or `BackgroundTasks.add_task` with a sync function (FastAPI runs those in a thread pool). Handlers declared with `def` are never flagged here (FastAPI runs them in a thread pool). |
| Required evidence | rule match (`ast_match` with file:line), the async entrypoint(s), call path, catalogue entry ID/version, framework version. |
| Supported | FastAPI ≥ 0.100 / Starlette, Python 3.10–3.13 source. |
| Expected false positives | Catalogue name collision (a local `requests` module, caught by import resolution); calls guarded by a startup-only flag; `time.sleep(0)`; code paths that run only in CLI scripts but share a helper with a route (mitigated: only request-reachable paths are reported). |
| Runtime verification needed? | Recommended. Severity on its own is static, impact isn't. |
| Suggested optimization | Use an async client (`httpx.AsyncClient`, `asyncpg`/`AsyncSession`), `await asyncio.sleep`, or move the work to `run_in_threadpool`/`asyncio.to_thread`, or declare the handler as `def` when it is entirely sync. **Not** "add a queue" unless the work is long-running *and* measured. |
| Verification strategy | Loop-lag probe during a request issued through the app's ASGI test client: the max event-loop lag during the request exceeds the threshold before the change, and doesn't after. Also concurrent-request completion time under N parallel loopback requests (repeated). |
| Limitations | Dynamic dispatch, monkeypatching, blocking inside C extensions not in the catalogue, blocking through third-party wrappers outside the repo (only the catalogue sees through them). |

## PF-SCALE-002 — Database query per item in a loop (N+1)

| Aspect | Specification |
|---|---|
| Detection | (a) A DB access site (prerequisite 4) inside `for`/`async for`/`while`/comprehension bodies (**high** if directly inside the loop in a request-reachable function). (b) A function containing a DB access site called inside a loop, depth ≤ 2 (**medium**). (c) Lazy relationship access: the loop iterates the result of a query on model `M`, and the loop body reads `item.<rel>`, where `<rel>` is a `relationship()` on `M` with default/`select` lazy loading and the originating statement has no `selectinload`/`joinedload`/`subqueryload`/`contains_eager` option for it (**medium**, sync `Session` only). (d) `await item.awaitable_attrs.<rel>` in a loop (**high**, async). |
| Exclusions | The loop iterates a literal or constant collection of size ≤ 3; the loop body `break`s/`return`s unconditionally after the first query; `session.add`/`add_all`/`delete` (writes are reported only as an *info* batch hint, never as N+1); queries whose parameters don't depend on the loop variable (that is loop-invariant repeated work: reported under the same rule with sub-ID `loop_invariant`, **medium**). |
| Required evidence | loop location, query site, dependency of the query on the loop variable (data-flow: name use), entrypoint(s), model/relationship definition for (c). |
| Supported | SQLAlchemy 2.0 ORM + Core; legacy `Query` API for (a)/(c). SQLModel sessions (same API). Async lazy loading raises `MissingGreenlet` at runtime in SQLAlchemy 2.0, so (c) isn't reported for `AsyncSession`. |
| Expected false positives | Loops that are bounded in practice (e.g. at most 3 user roles); intentional per-item queries with row locks (`with_for_update`) inside batch jobs; background tasks or scripts reachable from no route (reported only with `--include-non-request`); relationships configured `lazy="selectin"` in a mixin the index missed. |
| Runtime verification needed? | **Yes** for severity. Query count growth is the decisive signal. |
| Suggested optimization | Eager loading (`selectinload` for collections, `joinedload` for many-to-one), a single `WHERE id IN (...)` query plus a dict lookup, or `lazy="raise"` in tests to prevent regressions. Hoist loop-invariant queries. |
| Verification strategy | Deterministic: count SQL statements for the endpoint at N = 1, 10, 100 seeded rows. A fit of statements ≈ a + b·N with b ≥ 1 confirms (`reproduced`). After the fix, b ≈ 0 means `verified_fixed`. Timing is secondary. |
| Limitations | Repository/DAO patterns hidden behind protocols or DI containers; queries built dynamically via `getattr`; ORM events; caching layers that make a per-item query cheap. |

## PF-SCALE-003 — Unbounded collection query on a request path

| Aspect | Specification |
|---|---|
| Detection | In a request-reachable function: a `select(M)`/`session.query(M)` result consumed via `.all()`, `.scalars().all()`, `list(result)`, iteration, or returned directly as the response, where the statement has no `.limit()`/`.slice()`/`[a:b]`/`.first()`/`.one*()`/`.get()`, and no equality filter on a primary or unique key of `M`. **Medium** if the route has no pagination-like parameters (`limit`, `offset`, `page`, `cursor`, `size`, `per_page`, or a `Params` dependency from `fastapi-pagination`). Otherwise **low**: parameters exist but don't reach the statement (data-flow check). |
| Exclusions | Models listed in `scalability.bounded_models` in `.patchfrog.yml` (describing data is allowed for repos, authorizing execution isn't); aggregate queries (`func.count`, `group_by` with a bounded grouping key); `exists()`. |
| Required evidence | statement construction site, consumption site, route path + method, the absence of limit/pagination (as checked facts), model definition. |
| Supported | SQLAlchemy 2.0 `select()`, legacy `Query`. FastAPI routes. |
| Expected false positives | Small reference tables (countries, enums); filters that bound the result per tenant in practice (`where(owner_id == …)` can still be unbounded, and this is reported **low** with the assumption stated); admin-only endpoints. |
| Runtime verification needed? | **Yes.** Whether the result is unbounded is a data-volume assumption, which must be stated in `assumptions[]`. |
| Suggested optimization | Keyset (cursor) pagination for large or append-heavy tables, limit/offset with an enforced max `limit` for small ones, server-side streaming (`stream_scalars`) for exports. Not caching. |
| Verification strategy | Seed N = 100/1,000/10,000 rows. Response row count and payload bytes must grow with N before the fix and be capped after it. Report response-time scaling, with repeats, as secondary evidence. |
| Limitations | Statement built across several functions (tracked only within one function plus direct return), raw SQL strings (`text()`) not parsed in v1. |

---

## Deferred candidates (not MVP), with reasons

| Candidate | Why deferred | Possible later approach |
|---|---|---|
| Connection pool misconfiguration | Correctness depends on deployment (worker count × pool_size vs. DB `max_connections`), which static code doesn't contain. | A narrow, reliable sub-rule: `create_engine`/`create_async_engine` called **per request** (inside a handler or dependency). Config advisory only, with the user supplying the worker count. |
| Unsafe concurrency (shared mutable globals, `AsyncSession` shared across tasks) | High false-positive rate without type and alias analysis. | `AsyncSession` passed into `asyncio.gather` children: narrow and detectable. |
| Excessive repeated work (pure recomputation) | Needs purity and cost knowledge. | Covered partially by the PF-SCALE-002 `loop_invariant` sub-ID. |
| Change-introduced regressions | Not a separate rule: **diff mode** runs all rules on base and head and reports findings new in head (Phase 3c). | Reuses `diff/` + `change_risk/`. |
| Raw SQL (`text()`) analysis | Needs a SQL parser dependency (ADR required). | `sqlglot`, after a demand signal. |

## Rule lifecycle

`rule_id` is permanent. `rule_version` is bumped on any detection-logic change. Corpus gates
(TEST_STRATEGY.md) must pass before a version bump merges. Each rule has a kill switch in user
config, and its findings carry `rule_version` so comparisons across versions are flagged as
non-comparable.
