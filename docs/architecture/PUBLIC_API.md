# Public Engine API (v0)

Status: **Accepted, Milestone 1.3 (2026-11-22)**. Decision: ADR-017. Contract version:
**0.6.0** (`PUBLIC_API_VERSION` in `tests/architecture/public_api.py`). This document and
`tests/architecture/public_api.py` must always list the same entries. A test enforces that.

## 1. What this is and who it is for

These are the engine interfaces this public repository promises to keep importable for anyone
who builds on it: self-hosters, integrators, and separately maintained products, including the
hosted PatchFrog Cloud. The list comes from this repository's **own** documentation and
architecture:

- runtime entry points (`Makefile`, `docker/Dockerfile`, `docs/deployment.md`),
- the engine services named in `docs/product-boundary.md`,
- the agent-handoff MCP server (`docs/agent-handoff.md`),
- the approved core roots (`BOUNDARIES.md`, R1).

It is **not** derived from any private consumer's import list. A private consumer verifies its
own exact usage in its own CI (§4).

Anything that isn't listed is internal. It may change at any time, subject only to the normal
test suite and the boundary rules.

## 2. Tiers and compatibility expectations

| Tier | Count | Promise for the 0.x line |
|---|---:|---|
| **stable** | 18 | Stays importable with a compatible meaning. Removing, renaming or making an incompatible signature change needs the ADR-015 compatibility check **and** a deprecation window: the old name keeps working as a `deprecated` alias for at least one minor release. |
| **provisional** | 41 | Importable and side-effect-free today (tested), but **expected to evolve** in Phases 1–2: `patchfrog.app` will wrap the core primitives, M9 will move the sandbox, and M8 will change MCP tool registration. May change in a minor release with a changelog entry (§7). No deprecation window is guaranteed. |
| **deprecated** | 3 | Still importable, as the *same object* as its canonical replacement (tested). Kept for at least one minor release after deprecation, then removable after the ADR-015 check. |

Nothing here is "permanently stable". The project is pre-1.0 (`0.1.0-beta`). A 1.0 API freeze
would be a separate, explicit decision (an ADR), taken when the public beta (Phase 7) has real
downstream users.

Why the current split: the **stable** entries are the server-mode entry points and services that
downstream integrations already rely on, and that belong to the frozen product lines (D4:
compatibility preservation). The **provisional** entries are either new (`CoreSettings`,
`patchfrog.codegraph`, the Milestone 2.1–2.3 `patchfrog.app` revision, store, job-runner and discovery APIs, and `patchfrog.scalability.project`) or scheduled to be reshaped by the roadmap (MIGRATION_PLAN M5, M8, M9).

## 3. The contract (62 names)

