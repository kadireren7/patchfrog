# Architecture Decision Records

Format: Context → Decision → Alternatives → Consequences. Status values: `Proposed` (needs owner
approval), `Accepted`, `Superseded by ADR-x`. Never delete an ADR. Supersede it.
All ADRs were written in Phase 0 (2026-11-22). **Owner review on 2026-11-22 (decisions D1–D5,
recorded in `docs/PROGRESS.md`): ADR-001…012, 014 and 015 are Accepted, with the conditions noted
in each. ADR-013 is Accepted as an intent (Apache-2.0), but its execution is deferred. ADR-016 is
new.**

Owner conditions that apply to every ADR (D1):
- Don't attempt a repository-wide cleanup of the 31 existing package cycles. Enforce the
  boundaries the new core needs, and prevent *new* cycles with automated architecture tests.
- No speculative abstractions, and no distributed infrastructure without a documented requirement.
- Every migration step is incremental, reversible where practical, and protected by regression
  tests.

---

## ADR-001 — Scalability engine is a new product line in the same repo; existing lines are frozen, not removed
**Status: Accepted (D4).** Frozen means no new features. Maintenance, critical bug fixes,
security fixes and compatibility preservation continue. Self-hosted workflows must keep working.
**Context.** ~103k lines exist for GitHub PR review and API compatibility. The private Cloud
consumes them. Neither line has external user validation (`CURRENT_STATE.md` §1). This is the 3rd
product direction in about 3 months.
**Decision.** Build the scalability engine as new packages that reuse the pure primitives. Freeze
the review and compatibility lines (bug and security fixes only). Make no deletions in this phase.
Reassess at the Phase 7 gate with real usage data.
**Alternatives.** (a) Rewrite in a new repo: throws away verified primitives and the CI/security
discipline. (b) Remove the review/compat code now: breaks Cloud and loses reusable patterns, with
no evidence the new direction will win. (c) Build scalability *inside* `review/`: inherits the
LLM-centric hub coupling.
**Consequences.** The repo stays large. Contributors need the product-line table in `CLAUDE.md`.
The base install stays heavy until ADR-011/Phase 6.

## ADR-002 — Local mode stores immutable run artifacts on disk, not in a database
**Status: Accepted.** *Implementation note (Milestone 2.5, 2026-11-23):* the CLI's default store
location is `--store` → `$PATCHFROG_STORE_DIR` → `$XDG_CACHE_HOME/patchfrog/store` →
`~/.cache/patchfrog/store`, read from the real environment only. `CoreSettings` is not used for
it, because it reads `.env` from the working directory, which may be the analysed (untrusted)
repository (ADR-008 spirit: repository content configures nothing).
**Context.** Local mode must work with no services. Today every command needs Postgres.
**Decision.** `LocalRunStore` writes `<.patchfrog-cache>/runs/<run_id>/{run.json,findings.json,
evidence/…}` atomically (temp file + `os.replace`), content-addressed by the idempotency key. Use a
user cache dir by default (`$XDG_CACHE_HOME/patchfrog/<repo-hash>/`) so the analysed repo isn't
written to unless the user opts in. A `RunStore` protocol allows a `PostgresRunStore` later.
**Alternatives.** (a) SQLite with the existing schema: see ADR-003. (b) SQLite with a new small
schema: viable later if history queries need it, but it's premature now. (c) Require Postgres:
contradicts the mission.
**Consequences.** Querying history means listing files (fine at local scale). Concurrent local runs
are safe through atomic rename plus idempotency. Retention needs a `patchfrog cache prune`
command (Phase 2).
**Implementation note (Milestone 2.1, 2026-11-22).** Implemented as `patchfrog.app.store.LocalRunStore`.
The layout was refined to content-addressed `objects/`, plus `runs/<run_id>/` and `index/<key>`,
so that artifact identity (content), execution identity (`run_id`) and request identity
(idempotency key) stay separate and identical artifacts are deduplicated across runs. Liveness
uses `flock` instead of PIDs. The default location and the `prune` CLI come with the CLI
milestone. Details and measurements: `LOCAL_STORE.md`.

