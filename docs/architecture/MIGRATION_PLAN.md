# Migration Plan — Current → Target

Strategy: **strangler, additive-first.** New capability goes into new packages behind new
entry points. Existing code moves only when a new consumer needs it, and then through a re-export
shim so that no existing import path breaks (Cloud pins this engine, ADR-015). Each step is
independently mergeable and revertible.

## Guarantees during migration

1. Existing CLI commands, Celery task names (CI asserts 9), API routes, MCP review tools and
   `Settings` env vars keep working unchanged.
2. No Alembic migration is added for the scalability line before server mode (Phase 8).
3. The full existing suite plus the oracle eval sanity run stays green at every step.
4. Every moved symbol keeps a re-export at its old path, marked deprecated in its docstring, for
   ≥ 1 minor release.

## Steps

| # | Step | Touches | Reversible by | Phase |
|---|---|---|---|---|
| M1 ✅ | Add `patchfrog/config/core.py` (`CoreSettings`, no required fields). `Settings(CoreSettings)` keeps an identical env contract (pinned by test); `ServerSettings` alias | config | revert subclass + delete file | 1.1 (done 2026-11-22) |
| M2 ✅ | Lazy Celery import in `cli.py` (the `apps.worker` imports moved into `_ops_retry_async` and `_mcp_serve`). Tests: all 56 subcommand `--help` paths and `dependencies discover` work in an empty env; both dispatching paths still work in server mode | cli.py | revert hunk | 1.1 (done 2026-11-22) |
| M3 ✅ | Architecture boundary tests (`tests/architecture/`): R1–R4, shrink-only SCC baseline (1 × 29 units, 156 edges), downstream import contract, replaced in 1.3 by the public API contract (ADR-017) (no repo-wide cleanup, D1) | tests | delete `tests/architecture/` | 1.2 (done 2026-11-22) |
| M4 ✅ | (1.3) Create the `patchfrog/codegraph/` neutral package. Move `upstream/code_graph.py` there (byte-identical implementation), keeping the re-export in `upstream/code_graph.py`; `upstream.workspace`/`blast_radius` import the new path | codegraph, upstream | restore `upstream/code_graph.py` from git, delete `codegraph/`, revert 2 import lines | 1.3 (2026-11-22) |
| M5 | `patchfrog/app/` skeleton: `jobs.py` (JobRunner, lifecycle, budgets, cancellation; **done in 2.2**), `store.py` (LocalRunStore; **done in 2.1**; a `RunStore` protocol is deferred until a second implementation exists), `contracts/` (pydantic wire models + JSON Schema export; not started) | new | delete package | 2 |
| M6 | `patchfrog scale` CLI group (`analyze`, `show`, `findings`, `cache prune`) using `CoreSettings` only | cli (new module `cli_scale.py`) | remove registration | 2–3 |
| M7 | `patchfrog/scalability/` engine: project model, semantic pass, 3 rules, remediation catalogue | new | delete package | 3 |
| M8 | MCP: register the `scalability_*` tools. Register the review tools only if `DATABASE_URL` is present, which is a behaviour change: today the server fails without it. Document it in PROGRESS | mcp | revert | 3–4 |
| M9 | Extract the review-independent execution core from `executable_verification/sandbox.py` + `pytest_adapter.py` into `patchfrog/execution/`, with a re-export | executable_verification | restore | 5 |
| M10 | `patchfrog/perf/` probes + pytest plugin + comparison statistics | new | delete | 5 |
| M11 | Packaging: optional extras (`[server]`, `[review]`, `[semgrep]`, `[providers]`). The base install becomes slim. Dockerfile installs `[server,review,semgrep,providers]`, so images are unchanged in content | pyproject, Dockerfile | revert | 6 |
| M12 | `pyproject` `license` → SPDX string (only after ADR-013 is decided; the deadline is setuptools 2027-02-18) | pyproject | revert | 6/7 |

## Cloud compatibility check (ADR-015, mandatory before M8, M9, M11, M12 and any change to a Cloud-consumed component)

1. Identify whether the component is consumed downstream: is it in the Public Engine API
   (`docs/architecture/PUBLIC_API.md`) or named in `docs/product-boundary.md`? For the hosted
   product, also check its private import manifest, held outside this repository (ADR-017).
2. Make sure a regression test pins its current contract. If none exists, add it *first*, in a
   separate step.
3. Prefer additive changes and re-exports at old paths.
4. Record the impact assessment in `docs/PROGRESS.md`.
5. Never edit Cloud code from this repository.

## Risky steps and mitigations

- **M8**: MCP startup behaviour changes. Mitigation: explicit startup log of which tool groups are
  registered, plus a test for both configurations.
- **M11**: breaking change for anyone running `pip install patchfrog` and expecting the review
  stack. Mitigation: `[all]` extra, a release note, the Docker image unchanged, and the
  Cloud repo pins updated in the same coordinated change.
- **M9**: the sandbox is security-critical. Mitigation: a pure move plus re-export, the existing
  isolation tests unchanged, no behaviour change, and a separate PR.

## Out of scope for migration

The frozen product lines aren't refactored to the new layering. Their 31 package cycles stay
until a concrete need arises (e.g. a server-mode `PostgresRunStore` touching persistence).
