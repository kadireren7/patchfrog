# Architecture Boundaries

Status: **implemented in Milestone 1.2 (2026-11-22)**. This is the source of truth for the rules
enforced by `tests/architecture/` (ADR-012). The goal is to **stop architectural degradation** and
allow incremental improvement. Removing the historical cycles is a non-goal (D1).

| File | Role |
|---|---|
| `tests/architecture/import_graph.py` | stdlib-`ast` import analyzer: module graph, closures, unit graph, Tarjan SCC, baseline (de)serialization |
| `tests/architecture/rules.py` | rule checks R1–R4; returns sorted, actionable messages |
| `tests/architecture/dependency_baseline.json` | the committed historical-debt baseline (§6) |
| `tests/architecture/test_architecture_boundaries.py` | R1–R4 against the real repository |
| `tests/architecture/test_boundary_mutations.py` | mutation proofs (in-memory overrides; no production file is touched) |
| `tests/architecture/test_import_graph_analyzer.py` | correctness of the analyzer on synthetic trees |
| `tests/architecture/public_api.py` + `test_public_api_contract.py` | Public Engine API v0 contract (§3, `PUBLIC_API.md`) |

## 1. How imports are analysed

- **Import kinds.** `module`: executed at import time (top level, class bodies, top-level
  `if`/`try`/`with`; conditional imports count as `module`, conservatively). `lazy`: inside a
  function/method body. `type_checking`: inside `if TYPE_CHECKING:` / `if typing.TYPE_CHECKING:`
  (it wins over `lazy`; never executed, but still a static coupling).
- **Resolution.** Relative imports resolve against the importer's package (`__init__` files are
  their own package; going past the top level is an error). `from X import n` depends on `X.n` if
  that is a module, otherwise on `X`. `import a.b.c` depends on `a.b.c`.
- **Package initialization.** Importing `a.b.c` executes `a/__init__` and `a/b/__init__`, so
  closures add every parent package explicitly (the chain shows `(package init) …`).
- **Not counted.** Plain strings, comments, `from __future__`. **Not visible:** dynamic imports
  (`importlib.import_module`, `__import__`); there are none in production code today (verified).
- **Determinism.** Sorted discovery, sorted edges and sorted outputs. Results don't depend on
  traversal order, import order or formatting (tested). Each source is parsed once per run (`cache`).
- **Which kinds count where.** R1 closures and the R2/R3 unit graph use **all three kinds** (a
  lazy or type-only import is still coupling). R4 distinguishes `lazy` (reviewable exception)
  from `module`/`type_checking` (forbidden).

## 2. Measured baseline (2026-11-22, `main` @ 9b9de85 + Milestones 1.1–1.3)

> **After Milestone 2.1:** 503 modules, 3,130 import edges (3,101 / 28 / 1), 52 units, 304 unit
> edges (+`app → {dependencies, indexing, repository}`; nothing imports `app` yet). The
> historical SCC is **unchanged: 29 units, 156 edges**. The table below is the 1.3 measurement.
>
> **After Milestone 2.2:** 504 modules, 3,143 import edges (3,114 / 28 / 1), 52 units, 304 unit
> edges (unchanged: `patchfrog.app.jobs` imports only `patchfrog.app.{revision,store}` and the
> stdlib; nothing outside `patchfrog.app` imports it). Historical SCC **unchanged: 29 units, 156 edges**.
>
> **After Milestone 2.3:** 511 modules, 3,177 import edges (3,148 / 28 / 1), 53 units (+`scalability`),
> 306 unit edges (+`app → scalability`, +`scalability → dependencies`). `scalability` imports only
> `dependencies.{domain,manifests}` and the stdlib, and is not in any cycle. Historical SCC
> **unchanged: 29 units, 156 edges**.
>
> **After Milestone 2.4:** 513 modules, 3,189 import edges (3,160 / 28 / 1), 53 units, 306 unit edges
> (unchanged: the new `sqlalchemy_models`/`db_domain` modules import only `scalability` modules).
> Historical SCC **unchanged: 29 units, 156 edges**.

| Measure | Value |
|---|---|
| Modules analysed (`patchfrog/`, `apps/`) | 500 (498 at 1.2 + `patchfrog/codegraph/{__init__,local_graph}.py`) |
| Resolved import edges (module / lazy / type_checking) | 3,103 (3,074 / 28 / 1) |
| Dependency units (`patchfrog.<top>`, `apps.<top>`) | 51 (+ `patchfrog.codegraph`) |
| Unit-level edges | **301** (298 at 1.2. Milestone 1.3 added `codegraph → {dependencies, domain, indexing, intelligence, parsing}` and `upstream → codegraph`, and removed `upstream → {domain, intelligence, parsing}`: the compatibility line is now less coupled; measured, not estimated) |
| Non-trivial SCCs | **1, with 29 units** |
| Edges inside that SCC | **156** |
| 2-cycles (for comparison only, not enforced) | 31 |
| Function-local `apps.*` imports from `patchfrog.*` | **3 import sites in 2 modules** |

