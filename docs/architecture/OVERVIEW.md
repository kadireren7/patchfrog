# Target Architecture — Overview

Status: **Proposed** (Phase 0). The decisions are in [`DECISIONS.md`](DECISIONS.md), the current
state in [`CURRENT_STATE.md`](CURRENT_STATE.md), and the sequencing in
[`../ROADMAP.md`](../ROADMAP.md).

## 1. One-paragraph summary

PatchFrog stays **one repository and one Python distribution** for now. A new, dependency-light
**scalability engine** is added next to the existing review and compatibility lines. It reuses the
DB-free code-intelligence primitives that already exist. A small **application-service layer**
(use cases, jobs, budgets, run artifacts) is the single entry point that the **CLI and the MCP
server both call**. Local mode needs no database, Redis, GitHub App, network or LLM. Results are
immutable, revision-pinned run artifacts on disk. Server mode (Postgres + Celery) and the private
Cloud later run the **same application services** behind their own adapters.

## 2. Layers and module boundaries

```
                ┌──────────────────────── adapters (no business logic) ───────────────────────┐
                │  patchfrog.cli (scale …)   patchfrog.mcp (scalability tools)   apps/worker*  │
                └──────────────┬───────────────────────┬───────────────────────────┬──────────┘
                               ▼                       ▼                           ▼
                ┌──────────────────── application services: patchfrog.app ─────────────────────┐
                │ AnalyzeScalability · ExplainFinding · PlanVerification · RunVerification ·    │
                │ CompareRuns │ JobRunner (lifecycle, budgets, cancel) │ RunStore (protocol)    │
                └──────┬───────────────────────────┬─────────────────────────────┬─────────────┘
                       ▼                           ▼                             ▼
   ┌──── engine: patchfrog.scalability ────┐ ┌─ verification: patchfrog.perf ─┐ ┌─ stores ─────────────┐
   │ project model (FastAPI routes,        │ │ pytest runner adapter,          │ │ LocalRunStore (files)│
   │ SQLAlchemy models/sessions),          │ │ query-count probe, loop-lag     │ │ PostgresRunStore*    │
   │ rules (3 in MVP), tracer, remediation │ │ probe, k6 adapter (optional),   │ │ (*server mode, later)│
   │ catalogue, finding/evidence domain    │ │ stats (repeat/variance/verdict) │ └──────────────────────┘
   └──────┬────────────────────────────────┘ └──────┬──────────────────────────┘
          ▼                                         ▼
   ┌──── shared pure primitives (existing, KEEP) ─────────────────────────────────────────────┐
   │ parsing · intelligence.resolution/graph · codegraph (moved from upstream, 1.3) ·           │
   │ dependencies.files/manifests · change_risk/diff · repository.git · subprocess_sandbox ·    │
   │ executable_verification sandbox core (after Phase-5 extraction)                            │
   └──────────────────────────────────────────────────────────────────────────────────────────┘

   Frozen product lines (unchanged, still consumed by private Cloud):
   review/*, context, publishing, feedback, review_memory, *_intelligence, governance,
   merge_readiness, migration*, campaigns, watchers, github, services, persistence, apps/api
```

`*` server-mode adapters are future work, not part of the MVP.

### Dependency rules (enforced by an AST import test from milestone 1.2; detailed rules and baseline: [`BOUNDARIES.md`](BOUNDARIES.md))

| Package | May import | Must not import |
|---|---|---|
| `patchfrog.scalability` | stdlib, shared pure primitives, `patchfrog.app.contracts` | persistence, github, review, context, publishing, celery, redis, LLM SDKs, `patchfrog.app` services, `patchfrog.mcp`, `patchfrog.cli` |
| `patchfrog.perf` | stdlib, shared primitives, sandbox core | same as above, plus `patchfrog.scalability` internals other than `domain` |
| `patchfrog.app` | scalability, perf, stores, `config.core` | mcp, cli, apps, persistence (only through a `RunStore` implementation module) |
| `patchfrog.mcp` (new tools), `patchfrog.cli` (scale group) | `patchfrog.app` | engine internals (only the contracts) |