## ADR-003 — PostgreSQL stays the server backend; no SQLite port of the existing schema
**Status: Accepted.**
**Context.** The current schema relies on `pg_advisory_xact_lock` for idempotency, which is a
no-op on SQLite. Its 38 migrations contain ~120 ALTER/DROP operations without `render_as_batch`.
Tests use `create_all`, not Alembic (`CURRENT_STATE.md` §4).
**Decision.** Don't make the review schema dual-dialect. Local scalability mode doesn't use it
(ADR-002). Postgres remains required for server mode and the review line.
**Alternatives.** Port to SQLite: needs batch-mode rewrites of 38 migrations, replacing advisory
locks with `BEGIN IMMEDIATE`/file locks, and re-verifying every concurrency test. That is weeks of
risk for no current requirement.
**Consequences.** Two persistence styles exist (files locally, Postgres on server). The `RunStore`
contract tests must run against both once `PostgresRunStore` exists.

## ADR-004 — Python rule semantics use stdlib `ast`; tree-sitter stays for indexing
**Status: Accepted.**
**Context.** `ParsedCall` has no receiver, loop, await or comprehension context. The rules need
those. `dependencies/scan.py` already uses stdlib `ast` successfully.
**Decision.** Add a Python semantic pass (`patchfrog.scalability.python_semantics`) over `ast`:
loop nesting, `await` sites, receiver chains, decorators, annotations, `Depends(...)`, and
`with`/`async with` scopes. Join it to `LocalCodeGraph` by (file, qualified_name).
**Alternatives.** (a) Semgrep rules: good for single-function patterns, but adds a heavy runtime
dependency to the local path and can't easily join our call graph. Kept as a possible
cross-check. (b) Extend the tree-sitter parser: possible, but it duplicates what `ast` gives for
free and changes `PARSER_VERSION` for the review line. (c) LLM: non-deterministic and costly.
**Consequences.** `ast` parses with the *running* interpreter's grammar (3.12). Code using newer
syntax produces a `parse_error` coverage entry, never a crash.
**Implementation note (Milestone 2.3, 2026-11-22):** the first `ast` consumer is application
discovery (`patchfrog/scalability/project/`, spec `DISCOVERY.md`). Its choices are all reversible:
- Bindings are evaluated over a small symbolic value domain; anything else is *unknown* and
  becomes an `Unresolved` entry, never a route.
- Module names are path-derived, and absolute imports resolve against every ancestor directory
  of the importer.
- Only files with registration tokens are parsed up front; other files are parsed when a
  resolution needs them.
- Discovery limits are part of the job config (they shape results).
- Unresolved code keeps a run SUCCEEDED with `coverage.complete = false`; only budgets, limits
  and skipped files make it PARTIAL.
- `LocalCodeGraph` is not joined yet: request-path reachability (Phase 3a) will do that.
**Implementation note (Milestone 2.4, 2026-11-23):** database discovery reuses the same resolver
instead of a second one. `DatabaseDiscovery` subclasses the route resolver through five explicit
hooks (named construction, inline call, object attribute, `with` target, returnable value), so one
parse and one evaluation serve both, and sessions can be traced from routes. Further choices, all
reversible:
- The result is a `database` section of `ApplicationModel` (schema 2, version-1 artifacts still
  load), not a second artifact.
- String relationship targets resolve by class name within the declarative registry, as
  SQLAlchemy does; several matches are reported as ambiguous.
- `lazy` defaults to SQLAlchemy's documented `"select"`, with `provenance = default`.
- Engines keep only a literal URL's scheme.
- Unreferenced named constructions are evaluated so that they are reported.
- "Possible model" detection is structural, not textual.