SCC members: `apps.worker` and the `patchfrog.` units `agent_handoff`, `analysis`,
`change_intelligence`, `config`, `context`, `contract_intelligence`, `cross_pr_intelligence`,
`cross_repo_intelligence`, `executable_verification`, `feedback`, `fix_verification`,
`historical_regression_memory`, `indexing`, `intelligence`, `intent_verification`,
`learning_records`, `merge_readiness`, `ops`, `persistence`, `publishing`, `repository`,
`repository_learnings`, `review`, `review_memory`, `routing`, `services`, `test_intelligence`,
`trajectory_intelligence`.

**Reconciliation with the Phase 1.1 preparation scan** (296 unit edges): the difference is exactly
2 edges, `patchfrog.cli -> patchfrog.cli_campaigns` and `patchfrog.cli -> patchfrog.cli_changes`,
which the earlier rough scan dropped (`from patchfrog import cli_campaigns, cli_changes`). The SCC
membership (29), the intra-SCC edge set (156) and the 2-cycles (31) are identical.

**Why module-level closures (R1) and not package rules:** `intelligence`, `indexing`, `repository`
and `analysis` sit inside the SCC (e.g. `intelligence/queries.py → persistence`), yet the
specific modules the core reuses have clean closures (§4 below).

## 3. Downstream compatibility contracts (ADR-015, ADR-017)

Two contracts with two owners, so no private information enters this public repository:

- **Public, owned here:** the Public Engine API v0 (`docs/architecture/PUBLIC_API.md`,
  `tests/architecture/public_api.py`). `test_public_api_contract.py` imports every listed name in
  **one** fresh interpreter with dummy server settings. Sockets (`connect`, `connect_ex`,
  `create_connection`, `getaddrinfo`) and `subprocess.Popen` are **disabled and recorded**: a
  public module that tries network or process activity at import time fails the test, and is not
  mocked away. It also checks deprecated aliases for object identity, and that every "core" entry
  lies under an R1 root.
- **Private, owned by each private consumer:** a private consumer's exact import surface is
  verified in **that consumer's own CI** against the engine revision it pins (ADR-017). Its
  manifest, its commit references and its tooling are never stored in this repository.
  `.gitignore` excludes `/private/` and the former manifest path as a safeguard.

History: from Milestone 1.2 until 1.3 this section held a private consumer's import manifest. It
was removed from the public tree in 1.3 (never committed), and replaced by the two contracts
above.

## 4. Rules

### R1 — Core closure purity (module level, transitive, all import kinds)
**Core roots** (exactly these, for now; a package root covers all of its modules):
`patchfrog.analysis.subprocess_sandbox`, `patchfrog.app` (2.1), `patchfrog.change_risk`, `patchfrog.codegraph` (1.3), `patchfrog.config.core`,
`patchfrog.dependencies.files`, `patchfrog.dependencies.manifests`, `patchfrog.diff`,
`patchfrog.intelligence.graph`, `patchfrog.intelligence.resolution`, `patchfrog.intelligence.tests`,
`patchfrog.parsing.registry`, `patchfrog.repository.git`, `patchfrog.scalability` (2.3),
`patchfrog.upstream.code_graph`. Future core packages (`patchfrog.perf`) are added to
`CORE_ROOTS` when they are created.

A closure must not reach:
- internal: `apps.*`, `patchfrog.cli*`, `patchfrog.config.settings`, `patchfrog.feedback*`,
  `patchfrog.github*`, `patchfrog.mcp*`, `patchfrog.ops*`, `patchfrog.persistence*`,
  `patchfrog.publishing*`, `patchfrog.review*` (incl. `review_memory`), `patchfrog.services*`;
- external: `alembic`, `anthropic`, `asyncpg`, `celery`, `fastapi`, `google`, `openai`,
  `prometheus_client`, `redis`, `semgrep`, `sqlalchemy`, `uvicorn`.

Allowed third-party libraries: `pydantic`, `pydantic_settings`, `structlog`, `tree_sitter*`, `yaml`,
plus the stdlib (`sys.stdlib_module_names`). Anything else fails as "non-allowlisted (needs an
ADR)". Only the **frontier** is reported, meaning the first step into forbidden territory, with
the full chain (file:line, function, kind).

Measured third-party usage of the current roots: `pydantic`, `pydantic_settings` (`config.core`),
`tree_sitter*` (parsing and everything that parses), `yaml` (`dependencies`, `codegraph`, `upstream.code_graph`),
`structlog` (`subprocess_sandbox`). All of it is within the allowlist, so the ADR needed no amendment.

### R2 — No growth of cycles (normalized unit graph)
Compared as **sets**, not counts: the non-trivial SCCs, and for each the set of intra-SCC edges
`"a -> b"`.
- A new SCC outside the baseline fails ("new dependency cycle among …").
- A unit joining the historical SCC fails ("unit(s) … joined the historical SCC").
- Historical SCCs merging fails.
- A new edge between two members of the historical SCC fails, unless it's added to the baseline
  with a reviewed justification.