## 3. Core domain (all frozen dataclasses, versioned)

- `RepositoryRevision` (implemented in 2.1, `LOCAL_STORE.md`): `revision_id` (content identity over
  in-scope bytes + scope fingerprint; no absolute path), with `commit_sha`, `dirty` and `changes` as
  provenance. Every run and every piece of evidence is tied to exactly one revision.
- `ProjectModel` (first part implemented in Milestone 2.3 as `ApplicationModel`,
  [`DISCOVERY.md`](DISCOVERY.md); the SQLAlchemy index and session sites come later): detected frameworks and versions (from manifests and lockfiles), FastAPI route
  table (method, path, handler symbol, sync/async, dependencies), SQLAlchemy model/relationship
  index, session/engine construction sites. It records explicit `coverage` and `unsupported`
  lists.
- `Finding`: `finding_id` (a stable fingerprint of rule_id + rule_version + symbol + normalized
  location), `rule_id`, `rule_version`, `severity`, `confidence` (`high|medium|low`, with reasons),
  `verification_status`, `entrypoints` (routes that reach it), `trace` (ordered call path with
  file:line), `evidence[]` (typed: `ast_match`, `call_path`, `manifest_version`, `config_value`,
  `measurement`), `assumptions[]`, `false_positive_notes[]`, `remediation_ids[]`, `limitations[]`.
- `Run`: `run_id`, `kind` (`analysis|verification|comparison`), `idempotency_key`
  (= hash(revision, config fingerprint, engine version, rule set version)), `status`, `budget`,
  `budget_usage`, `partial_reasons[]`, `schema_version`, `created_at`.
- `Measurement`: metric, unit, samples[], summary statistics, environment fingerprint,
  comparability key.

## 4. Execution model (details: [`SCALABILITY.md`](SCALABILITY.md))

An in-process `JobRunner` (implemented in Milestone 2.2, `patchfrog/app/jobs.py`; see
`LOCAL_STORE.md` §3 for what is and isn't built yet) with an explicit lifecycle
`queued → running → (succeeded | partial | failed | cancelled)`, cooperative cancellation
(`asyncio` cancellation plus checkpoints between files and rules), and hard budgets (files, bytes,
wall time, graph depth, subprocess time and output). Analysis is a pure function of
(revision, config, engine version), so a repeat run with the same idempotency key returns the
stored artifact. Celery or Cloud execution later wraps the *same* job function. They never get a
second implementation.

## 5. Verification workflow (details: [`VERIFICATION.md`](VERIFICATION.md))

`BASELINE → ANALYZE → PROPOSE → APPLY (agent, with user approval) → VERIFY → COMPARE → REPORT`.
Deterministic signals (SQL statement counts per request, detected event-loop blocking) come
first, and timing second, always with repeat runs, variance, and an explicit `inconclusive`
verdict. PatchFrog orchestrates existing tools (pytest, SQLAlchemy events, optionally k6). It
never ships its own load generator.

## 6. MCP (details: [`MCP.md`](MCP.md))

Same process model as today (stdio, FastMCP). A new tool group, `scalability.*`, calls
`patchfrog.app`. The existing 7 PR-review tools stay unchanged and keep needing a database. The
new tools don't. Execution-capable tools need user-level authorization and are never authorized
by repository content.

## 7. What is deliberately *not* in the target architecture

- No microservices, Kubernetes, Kafka, distributed DB or service mesh. There is no requirement
  for any of them (ADR-010).
- No new `patchfrog-mcp` repository yet (ADR-007). No Cloud code in this repo.
- No LLM in the scalability engine's baseline path. The MCP client's agent is the reasoning
  layer (ADR-006).
- No rewrite or removal of the frozen product lines (ADR-001).
- No SQLite port of the 38-migration review schema (ADR-003).
