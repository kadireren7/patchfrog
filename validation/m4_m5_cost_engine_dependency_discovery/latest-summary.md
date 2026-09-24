# M4 (Ultra-Low-Cost Review Engine) + M5 (External Dependency Discovery + Contract Registry)

Status: **plan written before code** (workflow step 2). Sections marked
"RESULT" are filled in after implementation and validation.

Baseline: `main` @ `989567c` (Final beta-readiness pass). Branch:
`feat/m4-m5-cost-engine-dependency-discovery`.

Product direction this milestone serves:

> PatchFrog keeps software compatible with the APIs and SDKs it depends
> on. It detects upstream changes, maps their impact across codebases,
> generates migrations, verifies them, and opens evidence-backed pull
> requests.

Invariant: **change -> impact -> evidence -> verification -> decision.**
Generic PR review becomes a *secondary safety/verification capability*
that must be cheap enough to run inside future dependency-migration
workflows. M6 (upstream change detection, consumer impact/blast radius)
is explicitly **not** started here.

## 0. Phase 0 architecture audit

### 0.1 Module map (what exists today)

| Concern | Module(s) |
|---|---|
| repo indexing | `patchfrog/indexing/` (git ls-files inventory, parse cache), `patchfrog/parsing/` (tree-sitter Python/C/C++) |
| change analysis | `patchfrog/diff/`, `patchfrog/review/candidates.py`, `patchfrog/change_intelligence/` (J) |
| contract intelligence | `patchfrog/contract_intelligence/` (K: function-signature deltas, stale consumers) |
| caller/callee graph | `patchfrog/intelligence/graph.py` (`GraphNode`/`RepositoryEdge`/`EdgeKind`), `intelligence/queries.py` |
| reviewer orchestration | `patchfrog/review/orchestration.py` (`AgentOrchestrator`: per-candidate concurrent Correctness+Security fan-out) |
| critic orchestration | `orchestration._critique`, `review/critic.py`, `review/critic_selection.py`, `review/critic_policy.py` |
| provider routing | `patchfrog/routing/` (Model Router, one-hop fallback), `review/provider_factory.py`, `review/providers/*` |
| cost budgets | `review/budget.py` (`ReviewBudget` ledger: calls/retries/tokens/USD/elapsed), `review/effort.py` (per-candidate LIGHT/STANDARD/DEEP) |
| rate limiting | `review/rate_limiter.py` (RPM limiter, Retry-After) |
| GitHub publication | `patchfrog/publishing/` (planner/body/checks/service) |
| merge readiness | `patchfrog/merge_readiness/` |
| executable verification | `patchfrog/executable_verification/` (sandboxed targeted tests) |
| fix verification | `patchfrog/fix_verification/` |
| history/learning | `review_memory/` (Phase 7), `historical_regression_memory/` (N), `repository_learnings/` (O), `learning_records/` (Y), `feedback/` |
| cross-repo primitives | `cross_repo_intelligence/` (R) + `repository_contract_keys`/`repository_relations` tables |

### 0.2 Findings that shape the plan

1. **Call shape is per-candidate.** Every candidate gets 1 (LIGHT) or 2
   (STANDARD/DEEP) concurrent specialist calls plus per-proposal critic
   calls. A normal PR with 4 changed functions costs >= 4-8 reviewer
   calls. The existing Quality + Cost Guard reduces *per-candidate*
   effort but has no *PR-level* notion of risk, so "tiny PR = 1 call" is
   structurally impossible today.
2. **No zero-call path.** Docs-only and comment-only diffs still produce
   candidates (module regions / containing symbols) and therefore
   provider calls.
3. **Exact-head reuse already exists** for `SUCCEEDED` runs via the
   canonical `(repository, commit_sha, config_fp, model_fp,
   incremental_fp)` identity -- a rerun returns the stored run with zero
   provider calls. Missing: the cost policy is not part of that identity,
   and there is no explicit force-review bypass.
4. **Critic is already on-demand**: `_critique` returns with zero calls
   when no valid proposal exists, and `CriticSelectionPolicy` skips
   low-risk, high-confidence, statically corroborated proposals (and
   relaxes further for LIGHT). No change needed to the policy itself --
   only proof that it holds in the new single-pass path.
5. **`ReviewBudget` already counts retries, fallbacks and critic calls
   against one ledger** and terminates as `PARTIAL` with a typed reason;
   M4 builds on it (per-tier `max_provider_calls`) instead of adding a
   second budget.
6. **No external-dependency concept exists anywhere.** Contract
   intelligence (K) is *internal* function-contract deltas; cross-repo (R)
   is operator-registered internal contracts. Neither models
   third-party APIs/SDKs, manifests, lockfiles, OpenAPI, or env-var names.
7. Parsing supports only Python/C/C++. Node/TS SDK usage detection needs
   a lightweight, deterministic lexical scanner (no new parser dependency).