## ADR-005 — MVP has exactly three rules
**Status: Accepted.**
**Decision.** `PF-SCALE-001` blocking I/O in async request paths, `PF-SCALE-002` per-item database
queries in loops (N+1), `PF-SCALE-003` unbounded collection queries on request paths.
Specs: [`DETECTION_RULES.md`](DETECTION_RULES.md). Pool misconfiguration, unsafe concurrency and
repeated work are deferred, with the reasons recorded there.
**Alternatives.** A broad catalogue of heuristics. Rejected, because false positives destroy
trust faster than missing rules do.

## ADR-006 — No LLM in the scalability engine; the MCP client is the reasoning layer
**Status: Accepted.**
**Context.** The user's agent (Claude Code, Codex, Cursor) is already an LLM. A second LLM call
inside PatchFrog adds cost, non-determinism and a provider dependency.
**Decision.** Explanations and remediations are deterministic, versioned templates filled with
evidence. The agent turns them into code. Existing provider adapters stay in the review line.
**Consequences.** Remediation quality depends on template quality. Phase 4 measures it on the
corpus.

## ADR-007 — MCP stays in this repository; no `patchfrog-mcp` repository yet
**Status: Accepted.**
**Context.** The MCP layer is 521 lines, and a thin adapter by design. A separate repo would need
a stable, versioned core API that doesn't exist yet.
**Decision.** Keep MCP in `patchfrog.mcp`. Ship it as a console entry point (`patchfrog mcp serve`)
and, in Phase 6, as a lightweight install target (`uvx patchfrog …` with optional extras).
**Split criteria (all required):** (1) `patchfrog.app` contracts are semver-stable for ≥ 2 minor
releases; (2) MCP needs a different release cadence or a non-Python distribution (e.g. an npm
shim); (3) at least one external contributor or client integration needs it independently.
**Alternatives.** Split now: double release overhead and premature API freezing.

## ADR-008 — Execution is authorized by the user, never by repository content
**Status: Accepted.**
**Decision.** Running tests or benchmarks needs both (1) user-level enablement (CLI flag
`--allow-exec`, or `~/.config/patchfrog/config.toml` `[execution] enabled = true` scoped to a repo
path) and (2) per-run approval of the exact command plan (the MCP tool returns a plan plus
`plan_hash`, and execution needs that hash echoed back). Targets are limited to loopback by
default. Non-loopback hosts are refused. There is no auto-trigger on analysis.
`.patchfrog.yml` may *describe* commands but can never enable them. This matches the existing
review-line principle that repo config can't choose a provider or publication.
**Consequences.** Slightly more friction, and a clear audit trail (`plan.json` stored with the run).

## ADR-009 — Measurement hierarchy: deterministic counters before timing
**Status: Accepted.**
**Decision.** Verification verdicts rely first on deterministic signals (SQL statements per
request via SQLAlchemy `before_cursor_execute`, loop-blocking events), then on timing with
warm-up, N ≥ 5 repeats, median/IQR/bootstrap CI, and an explicit `inconclusive` verdict when
intervals overlap. k6 is an *optional external* adapter for HTTP load against loopback only.
Details: [`VERIFICATION.md`](VERIFICATION.md).

## ADR-010 — No distributed infrastructure; in-process job runner first
**Status: Accepted.**
**Decision.** Jobs run in-process with explicit lifecycle and budgets. Server mode may later
dispatch the same job function through Celery (which already exists). No Kafka, Kubernetes or
microservices until a measured requirement exists, such as sustained queue depth beyond the
capacity of N workers on one host.
**Implementation note (Milestone 2.2, 2026-11-22):** `patchfrog/app/jobs.py` runs one job
synchronously in the calling thread. Choices, all reversible and documented in `LOCAL_STORE.md` §3:
(a) there is no `queued` state, because nothing queues yet; the bounded MCP queue (`SCALABILITY.md`
§4) adds it in Phase 4. (b) A dead owner's run is `interrupted`, its own state (set by
`LocalRunStore.recover()` through `flock`, no PIDs), not `failed: interrupted`. (c) Budgets are not
part of the idempotency key; result-shaping limits belong in the job config. (d) Identical requests
are serialized by 256 striped `flock` lock files in the store, so the job function runs once across
threads and processes. (e) Cancellation is a thread-safe token honoured at checkpoints; an asyncio
wrapper is left to the MCP adapter (Phase 4).

