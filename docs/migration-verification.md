# Migration Verification Lab (M8)

`patchfrog/migration_verification/` answers the executable-evidence stage
of **change -> impact -> evidence -> verification -> decision**: given
M7's migration plan and generated patch, **what evidence would actually
show this migration restores compatibility, and what did PatchFrog
actually gather?** `MIGRATION_VERIFICATION_VERSION = 1`. Never calls a
provider -- `test_migration_verification_and_pr_modules_never_import_a_provider`
(`tests/unit/test_migration_verification_and_pr_module_boundaries.py`)
enforces this the same way
`test_deterministic_migration_modules_never_import_a_provider` already does
for M7.

A passing generic test, or a syntax/type check alone, is never called
`VERIFIED`. The decision engine (M8.11) is deterministic and explainable;
no LLM ever decides final truth.

```bash
# Bounded, sandboxed verification of a generated migration patch
python -m patchfrog.cli migrations verify --old old.yaml --new new.yaml \
  --hints hints.yaml --repo demo=path/to/repo

# The bundled demos, extended through M8 (see docs/migration-pr.md for M9)
python -m patchfrog.cli migrations demo             # human-required path
python -m patchfrog.cli migrations demo --verified  # fully-automatic, VERIFIED path
```

## Pipeline (`service.py`)

```
plan + patch + blast radii + exact repository state
  -> requirements (requirements.py)         -- M8.2
  -> targeted test selection (test_selection.py) -- M8.3
  -> bounded execution plan (execution_plan.py)  -- M8.4
  -> sandboxed execution (via executable_verification.sandbox) -- M8.5
  -> baseline-vs-patched comparison (baseline.py) -- M8.6
  -> contract verification (contract.py)    -- M8.7
  -> regression detection (regression.py)   -- M8.8
  -> evidence bundle (evidence.py)           -- M8.9/M8.10
  -> decision (decision.py)                  -- M8.11
```

`run_migration_verification` is safe to call for any migration result,
including one with no patch at all (a plan that is entirely
`HUMAN_REQUIRED`): it returns an evidence-free bundle immediately, no
sandbox touched.

## Requirement generation and targeted test selection (M8.2/M8.3)

Requirements are generated only from what the migration actually claims:
syntax validity, import resolution, a type check when the repository has
one configured, contract-specific checks per M6 diff item kind, and --
only when consumer-behavior evidence is possible -- targeted unit/
integration tests. The plan is bounded (`MAX_VERIFICATION_STEPS_PER_PLAN`,
`MAX_TARGETED_TESTS_PER_PLAN`); PatchFrog never runs "everything" when
targeted evidence is enough.

Test selection reuses M8's own affected-symbol/blast-radius evidence
(`BlastRadius.related_tests`) and the caller graph, and classifies every
selection:

| Classification | Meaning |
|---|---|
| `DIRECT` | a test file already known to reference the affected symbol/usage site |
| `TRANSITIVE` | reached through the caller graph, not a direct reference |
| `FALLBACK` | no direct/transitive evidence; a broader, lower-confidence net |
| `UNKNOWN` | selection exists but confidence could not be established |

Every selection carries its own `reason` -- the mapping is never claimed
perfect.

## Safe execution boundary (M8.5)

Every verification step runs through
`patchfrog.executable_verification.sandbox.VerificationSandbox` (`bwrap` +
`prlimit`) -- the same primitive Executable Verification already uses for
finding-fix verification; M8 adds no second sandbox. Fixed working
directory, bounded timeout, bounded stdout/stderr capture, no network by
default, no secret values injected, no git push, no package-publish/
deploy command, process termination on timeout. Where the sandbox is
unavailable on a host, every step comes back `CheckStatus.UNAVAILABLE`
with an honest `detail` -- never silently skipped, never guessed passing.

## Baseline-vs-patched comparison (M8.6)

Bounded to `MAX_BASELINE_COMPARISONS` (2) `DIRECT`/`TRANSITIVE` selections
only -- `FALLBACK`/`UNKNOWN` selections are heuristic-strength at best and
never worth spending a second sandbox run on. Reuses
`executable_verification.snapshot_staging` to export the baseline
(pre-migration) commit state and run the identical check against it, then
against the patched workspace:

| Baseline | Patched | Outcome |
|---|---|---|
| fail | pass | `BASELINE_FAIL_PATCHED_PASS` -- strong fix evidence |
| pass | pass | compatibility evidence, not proof of an original failure |
| fail | fail (differently) | unresolved |
| pass | fail | regression |
| unavailable | -- | evidence weaker; said so explicitly, never guessed |

