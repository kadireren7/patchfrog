# M4 (Ultra-Low-Cost Review Engine) + M5 (External Dependency Discovery + Contract Registry)

Status: plan written before code (workflow step 2); implementation and
validation results are in the RESULT section below.

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

### R1. Deliberate deviation from the brief: TINY total = 2

The suggested TINY ceiling of 1 total call would make a tiny PR's
HIGH/security finding impossible to critic-verify, and the existing
safety rule would then suppress it: cost saved by dropping a useful
finding. Budgets are therefore split into a review-phase ceiling (TINY =
**1 reviewer call**, as specified) plus a verification reserve only the
critic can use. A clean tiny PR costs exactly 1 call. `{"tiny":1}`
restores the strict policy (tested: finding suppressed, never published
unverified). Totals: no_ai 0, tiny 2 (1+1), normal 2 (1+1), elevated 3
(2+1), high_risk 5 (3+2).

### R2. M4 cost benchmark (`evaluation_baselines/m4_cost_benchmark.*`)

Fake reviewer, deterministic token estimates of the exact prompts,
synthetic prices ($1/$4 per M tokens). Measures call shape/cost only.

| scenario | tier | calls before -> after | critic | input tokens | findings |
|---|---|---|---|---|---|
| comment_only | no_ai | 1 -> 0 | 0 -> 0 | 2023 -> 0 | 0/0 |
| docs_only | no_ai | 1 -> 0 | 0 -> 0 | 1742 -> 0 | 0/0 |
| tiny_code | tiny | 1 -> 1 | 0 -> 0 | 1787 -> 1822 | 0/0 |
| normal_correctness_bug | normal | 6 -> 1 | 1 -> 0 | 12064 -> 2906 | 1/1 |
| medium_cross_module | elevated | 4 -> 1 | 0 -> 0 | 7537 -> 2478 | 0/0 |
| auth_sensitive | high_risk | 3 -> 3 | 1 -> 1 | 5197 -> 5245 | 1/1 |
| schema_migration | high_risk | 4 -> 2 | 0 -> 0 | 6969 -> 3938 | 0/0 |
| public_api_change | elevated | 1 -> 1 | 0 -> 0 | 2032 -> 2067 | 0/0 |
| test_only | tiny | 2 -> 1 | 0 -> 0 | 3579 -> 1927 | 0/0 |
| exact_head_repeat | tiny | 0 -> 0 | 0 -> 0 | 0 -> 0 | 0/0 |

Totals: 23 -> 10 provider calls, 42,930 -> 20,383 input tokens,
$0.046 -> $0.022 (synthetic). Every target met, no finding lost.
Exact-head reuse already existed pre-M4 (the "before" repeat is 0 too).
`auth_sensitive` is cost-neutral, not cheaper: single pass + one security
escalation + critic = the legacy two specialists + critic. An earlier
draft also escalated Correctness on the same candidate (3 -> 4); it was
removed because the single pass already covers correctness there.

### R3. Deterministic beta-readiness quality guard (20-case profile, `--repeat 2`)

| metric | before (main) | after (cost-aware) |
|---|---|---|
| expectation pass rate | 1.0 | 1.0 |
| candidate recall | 1.0 | 1.0 |
| accepted-finding recall | 1.0 | 1.0 |
| false-positive rate | 0.0 | 0.0 |
| false-negative rate | 0.0 | 0.0 |
| critic rejection / false-negative rate | 0.0 / 0.0 | 0.0 / 0.0 |
| repeated-run variance | 0.0 | 0.0 |
| reviewer calls (sum over cases) | 84 | 22 |
| critic calls (sum over cases) | 10 | 10 |

The `specialist_fanout` arm run through the new code reproduces main's
per-case call shape exactly. **This suite is an oracle-scripted
pipeline-correctness benchmark: it proves plumbing, not real-model review
quality.** The eval harness diffs whole fixture files as additions, so
`beta-comment-only-change` still makes one call there; the true
comment-only path is proven by the cost benchmark and integration tests.

### R4. M5

Detection: OpenAI, Stripe, GitHub (SDK + REST), generic OpenAPI (3.x and
Swagger 2.0, local specs, path literals, server hosts, generated-client
markers), generic declared packages (PyPI/npm/Go manifests; npm/yarn/
pnpm/poetry/uv/Pipfile lockfiles). Registry migration
`0034_external_dep_registry`. The mixed fixture yields openai:pypi (8
sites, ==1.40.0, high), stripe:npm (6 sites, ^12.0.0 resolved 12.3.0,
high), github:http (4 sites, medium), openapi:openapi.yaml (2 ops, 3
sites), package:pypi:requests. The false-positive fixture yields nothing.
Revision-id length (33 > alembic's 32) was caught on real Postgres and
the id shortened.

### R5. Validation

ruff clean; `mypy . --strict` clean (692 files); single Alembic head;
34/34 migrations on a brand-new Postgres; 0032<->0034 down/up round
trip; full pytest with `PATCHFROG_REQUIRE_POSTGRES=1`: 2652 passed, 0 failed;
both Docker images built, worker image registers 9/9 Celery tasks; regex secret scan over the full
branch diff: 0 hits (tracked diff only -- it proves nothing about
terminal/local exposure). No live provider call was made anywhere.