## ADR-011 — Split configuration: `CoreSettings` vs `ServerSettings`
**Status: Accepted, implemented in Phase 1 milestone 1.1 (2026-11-22).** Implementation choice:
`Settings(CoreSettings)` is a subclass, and `ServerSettings = Settings` is an alias. The server
environment contract (50 aliases, the 4 required ones, defaults and order) was verified identical
to `HEAD` and is pinned by `tests/unit/test_core_settings.py`. The CLI imports `apps.worker` lazily
in exactly the two functions that dispatch Celery work (`_ops_retry_async`, `_mcp_serve` when the
verifier is enabled).
**Context.** `Settings` requires GitHub/DB/Redis fields, so the CLI can't even print `--help`
without them (`CURRENT_STATE.md` §2).
**Decision.** `patchfrog.config.core.CoreSettings` has no required fields. `Settings` (server)
keeps its exact current fields and validation, so Cloud and Docker are unaffected. The CLI
imports the Celery app lazily, only inside the commands that need it.
**Consequences.** A lot of call sites keep using `get_settings()`. Only the new paths use
`CoreSettings`. Regression tests: `env -i patchfrog --help` exits 0, and the server settings tests
stay unchanged.

## ADR-012 — Import boundaries enforced by an AST test, not a new dependency
**Status: Accepted.**
**Decision.** `tests/architecture/` parses imports with stdlib `ast` (the same technique as
the existing `test_mcp_module_boundaries.py`) and fails on forbidden edges from the OVERVIEW §2
table. Existing cycles in the frozen lines are recorded as an allowlist that may only shrink.
**Implemented in Milestone 1.2 (2026-11-22).** Refinements based on evidence: (a) core purity is
checked on **module-level transitive closures**, because the reused packages `intelligence`,
`indexing`, `repository` and `analysis` sit inside the historical SCC while the specific modules
used are clean. (b) The cycle baseline is the normalized set of SCCs plus intra-SCC edges (1 × 29,
156 edges), not a count of 2-cycles. Details: `BOUNDARIES.md`.

## ADR-013 — License (owner decision; **not changed**)
**Status: Accepted as intent (D3): the final license will be Apache-2.0. Execution is deferred.**
Prerequisite audit and migration requirements: [`../legal/APACHE_2_0_MIGRATION.md`](../legal/APACHE_2_0_MIGRATION.md).
`LICENSE` stays ELv2 until the owner explicitly approves the switch.
**Context.** ELv2 is source-available, not open source. The proposed tagline says "open-source".
The repo was previously Apache-2.0. There is a single copyright holder and no outside
contributions, so relicensing is legally simple. Cloud already exists privately.
**Options.**
(a) Keep ELv2. You can't say "open source". Protects against hosted clones.
(b) **Apache-2.0 for the whole repo.** Maximizes adoption in the developer-tool and MCP ecosystem
and lets you call it "open source". The Cloud moat then comes from operations, hosted execution,
integrations and the official App, not from license restrictions. A third license change in
about 3 months needs a clear announcement, and you must keep NOTICE/history.
(c) Split licensing: Apache-2.0 for scalability, MCP and pure primitives, ELv2 for the
review/GitHub-App server. This needs separate distributions or directories with per-file
headers. It is more complex and confuses contributors.
**Recommendation.** (b) if the "open-source" positioning is essential, decided **before** the
Phase 7 public beta. Otherwise (a), with the messaging fixed to "source-available". Until the
owner decides: no license change, and no "open-source" wording in user-facing docs.