Failure messages list every involved edge with its import location, plus an example cycle path.

### R3 — Shrink-only baseline
- A baseline edge that is no longer an intra-SCC edge fails, with the instruction to delete it.
- A member that is no longer in any cycle fails, with the instruction to remove it.
- Justifications must be non-empty and must not start with `TODO`.

Removing debt can never look like growth (tested: removing `config -> review` yields only R3
messages).

### R4 — Adapter direction
- `patchfrog.*` must not import `apps.*` at `module` or `type_checking` level.
- Function-local imports must match a reviewed exception exactly (importer, function, target).
  There are **3 sites in 2 modules** today:
  - `patchfrog.cli:_mcp_serve → apps.worker.celery_app`
  - `patchfrog.cli:_ops_retry_async → apps.worker.tasks.run_review_pipeline`
  - `patchfrog.ops.orchestrator:schedule_pipeline_if_eligible → apps.worker.tasks.run_review_pipeline`
- *Clarification (2026-11-22):* the 1.1 preparation text said "two exceptions". That counted
  modules, and the precise unit is the import site. Nothing was added; the set is identical.
- Obsolete exceptions fail (shrink-only).
- Future `patchfrog.mcp` scalability tools and `cli_scale` may import `patchfrog.app` services
  only. This is enforced once those packages exist.

### R5 — Import-time side effects
Importing `patchfrog.cli` must not import `apps.worker.celery_app` or construct server `Settings`
(`tests/unit/test_cli_local_startup.py`, from 1.1).

## 5. Acceptance criteria (Milestone 1.2) and results

| # | Criterion | Result (2026-11-22) |
|---|---|---|
| 1 | R1–R4 implemented, pure AST, no new dependency, ~10 s | ✅ 28 tests; 7.4 s in pytest, ~10 s wall incl. startup; stdlib only |
| 2 | Baseline equals the measurement | ✅ 1 SCC × 29, 156 edges, 3 apps sites |
| 3 | Mutation proofs | ✅ 14 mutation cases + 1 clean-tree control = 15 tests (§7) |
| 4 | Downstream import contract passes | ✅ at 1.2 (private consumer surface, 0 side effects). Since 1.3 split into the public API v0 contract plus private CI (§3, ADR-017) |
| 5 | No production code changed | ✅ `git diff --stat patchfrog apps` identical to the end of 1.1 |
| 6 | Gates green and full suite verified | see `docs/PROGRESS.md` |

## 6. Updating the baseline safely

1. **Debt removed (R3 failure):** run `python -m tests.architecture.import_graph`. It **prints**
   the baseline implied by the current tree, keeping existing justifications, and never writes
   anything. Delete the reported edges and members from `dependency_baseline.json`, review the
   diff (it may only remove lines), and commit.
2. **New coupling inside the historical SCC (R2):** first prefer removing the import. If it's
   truly required, the printed baseline marks it `TODO: justify…`. Replace that with a reviewed
   justification and an ADR/PR reference. A `TODO` fails the tests, so nothing gets accepted
   silently.
3. **New cycle outside the SCC, or a unit joining it:** never accepted by editing the baseline
   alone. It needs an ADR that amends this document.
4. **New `apps.*` function-local import:** needs architectural review, with a reason recorded in
   `apps_lazy_import_exceptions`.
5. Always update §2 of this document with the new numbers in the same change.

## 7. Mutation proofs (in `test_boundary_mutations.py`)

| ID | Mutation (in memory) | Detected as |
|---|---|---|
| M1 ×3 | `patchfrog.persistence.database` imported into `config/core.py` at module, lazy and `TYPE_CHECKING` level | R1 forbidden module, with `core.py:<line> [kind]` |
| M1b | core → helper → `sqlalchemy` | R1 forbidden library, with a two-hop chain |
| M1c | `numpy` in `diff` | R1 non-allowlisted (needs ADR) |
| M2 | `diff → change_risk` (`change_risk → diff` exists) | R2 new cycle, edges and example cycle |
| M2b | `parsing → persistence` | R2 `parsing` joined the historical SCC |
| M2c | a new edge between two SCC members (chosen deterministically) | R2 new intra-SCC edge |
| M3 | module-level `import apps.worker.celery_app` in `diff` | R4 at module level |
| M3b | new function-local `apps.*` import | R4 needs architectural review |
| M4 | fake extra baseline edge | R3 obsolete edge |
| M4b | `config → review` removed (real debt removal) | R3 only (`config` left the cycle, edge obsolete), never R2 |
| M4c | lazy import removed from `cli._ops_retry_async` | R4 obsolete exception |
| M4d | `TODO`/empty justifications | rejected |

## 8. Deliberately out of scope

Breaking up the 29-unit SCC; moving persistence models out of domain packages; rewriting the
`review/` hub; third-party import-linter tooling (ADR-012 prefers no new dependency); detecting
dynamic imports.
