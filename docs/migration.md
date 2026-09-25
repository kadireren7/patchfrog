# Migration Planner + Generated Fix (M7)

`patchfrog/migration/` answers the last deterministic stage of **change ->
impact -> evidence -> verification -> decision**: given M6's classified
change and consumer impact, **what exactly must each affected call site
do, can it be fixed automatically, and if so, what is the literal patch?**
`MIGRATION_ENGINE_VERSION = 1`. "Verification" here means deterministic
structural/contract validation only (the safety gates below) --
executable/runtime verification is M8 and automated migration PRs are
M9; neither is implemented in this milestone.

```bash
# Plan only (one repository, or several with --repo NAME=PATH, repeatable)
python -m patchfrog.cli migrations plan --old old.yaml --new new.yaml \
  --hints hints.yaml --repo demo=path/to/repo

# Plan + generate a unified diff (never writes the checkout unless --write)
python -m patchfrog.cli migrations generate --old old.yaml --new new.yaml \
  --hints hints.yaml --repo demo=path/to/repo --output-dir ./patches

# The bundled, fully offline demo (fictional acme-ai SDK; see
# tests/fixtures/upstream_changes/demo/README.md) -- no real vendor described
python -m patchfrog.cli migrations demo
```

## Plan model (`domain.py`, `planner.py`)

`MigrationPlan` is a per-repository, per-change document: a
`MigrationStatus` (PLANNED/PATCH_GENERATED/PARTIAL/HUMAN_REQUIRED/
UNSUPPORTED/FAILED), a `ResidualRisk`, and an ordered tuple of
`MigrationStep`s. Each step ties one M6 diff item to one affected M6
usage site and assigns a `MigrationStrategy` (rename_symbol,
rename_parameter, add_required_parameter, remove_argument,
replace_enum_value, adapt_return_field, bump_package_version, ...) and
an `AutoFixEligibility`:

| Eligibility | Meaning |
|---|---|
| `AUTO_SAFE` | one-to-one, value-free rewrite (a rename) -- no residual uncertainty |
| `AUTO_WITH_REVIEW` | a value is known (a hint, or derivable from the diff itself) but the change has some residual scope limit, stated per step |
| `HUMAN_REQUIRED` | no deterministic value source exists (e.g. a required parameter with no hinted value, or a removed argument with no replacement) |
| `UNSUPPORTED` | no rewriter exists for this language/strategy combination |

**Values are never invented.** `planner.py` only proposes a strategy a
step can actually carry out from evidence already on hand (a hint's
literal value, the diff item's own old/new names); if no source exists,
the step is `HUMAN_REQUIRED` and carries no fabricated value.

## Deterministic rewrite (`python_rewrite.py`, `js_rewrite.py`, `manifest_rewrite.py`, `edits.py`)

Each step becomes a `TextEdit`: an exact character span plus replacement
text, located by the standard-library `ast` for Python or a lexical scan
for JS/TS -- never a line-based patch, never a regex over the whole file.
All other bytes are untouched (formatting, comments, unrelated code
survive verbatim). `manifest_rewrite.py` handles version bumps in
`requirements.txt`/`package.json` the same way -- one field changed, the
rest of the file byte-identical.

## Patch generation + idempotency (`generator.py`)

`generate_patch(plan, root)` is pure: it reads the checkout, never writes
it, and returns a `GeneratedPatch` (a `PatchOrigin`, a unified diff, the
modified files, a `StepResult` per step, and the safety-gate results
below). Re-running generation against an **already-migrated** checkout is
a no-op: the planner itself finds zero remaining affected usage sites
(the obsolete call shape is gone), so the plan has zero steps and the
patch is not a candidate -- confirmed empirically against the bundled
demo (`migrations demo --write` once, then `migrations generate` again
against the same checkout: `modified_files: []`, `is_candidate: false`,
empty diff).

## Safety gates (`safety.py`)

Every generated patch, deterministic or model-assisted, passes the same
gates before it is ever called a "candidate":

| Gate | Checks |
|---|---|
| `allowed_files` | only files a plan step actually targets were touched |
| `edits_match_steps` | every changed line falls inside an applied step's own edit scope |
| `no_secret_files` | no secret-store path touched, no credential-shaped literal introduced |
| `no_dependency_removed` | a manifest edit never drops a declared dependency |
| `bounded_diff` | the changed-line count stays within a size bound |
| `line_stability` | line numbers outside the edited spans are preserved |
| `syntax` | every modified file still parses |
| `obsolete_shape_gone` | the migrated usage no longer matches the pre-change shape |

A patch is a **candidate** only if every gate passes; otherwise its
`StepResult`s explain exactly which step failed and why.

## Optional model-assisted path (`assist.py`)

Used only when a deterministic strategy leaves a step `HUMAN_REQUIRED`
for a single, already-located SDK call with no known value
(`add_required_parameter` / `replace_enum_value`). The prompt gives the
model exactly that one line of code and the step's own facts -- never
another file, never the repository. The model's entire output is one
structured field (the literal value); the character-span edit is still
produced by the same deterministic rewriter a hint would use, so a
hallucinated value fails the same `RewriteError` path a bad hint would.
The result runs through the identical safety gates and comes back as its
own `GeneratedPatch` with `origin=PatchOrigin.MODEL_ASSISTED` -- never
merged into, or silently trusted alongside, the deterministic patch.
Nothing in the deterministic planner or generator calls this module, and
no CLI command defaults to it; a structural test
(`test_deterministic_migration_modules_never_import_a_provider`) enforces
that every other module in this package stays provider-free.

## Persistence (`store.py`, migration `0035_upstream_change_migration`)

`migration_plans` -- one row per plan fingerprint, which is the
originating change plus a `base_content_fingerprint`: a sha256 over
every file any step targets, tying the plan to an *exact* repository
content state independent of whether that state is a git commit (a
local, uncommitted checkout has none). `migration_patches` -- one row
per (plan, patch fingerprint); a regenerated identical patch is a no-op
write. `PatchLinkage` (change fingerprint, dependency keys, diff-item
keys, usage-site keys, base commit/content fingerprint, plan/patch
fingerprint) is what a future M8 verification run and M9 PR-idempotency
check key on.

## Deliberately out of scope

Executable/runtime verification that a generated patch actually fixes
the break (M8); opening a PR with the generated patch (M9); any
migration strategy without a deterministic or hint/model-sourced value
(left `HUMAN_REQUIRED`, never guessed).