## ADR-014 — Documentation sources of truth
**Status: Accepted.**
**Decision.** Plan: `docs/ROADMAP.md`. Status: `docs/PROGRESS.md`. Architecture:
`docs/architecture/*`. Component docs stay in `docs/*.md`. The old roadmap moved to
`docs/history/` (this also fixes a case-insensitive filename collision between `roadmap.md` and
`ROADMAP.md` on macOS/Windows). The old `docs/architecture.md` became
`docs/compatibility-pipeline.md`, so it no longer sits next to the `docs/architecture/` directory
with an ambiguous name.

## ADR-015 — Cloud-consumed interfaces are a compatibility contract
**Status: Accepted (D5), with a mandatory pre-change check.** Before changing package extras,
public interfaces, import paths, dependency declarations or persistence behaviour, run the
**Cloud compatibility check**: (1) is the component on the Cloud-consumed list
(`docs/architecture/PUBLIC_API.md`, `docs/product-boundary.md`, and, for the hosted product, its private import manifest, which is kept outside this repository per ADR-017)?
(2) Is there a regression test pinning its current contract? If not, add one *before* the
change. (3) Record the impact assessment in `PROGRESS.md`. (4) Prefer additive changes and
re-exports. Never change Cloud code from this repo. Existing pinned contracts:
`tests/unit/test_core_settings.py` (server env contract), and the CI Celery task-count check (9 tasks).
**Decision.** Until Phase 8, no refactor may change the signatures or behaviour of the engine
services Cloud calls (`docs/product-boundary.md`: webhook verification, installation sync, PR
ingestion, review pipeline, merge readiness) without a coordinated Cloud change. The Phase 1
boundary work is additive only.

## ADR-016 — Two validation gates: early feedback (G1) and external validation (G2)
**Status: Accepted (D2, 2026-11-22). Supersedes the "G1 hard gate" in the Phase 0 roadmap.**
**Context.** PatchFrog is first developed for the owner's own real projects. A hard external gate
before MCP work would block the tool's own dogfooding loop.
**Decision.** **G1** (after Phase 3) is a *non-blocking* feedback checkpoint on the owner's own
projects: every finding is triaged by hand, precision is recorded, and rule fixes go into the
backlog. **G2** (after the first complete MCP-driven analyze → optimize → verify workflow, i.e.
the end of Phase 5) is the *blocking* external validation gate before packaging for and opening
to outside users (Phases 6–7). Both gates have measurable criteria in `docs/ROADMAP.md`.
**Consequences.** Verification work may start without external evidence. The risk of building the
wrong thing is caught at G2 instead, before the public beta.


## ADR-017 — Public API contract separated from private consumer import contracts
**Status: Accepted (owner decision, Milestone 1.3, 2026-11-22).** Supersedes the Cloud import
manifest that sat in `tests/architecture/` during Milestone 1.2 (it was never committed).

**Context.** Milestone 1.2 pinned compatibility with the private hosted product by storing that
product's exact engine-import manifest (module/name pairs, counts, its commit SHA and its engine
pin) in a public test. It worked technically, but it would have published the internal structure
and dependency surface of a private codebase. The owner ruled that the public repository must not
contain it.

**Decision.**
1. **Public contract, owned by this repository:** the *Public Engine API v0*
   (`docs/architecture/PUBLIC_API.md`, `tests/architecture/public_api.py`, 36 names in three
   tiers: stable, provisional, deprecated), derived only from this repository's own documentation
   and architecture. `test_public_api_contract.py` verifies importability, alias identity, R1
   coverage of core entries, that library modules don't build server `Settings` at import, and
   that there is no network or process activity at import. It also checks that the document and
   the code stay in sync.
2. **Private contract, owned by each private consumer:** the consumer keeps its own manifest,
   scanner and import-contract test in its own repository, and runs them in its own CI against
   the engine revision it pins. This repository holds no private manifest, commit reference or
   consumer-specific tooling. `.gitignore` excludes `/private/` and the former manifest path.