## Contract verification (M8.7)

Reuses M6's own `ContractDiffItem`/`DiffItemKind` evidence and M7's
`PatchLinkage` directly -- never re-derives them, never relies on string
matching alone where parser/symbol information already exists (the same
AST-based location M7's rewriters use).

## Regression detection (M8.8)

Distinct from `UNVERIFIED`: a migration that introduces new damage
(syntax failure, type failure, a targeted-test regression, an obsolete
symbol newly introduced, a dependency conflict, a contract mismatch, a
safety-gate regression) is `REGRESSION_DETECTED`, never merely
"insufficient evidence."

## Evidence bundle and strength (M8.9/M8.10)

`MigrationEvidenceBundle` is first-class, reproducible, and
machine-readable: upstream change fingerprint, repository + exact head
SHA, patch fingerprint, the verification plan (with its own
`fingerprint()`), every step's evidence, contract checks, baseline
comparisons, coverage (satisfied/failed/not-run/unavailable, split from
mandatory), evidence strength, residual risk, and the final outcome with
explicit reasons. Only bounded summaries, hashes, exit status and timing
are persisted -- never raw logs beyond a short bounded excerpt, never
secrets.

`bundle_fingerprint` ties the bundle to an *exact* patch/change/repository
state -- see M9.8's integrity check in `docs/migration-pr.md`, which
enforces that a `VERIFIED` bundle for one patch is never reused for
another.

| Evidence strength | Example |
|---|---|
| `STRONG` | baseline-fail/patched-pass on a direct check; all mandatory contract checks passed |
| `MODERATE` | a directly-related test/type check passes on the patched state alone |
| `WEAK` | a heuristic/fallback-only signal, or many generic passes with no direct relation |
| `NONE` | nothing gathered (no patch, or no requirements at all) |

Many weak signals never combine into `STRONG` -- see
`test_many_weak_signals_never_promote_to_strong`.

## Decision engine (M8.11)

Mirrors the evidence-combination discipline
`patchfrog.fix_verification.domain.FixEvidenceDirection` reached only
after two real correction rounds (`validation/agent_handoff/latest-summary.md`):
a single strong contradicting signal (a regression, a failed mandatory
contract check) always wins outright; only strong, direct evidence may
reach `VERIFIED`; any combination of only weak/generic evidence never
promotes above `PARTIALLY_VERIFIED`; missing required evidence is
`UNVERIFIED`, never guessed in either direction.

| Outcome | Meaning |
|---|---|
| `VERIFIED` | all mandatory requirements satisfied, no regression, sufficient evidence strength |
| `PARTIALLY_VERIFIED` | important checks pass, some required evidence unavailable, no known regression |
| `UNVERIFIED` | insufficient evidence |
| `FAILED` | the migration does not restore required compatibility |
| `REGRESSION_DETECTED` | the migration introduces a new failure |
| `HUMAN_REQUIRED` | the migration plan itself has an unresolved human-required step |

## Persistence (`store.py`, migration `0036_migration_verification`)

`migration_verification_runs` -- one bounded row per (patch, bundle
fingerprint); a repeated identical run is idempotent (re-verifying does
not create a duplicate row). Chained after `0035_upstream_change_migration`.

## Fix Verification is deliberately not reused

`patchfrog.fix_verification` is finding-centric (keyed by `handoff_id`,
answers "does this fix a specific reported finding"). Migration
Verification answers a different question ("does this migration restore
compatibility with an upstream change") over a different identity
(change/patch fingerprint). Only the evidence-combination *discipline* is
shared; the enums, modules, and persistence are deliberately separate, per
this package's own instruction not to overload existing review-status
enums with a second, unrelated meaning.

## Known limitations

- Baseline comparisons only run for `DIRECT`/`TRANSITIVE` test selections,
  bounded to 2 per verification -- a migration with many affected call
  sites gets baseline proof for its strongest evidence only, not every site.
- Where the sandbox itself is unavailable on a host, every step is
  reported `UNAVAILABLE` rather than run -- this is honest, bounded
  degradation, not a silent pass.
- Type checking only runs where the target repository already has mypy
  configured; PatchFrog never invents a type-checker configuration.

## Deliberately out of scope

Opening a PR with the verified migration (M9, see `docs/migration-pr.md`);
any verification requirement without a deterministic evidence source
(left `UNVERIFIED`/`HUMAN_REQUIRED`, never guessed).