| Module | Name | Area | Tier |
|---|---|---|---|
| `apps.api.main` | `app` | runtime | stable |
| `apps.verifier.celery_app` | `verifier_celery_app` | runtime | stable |
| `apps.worker.celery_app` | `celery_app` | runtime | stable |
| `patchfrog.cli_scale` | `main` | runtime | provisional |
| `patchfrog.cli` | `main` | runtime | stable |
| `patchfrog.config.core` | `CoreSettings` | configuration | provisional |
| `patchfrog.config.core` | `get_core_settings` | configuration | provisional |
| `patchfrog.config.logging` | `configure_logging` | configuration | stable |
| `patchfrog.config.settings` | `ServerSettings` | configuration | stable |
| `patchfrog.config.settings` | `Settings` | configuration | stable |
| `patchfrog.config.settings` | `get_settings` | configuration | stable |
| `patchfrog.persistence.database` | `create_engine` | persistence | stable |
| `patchfrog.persistence.database` | `create_session_factory` | persistence | stable |
| `patchfrog.persistence.models` | `Base` | persistence | stable |
| `patchfrog.github.signatures` | `verify_signature` | services | stable |
| `patchfrog.merge_readiness.domain` | `MergeReadinessResult` | services | stable |
| `patchfrog.merge_readiness.service` | `MergeReadinessService` | services | stable |
| `patchfrog.ops.orchestrator` | `schedule_pipeline_if_eligible` | services | stable |
| `patchfrog.services.installation_sync` | `InstallationSyncService` | services | stable |
| `patchfrog.services.pull_request_ingestion` | `IngestionOutcome` | services | stable |
| `patchfrog.services.pull_request_ingestion` | `PullRequestIngestionService` | services | stable |
| `patchfrog.mcp.server` | `PatchFrogMCPServer` | agent | provisional |
| `patchfrog.analysis.subprocess_sandbox` | `run_sandboxed` | core | provisional |
| `patchfrog.app.discovery` | `DiscoveryResult` | core | provisional |
| `patchfrog.app.discovery` | `run_discovery` | core | provisional |
| `patchfrog.app.jobs` | `CancellationToken` | core | provisional |
| `patchfrog.app.jobs` | `FailureClass` | core | provisional |
| `patchfrog.app.jobs` | `JobBudget` | core | provisional |
| `patchfrog.app.jobs` | `JobContext` | core | provisional |
| `patchfrog.app.jobs` | `JobError` | core | provisional |
| `patchfrog.app.jobs` | `JobReport` | core | provisional |
| `patchfrog.app.jobs` | `JobResult` | core | provisional |
| `patchfrog.app.jobs` | `JobRunner` | core | provisional |
| `patchfrog.app.jobs` | `JobSpec` | core | provisional |
| `patchfrog.app.jobs` | `derive_idempotency_key` | core | provisional |
| `patchfrog.app.revision` | `RepositoryRevision` | core | provisional |
| `patchfrog.app.revision` | `RevisionCaptureError` | core | provisional |
| `patchfrog.app.revision` | `RevisionScope` | core | provisional |
| `patchfrog.app.revision` | `capture_revision` | core | provisional |
| `patchfrog.app.store` | `ArtifactRef` | core | provisional |
| `patchfrog.app.store` | `LocalRunStore` | core | provisional |
| `patchfrog.app.store` | `RunRecord` | core | provisional |
| `patchfrog.app.store` | `RunState` | core | provisional |
| `patchfrog.app.store` | `StoreError` | core | provisional |
| `patchfrog.change_risk` | `classify_change` | core | provisional |
| `patchfrog.codegraph` | `EMPTY_GRAPH` | core | provisional |
| `patchfrog.codegraph` | `LocalCodeGraph` | core | provisional |
| `patchfrog.codegraph` | `build_local_code_graph` | core | provisional |
| `patchfrog.dependencies.files` | `iter_repository_files` | core | provisional |
| `patchfrog.dependencies.manifests` | `parse_lockfile` | core | provisional |
| `patchfrog.dependencies.manifests` | `parse_manifest` | core | provisional |
| `patchfrog.intelligence.graph` | `build_graph` | core | provisional |
| `patchfrog.intelligence.resolution` | `RepositoryResolver` | core | provisional |
| `patchfrog.parsing.registry` | `default_registry` | core | provisional |
| `patchfrog.repository.git` | `run_git` | core | provisional |
| `patchfrog.scalability.project` | `ApplicationModel` | core | provisional |
| `patchfrog.scalability.project` | `DiscoveryLimits` | core | provisional |
| `patchfrog.scalability.project` | `DatabaseModel` | core | provisional |
| `patchfrog.scalability.project.schema` | `application_model_json_schema` | core | provisional |
| `patchfrog.upstream.code_graph` | `EMPTY_GRAPH` | core | deprecated |
| `patchfrog.upstream.code_graph` | `LocalCodeGraph` | core | deprecated |
| `patchfrog.upstream.code_graph` | `build_local_code_graph` | core | deprecated |

Deprecated aliases and their canonical targets (object identity is tested):
`patchfrog.upstream.code_graph.{EMPTY_GRAPH, LocalCodeGraph, build_local_code_graph}` →
`patchfrog.codegraph.{…}` (moved in Milestone 1.3, MIGRATION_PLAN M4). Two further names,
`MAX_GRAPH_FILE_BYTES` and `SymbolRef`, are re-exported from both paths for compatibility, but
they are not part of the contract.

## 4. How the contract is verified

`tests/architecture/test_public_api_contract.py`:

1. **Well-formed:** no duplicates, valid tiers, and every deprecated entry maps to exactly one
   canonical target that is itself public.
2. **Core entries are guarded:** every `core` entry lives under an R1 core root
   (`BOUNDARIES.md` §4), so its import closure is kept free of persistence, server
   configuration, Celery, Redis, GitHub, web and LLM-provider code.
3. **Import-side-effect constraint:** every listed name is imported in **one fresh
   interpreter** with dummy server settings and no services. Sockets (`connect`,
   `connect_ex`, `create_connection`, `getaddrinfo`) and `subprocess.Popen` are
   **disabled and recorded**. Any attempt at import time fails the test; it is never mocked away.
   Importing a public module must also never construct server `Settings` unless the module is
   itself a server entry point (`apps.*`, which do so by design and need the server
   environment).
4. **Doc sync:** this document's §3 table matches `public_api.py` exactly.