### 0.3 Classification of existing code

- **Reusable, extended**: `ReviewBudget`, `ReviewEffortPolicy`,
  `CriticSelectionPolicy`, `AgentOrchestrator` (post-proposal
  verification extracted into a reusable method), prompt shared rules,
  validation, Model Router, parser registry (symbol spans for usage
  sites), `GraphNode`/`RepositoryEdge` (dependency graph edges),
  indexing denylist, evaluation oracle/runner.
- **Unchanged**: publishing, merge readiness, executable/fix
  verification, J-R intelligence layers, feedback, review memory,
  routing, rate limiter.
- **Now secondary** (kept, not deleted): per-candidate specialist fan-out
  (`ReviewStrategy.SPECIALIST_FANOUT`) -- still used for escalation
  roles, as an operator-selectable strategy, and as the "before" arm of
  the cost benchmark.
- **Possibly removable later** (not removed here): `routing.router.is_small_review`
  (superseded by the change-risk classifier as the run-level "small"
  signal), the legacy `budget_state["used_input_tokens"]` shadow counter
  in `orchestration.py` (duplicates `ReviewBudget.max_input_tokens`).
- **Integration points for M5**: new `patchfrog/dependencies/` package
  (domain, discovery, adapters, OpenAPI, fingerprint, graph, registry),
  new persistence tables with `ON DELETE CASCADE` from `repositories`,
  new CLI `patchfrog.cli dependencies discover`.

## 1. M4 plan

- **M4.1** `patchfrog/change_risk/`: pure, deterministic
  `classify_change(diff_files) -> ChangeRiskClassification` with tiers
  `NO_AI/TINY/NORMAL/ELEVATED/HIGH_RISK`, typed signals, human reasons.
  Conservative comment detection (language-aware; `#` is never a comment
  in C/C++; pragmas/shebang/encoding/type comments count as code;
  Python triple-quoted strings disable comment-only).
  Security-sensitive *paths* alone never reach HIGH_RISK (project rule:
  a path keyword is never the sole decisive signal) -- they need
  corroboration.
- **M4.2** NO_AI: the review run is still created, persisted and
  `SUCCEEDED` with `risk_tier=no_ai` and a reason; zero reviewer, critic
  or fallback calls; the check/publication lifecycle runs unchanged.
- **M4.3** Single pass: one structured `review_response:unified` call
  covers all prepared candidates of a batch (correctness, security,
  contracts, regression, tests). Findings are validated against exactly
  the text sent, then attributed back to a candidate deterministically.
- **M4.4** Sequential escalation: Security / Correctness specialist
  calls only for ELEVATED/HIGH_RISK with an explicit typed reason and
  only when the tier budget can still afford a critic afterwards.
- **M4.5** Critic unchanged (on-demand); proven in the new path.
- **M4.6** TINY/NORMAL context capped and adaptive expansion disabled;
  initial/expanded token estimates and expansion reasons recorded.
- **M4.7** Cost policy folded into run identity; `force_review` bypass.
- **M4.8** Operator-configurable per-tier call budgets
  (`PATCHFROG_RISK_TIER_MAX_PROVIDER_CALLS`), applied as
  `min(repo max_provider_calls, tier budget)` on the existing ledger.
- **M4.9** `review_runs` gains risk tier, signals, escalation reasons,
  strategy, context metrics; telemetry snapshot gains a `cost` section.
- **M4.10** `patchfrog/evaluation/cost_benchmark.py` + CLI
  `eval cost-benchmark`: 10 deterministic fixtures, fake provider,
  deterministic pricing, legacy vs. cost-aware.

## 2. M5 plan

- Domain types (`ExternalDependency`, `DependencyUsageSite`,
  `DependencyContract`, `ContractFingerprint`, `DiscoveryEvidence`, ...).
- Safe file walker: git-tracked files only when a git repo, indexing
  denylist, symlinks refused, size cap, **`.env*` files never opened**;
  evidence stores only controlled tokens (module name, hostname,
  env-var *name*, SDK attribute chain) -- never source lines or values.
- Adapter interface + declarative `ProviderSpec` adapters (OpenAI,
  Stripe, GitHub) and a generic OpenAPI adapter; generic packages from
  manifests/lockfiles.
- OpenAPI 3.x / Swagger 2.0 normalization (paths, methods, params,
  bodies, responses, security, components) with order/format-insensitive
  fingerprints; descriptions/examples excluded.
- Registry tables (cascade from `repositories`), idempotent upsert,
  contract snapshot history keyed by fingerprint.
- Dependency graph expressed with the existing `GraphNode`/`RepositoryEdge`.
- CLI `python -m patchfrog.cli dependencies discover <path> [--json] [--persist]`.

## 3. Non-goals

No M6 (upstream monitoring, breaking-change classification, blast
radius), no Cloud changes, no live provider calls, no deletion of
working review capabilities.

## RESULT

(filled in after implementation)