3. Local, untracked handoff material for the hosted product (manifest, scanner, ready-to-copy
   test, instructions) sits in `private/cloud-handoff/` until the owner moves it into that
   repository. It is never committed here.

**Alternatives considered.**
(a) *Keep the manifest public.* Simplest and strongest coupling check, but it discloses private
architecture. Rejected by the owner.
(b) *Encrypt or obfuscate the manifest in the public repo.* Adds key management to a public CI,
and still leaks size and churn. Rejected.
(c) *Optional public test that runs only when a private manifest exists locally.* Hides the data,
but it leaves dead, conditional code in the public suite, and makes "green" depend on an
unshared file. Rejected.
(d) *Only the private CI, no public contract.* Self-hosters and other integrators would get no
promise. Rejected; the public API is useful independently.

**Security and information disclosure.** Public artifacts may name the hosted product as a product
(as `docs/product-boundary.md` already does). They must not contain its import inventory, file
paths, commit SHAs, engine pin, or implementation details. Previously committed text that predates
this ADR (some docstrings and `validation/*` summaries) is left unchanged: history isn't
rewritten, and those files are historical records. Any later cleanup of them is a separate
owner decision.

**Compatibility testing ownership.**
- Engine repository: keeps the public API contract green. Before removing or renaming a stable
  or deprecated entry, it runs the ADR-015 check. It doesn't need private data to stay correct.
- Private consumer: owns detecting breakage of *its* usage. Its CI must be able to fail **before**
  a pin bump is merged.

**How the private CI should validate compatibility** (handed over, not implemented here):
1. A blocking job on every Cloud change and on every engine-pin bump checks three things: the
   manifest's `engine_pin` equals the pin in `pyproject.toml`; the manifest equals a fresh static
   scan of the Cloud sources (so any change in Cloud's engine imports forces a reviewed manifest
   update); every *production* pair resolves against the pinned engine in one fresh interpreter,
   with dummy settings and sockets and process spawning disabled and recorded.
2. An optional non-blocking scheduled job installs the engine `main` branch and runs the same
   check, so upcoming breakage shows up before a pin bump.
3. When the job fails, prefer changing Cloud to stable or provisional public names. Asking the
   engine to promote an internal name to the public API goes through an engine ADR.

**Migration and maintenance responsibilities.** The owner moves `private/cloud-handoff/*` into the
Cloud repository (`tests/engine_contract/`, `scripts/`) and wires the CI job (Cloud milestone, not
an engine one). The engine maintainers keep `PUBLIC_API.md` and `public_api.py` in sync (a test
enforces it), record API changes in the PUBLIC_API changelog, and treat stable removals as
ADR-015 events. Last verification (2026-11-22, Milestone 1.3 final tree): the hosted product's full
recorded engine-import surface resolves with zero failures and zero import-time side effects. The
counts and references are kept in the private handoff only.

## ADR-018 — A `patchfrog` console script for the CLI
**Status: Proposed (2026-11-23, Milestone 2.5). Not implemented.**
**Context.** The CLI is invoked as `python -m patchfrog.cli ...` and, since Milestone 2.5, also as
`python -m patchfrog scale ...`. That second form uses a fast path that skips the eagerly
importing full CLI: 0.15 s instead of 3.5 s of startup, measured. The docs and roadmap speak of
`patchfrog scale ...` and `uvx patchfrog mcp serve`, but `pyproject.toml` declares no
`[project.scripts]`.
**Options.** (a) Add `patchfrog = "patchfrog.__main__:main"` now: one line, and keeps the fast
path. But it changes package metadata, which requires the ADR-015 downstream check (a downstream
consumer could define its own `patchfrog` executable). (b) Add it with the Phase 6 packaging work
(M11 extras split, `uvx`), together with the ADR-015 check. (c) Never add it.
**Recommendation.** (b): packaging changes are batched in Phase 6, where the ADR-015 check
happens anyway. Until then, the documented commands use `python -m patchfrog`.