**Private consumers** (e.g. the hosted product) keep their own manifest of the engine names they
import, and run an equivalent import-and-side-effect check **in their own CI** against the engine
revision they pin. This repository never stores that manifest, its commit references or its
tooling (ADR-017).

## 5. Versioning strategy

- `PUBLIC_API_VERSION` follows semantic rules within 0.x. Adding an entry or promoting
  provisional → stable is a **minor** bump. Moving a stable entry to deprecated is a **minor** bump
  plus a changelog entry. Removing a deprecated entry after its window is a **minor** bump.
  Fixing documentation only is a **patch** bump.
- Package version (`pyproject.toml`) and API version are separate. A release note states the API
  version it ships.
- Wire contracts (MCP tool I/O, run artifacts, findings) are versioned separately through
  `schema_version` (CLAUDE.md rule 4, `MCP.md`). This document covers Python import surfaces
  only.

## 6. Relationship to the CLI and MCP adapters

- `patchfrog.cli.main` (stable) and `patchfrog.mcp.server.PatchFrogMCPServer` (provisional) are
  **adapters**. Their command-line and MCP-tool behaviour is user-facing, and it is versioned by the
  CLI help/docs and the MCP tool `schema_version`, not by this list.
- From Phase 2 on, both adapters will call the shared application services in `patchfrog.app`
  (OVERVIEW §2). Those services will enter this list as **provisional** when created, and they
  become **stable** only after a minor release with real use (an ADR records the promotion).
- Adapters must not grow business logic that downstream code would then need to import. Shared
  behaviour belongs in `patchfrog.app` or the core.

## 7. Deprecation rules and changelog

1. Deprecate first: keep the old name as an alias for the *same object* (or a thin wrapper with
   identical behaviour), mark it `deprecated` here and in `public_api.py`, and mention the
   replacement in its docstring.
2. Keep it for at least one minor release. Before removal, run the ADR-015 compatibility check:
   known downstream consumers must have migrated.
3. Changes to provisional entries need a changelog line below. No alias is required, but one is
   recommended when it's cheap.

| API version | Date | Change |
|---|---|---|
| 0.1.0 | 2026-11-22 | Initial contract (36 names: 18 stable, 15 provisional, 3 deprecated). `patchfrog.upstream.code_graph` names deprecated in favour of `patchfrog.codegraph`. |
| 0.2.0 | 2026-11-22 | Milestone 2.1: +9 **provisional** names in `patchfrog.app.revision` and `patchfrog.app.store` (`capture_revision`, `RepositoryRevision`, `RevisionScope`, `RevisionCaptureError`, `LocalRunStore`, `ArtifactRef`, `RunRecord`, `RunState`, `StoreError`). Additive, so a minor bump. |
| 0.3.0 | 2026-11-22 | Milestone 2.2: +10 **provisional** names in `patchfrog.app.jobs` (`JobRunner`, `JobSpec`, `JobBudget`, `JobContext`, `JobResult`, `JobReport`, `JobError`, `FailureClass`, `CancellationToken`, `derive_idempotency_key`), and the additive provisional method `LocalRunStore.request_lock`. Additive, so a minor bump (55 names: 18 stable / 34 provisional / 3 deprecated). |
| 0.4.0 | 2026-11-22 | Milestone 2.3: +4 **provisional** names: `patchfrog.app.discovery.{run_discovery, DiscoveryResult}` and `patchfrog.scalability.project.{ApplicationModel, DiscoveryLimits}`; the additive provisional method `JobContext.read_file`. Additive, so a minor bump (59 names: 18 stable / 38 provisional / 3 deprecated). |
| 0.5.0 | 2026-11-23 | Milestone 2.4: +1 **provisional** name, `patchfrog.scalability.project.DatabaseModel` (the new `ApplicationModel.database` section, discovery schema 2). Additive, so a minor bump (60 names: 18 stable / 39 provisional / 3 deprecated). |
| 0.6.0 | 2026-11-23 | Milestone 2.5: +2 **provisional** names: `patchfrog.cli_scale.main` (runtime: `python -m patchfrog scale`, exit codes in `docs/scale-cli.md`) and `patchfrog.scalability.project.schema.application_model_json_schema` (the generated JSON Schema). Additive provisional changes: `LocalRunStore.get_run` raises `RunNotFoundError` (a `StoreError` subclass) for a missing run; `load_discovery` raises `DiscoveryArtifactError`/`UnsupportedDiscoverySchemaError`; `ApplicationModel.from_dict` rejects unknown fields, like the schema. 62 names: 18 stable / 41 provisional / 3 deprecated. |
