# PatchFrog quality evaluation harness (Phase 8)

`patchfrog/evaluation/` is PatchFrog's quality regression harness: a
deterministic, repeatable way to answer "did a change make PatchFrog
better or worse?" without ever asking an LLM to grade itself.

## Architecture, through Phase 8

```
patchfrog/
  github/         Phase 1  ingestion (webhooks, App auth)
  indexing/       Phase 2  repository intelligence (tree-sitter, symbol graph)
  analysis/       Phase 3  static analysis (ruff/semgrep/cppcheck/clang-tidy)
  context/        Phase 4  deterministic, non-LLM context selection
  review/         Phase 5  AI reviewer + critic
  publishing/     Phase 6  GitHub review publishing
  review_memory/  Phase 7  incremental review + review memory
  evaluation/     Phase 8  quality evaluation harness (this document)
```

`patchfrog/evaluation/` **calls** the production packages above (real
indexing, real static analysis, real context, real review/critic, real
persistence) — it never reimplements or duplicates their logic. The
dependency is one-directional: production code has zero imports from
`patchfrog.evaluation` (`tests/integration/test_evaluation_no_label_leakage.py`
is the regression test proving the reviewer prompt itself never receives
benchmark ground truth).

Modules:

| module          | role |
|------------------|------|
| `domain.py`      | pure dataclasses/enums — the stable schema everything else shares |
| `fixtures.py`    | loads/validates `case.yaml` + fixture repos from `tests/fixtures/evaluation/cases/` |
| `matcher.py`     | deterministic predicted-finding ↔ ground-truth matching (no LLM, no embeddings) |
| `metrics.py`     | pure aggregation: precision/recall/F1, clean-case rate, severity calibration, category/difficulty breakdowns, hallucination rate, critic comparison, static/AI overlap |
| `runner.py`      | orchestrates real production components against one case, and a whole suite |
| `oracle.py`      | scripts a `FakeLLMProvider` from a case's own ground truth — see "Two kinds of AI numbers" below |
| `incremental.py` | Phase 7 multi-commit incremental-review-memory benchmark scenarios |
| `regression.py`  | identity-gated comparison between two runs, with configurable thresholds |
| `reporting.py`   | JSON (canonical) + Markdown report generation |
| `queries.py`     | read-only lookup of baseline artifacts on disk |

## Benchmark philosophy

**No "it looks good" evaluation.** Every benchmark case has an
explicit, human-authored, committed ground truth (`case.yaml`). No LLM
is ever the judge of the canonical score — matching is deterministic
and explainable (`matcher.py`): file path, category, symbol (with a
tolerant qualified-name suffix match), line-range overlap/tolerance,
and an optional evidence substring.

**Precision over recall.** A small recall increase never justifies a
large false-positive increase. `regression.py` encodes this directly:
the default precision-drop threshold is a strict 3 points, while the
recall-drop threshold is a much looser 10 points.

**Clean cases are mandatory, not optional.** ~34% of the corpus (20/59
at the time of writing) is intentionally bug-free code that is designed
to *look* suspicious — a "false-positive trap." A benchmark with only
bugs cannot measure false positives at all.

**A wrong carry-forward is worse than an extra LLM call.** Every
incremental-review-memory scenario in `incremental.py` explicitly
computes `unsafe_carry_forward` and the regression thresholds treat any
nonzero value as an automatic failure.

## Two kinds of AI numbers — read this before trusting a number

There are two fundamentally different things a `full_pipeline`/`ai_only`
run can measure, and every report labels which one it is
(`report["benchmark_label"]`):

- **`pipeline_correctness`** (`--provider fake`, the default): the
  reviewer/critic are a `FakeLLMProvider` scripted by
  `oracle.py` from the case's *own* committed ground truth. A perfect
  score here proves the plumbing — candidate generation, context
  building, prompt construction, Phase 5 validation, critic review,
  confidence aggregation, dedup, persistence — correctly carries a
  *known-correct* finding through to an accepted finding, and correctly
  produces zero findings when none are expected. **It proves nothing
  about whether a real model would actually have found the bug.**
- **`ai_quality`** (`--provider live`, requires `ANTHROPIC_API_KEY`):
  the real configured Anthropic provider. This is the only number that
  measures actual reviewer quality.

`report["ai_quality_measured"]` is `false` whenever `--provider fake`
was used. Never read a `pipeline_correctness` precision/recall number as
"how good is the AI reviewer" — it isn't measuring that.

If `ANTHROPIC_API_KEY` is not set in the environment, `--provider live`
fails fast with a clear `MissingProviderCredentialsError` message. This
is expected in most dev/CI environments; the fake-provider path is not
a fallback hack, it's the intended default for infrastructure
correctness.

## Running the harness

```bash
# Full corpus, pipeline-correctness (fake) provider, critic on:
python -m patchfrog.cli eval run

# Filtered subset:
python -m patchfrog.cli eval run --tag security --language python --difficulty hard
python -m patchfrog.cli eval run --case py-inverted-boundary --case c-memory-leak

# Static analyzers only, no LLM at all:
python -m patchfrog.cli eval run --mode static_only

# STATIC_ONLY reported as its own section (TP/FP/missed, per-analyzer
# coverage, clean-case static false positives) alongside a FULL_PIPELINE
# run -- together with FULL_PIPELINE's own static_ai_overlap metric,
# this is the real static-vs-AI overlap matrix (real static output +
# oracle AI output, not two independently-run numbers):
python -m patchfrog.cli eval run --static-only-comparison

# Critic value measurement (runs the corpus twice: critic off, critic on):
python -m patchfrog.cli eval run --critic both

# Context Engine ablation (normal ranked context vs. target-only vs.
# no-extra-context -- target symbol alone, nothing else):
python -m patchfrog.cli eval run --context-ablation

# Phase 7 incremental-review-memory scenarios:
python -m patchfrog.cli eval run --incremental-scenarios

# Everything at once (what the committed baseline is generated with):
python -m patchfrog.cli eval run --critic both --context-ablation \
    --static-only-comparison --incremental-scenarios

# Real live-provider run (requires ANTHROPIC_API_KEY):
python -m patchfrog.cli eval run --provider live
```

Every `eval run` writes a JSON report (default
`evaluation_baselines/latest_run.json`) and prints a one-line summary.
Pass `--markdown-output PATH` for a human-readable Markdown report, or
`--json` to print the full JSON to stdout.

### Comparing against the baseline

```bash
python -m patchfrog.cli eval compare
```

Exit codes are CI-friendly and distinguish two different kinds of
failure:

- `0` — within regression thresholds.
- `1` — a real quality regression (precision, clean-case pass rate,
  hallucination, duplicate rate, or unsafe carry-forward crossed its
  threshold).
- `2` — the two runs' identities aren't even comparable (different
  benchmark version, mode, provider, prompt/policy/engine version) —
  refused outright rather than producing a misleading verdict.

Thresholds are configurable: `--max-precision-drop`,
`--max-clean-pass-rate-drop`, `--max-recall-drop` (recall's default is
intentionally loose — see "precision over recall" above).

### Updating the golden baseline

The baseline is never overwritten silently:

```bash
python -m patchfrog.cli eval update-baseline --confirm
```

Without `--confirm` this refuses and exits 1. This should only happen
after a human has reviewed `eval compare`'s output and decided the new
numbers are the new normal — never automatically from a single
stochastic live-provider run (see spec section 54: "do not update the
golden baseline automatically from one stochastic run").

### Rendering an existing report as Markdown

```bash
python -m patchfrog.cli eval report --input evaluation_baselines/phase8_baseline.json
```

## Interpreting a report

- **Overall**: TP/FP/missed, precision/recall/F1, false positives per
  case and per 100 review candidates.
- **Safety**: clean-case pass rate (what fraction of bug-free cases
  produced zero accepted findings), duplicate rate, hallucination rate
  **before and after** Phase 5 validation (proves the guardrails add
  real value), severity calibration (exact match / within-one-level /
  overstatement / understatement — critical/high overstatement is the
  one to watch).
- **Category / difficulty breakdown**: every category and difficulty
  level is reported even at low support — never hidden.
- **Static / AI overlap**: findings caught by static analysis only, AI
  only, both, or missed by both — a genuine matrix computed from one
  `FULL_PIPELINE` run's real per-prediction sources (real ruff/semgrep/
  cppcheck/clang-tidy output alongside the oracle's AI output), never
  two independently-run numbers stitched together after the fact.
- **`static_only`** (with `--static-only-comparison`): the real Phase 3
  engine run standalone, no LLM involved — static TP/FP/missed, a
  **per-analyzer coverage table** (attempted/succeeded/failed/skipped/
  unsupported/raw-findings per analyzer — an analyzer that's missing
  from the machine shows as `unsupported`, never silently as "zero
  findings"), and clean-case static false positives.
- **Efficiency**: candidates generated/reviewed/skipped, provider
  calls, input/output tokens — all normalized per true positive so
  models/prompts are comparable.
- **Incremental review memory**: scenarios passed, total provider calls
  avoided, and — most importantly — `unsafe_carry_forward_count`, which
  must always read `0`.
- **`critic_comparison`** (with `--critic both`): full metrics (TP/FP/
  missed/precision/recall, unsupported proposals/final findings,
  severity overstatement) for critic-off and critic-on separately, plus
  `precision_delta`/`recall_delta`/`false_positive_delta`/
  `unsupported_delta`. Always labeled `pipeline/guardrail behavior`
  under the fake oracle — the critic is not assumed to always help, and
  this never claims real model-quality evidence; only a `--provider
  live` run does that.
- **`context_ablation`** (with `--context-ablation`): full metrics
  (candidates reviewed, provider calls, TP/FP/missed, input tokens,
  evidence-validation/hallucination outcomes, wall-clock runtime) under
  three variants — normal ranked context, target-only, and
  no-extra-context (just the target symbol, nothing else). Always
  labeled `pipeline/guardrail behavior` under the fake oracle for the
  same reason as above: the oracle doesn't actually consult context to
  decide what to report, so a fake-provider ablation run measures
  plumbing correctness across context configs, not real semantic
  quality — only `--provider live` measures that.

## The benchmark corpus

`tests/fixtures/evaluation/cases/<case-id>/` — one directory per case:

```
<case-id>/
  case.yaml   human-authored, committed ground truth
  repo/       a plain file tree (no .git — materialized as a real,
              throwaway git repo by the runner for each run)
```

`case.yaml` schema (see any committed case for a concrete example, and
`patchfrog/evaluation/fixtures.py`/`domain.py` for the authoritative
parser/dataclasses):

```yaml
case:
  id: python-inverted-boundary       # must equal the directory name
  title: ...
  description: ...
  language: python                    # python | c | cpp
  difficulty: easy                    # easy | medium | hard
  tags: [boundary, correctness]

expected:                             # omit/empty -> a "clean" case
  - id: ef1                           # unique within this case
    category: correctness             # patchfrog.analysis.domain.FindingCategory
    file: src/payment.py              # relative to repo/
    symbol: can_withdraw              # bare function/method name; must
                                       # appear literally in the file
    issue_family: inverted_comparison # free-form grouping tag, reporting only
    severity: high                    # or severity_min/severity_max
    line: 14                          # 1-indexed
    line_tolerance: 3                 # default 3
    ground_truth_source: ai_expected  # static_expected | ai_expected | either
    notes: ...

forbidden:                            # optional
  - reason: style-only nitpick, not a real bug
    category: maintainability
```

Ground-truth validation (`fixtures.validate_and_raise`) runs before any
benchmark run starts and fails fast on: a missing expected file, an
out-of-range line, `line_end < line`, a symbol that doesn't literally
appear in the file, duplicate expected-finding ids, an accidental
duplicate `(file, symbol, category, line)` pair, or a forbidden rule
missing its reason/category.

**Static toolchain note**: this development environment has `ruff` and
`semgrep` available (both pip-installed into the venv) but not the
system binaries `cppcheck`/`clang-tidy` (the worker Docker image *does*
bundle both — confirmed by a real `docker build`). A handful of cases
are deliberately built to be caught by the
real, installed analyzers without any AI call — `ground_truth_source:
static_expected` (undefined names via ruff's F821, bare `except:` via
ruff's E722, an unsafe `strcpy` via PatchFrog's bundled semgrep rule
`patchfrog-c-unsafe-strcpy`) and `ground_truth_source: either` (a
second bare-except and an `eval()` case, both caught by *both* ruff/
semgrep and the AI oracle — a genuine, non-degenerate static/AI overlap
"both" bucket, not just AI-only). Every other bug case remains
`ai_expected` — semantic bugs (inverted comparisons, wrong argument
order, stale cache invalidation, ...) that no static rule is expected
to catch, and that's the correct, honest state: Phase 8 does not force
every case to be static-detectable. A missing analyzer is always
reported as a missing capability (Phase 3's own
`AnalyzerExecutionStatus.UNSUPPORTED`, surfaced per-analyzer in every
report's "Static analyzer coverage" table), never silently treated as
"zero findings means the code is clean."

## Adding a new case

1. Create `tests/fixtures/evaluation/cases/<your-case-id>/repo/...` with
   a small (15-60 line), realistic, syntactically valid fixture. Put the
   bug (or, for a clean case, the "tempting but safe" pattern) inside a
   clearly named function or method — Phase 5's candidate generator
   targets symbols, not bare statements.
2. Write `case.yaml` next to it, `id` matching the directory name.
3. Validate: `python -c "from patchfrog.evaluation.fixtures import load_case, validate_case, DEFAULT_CASES_ROOT; c = load_case(DEFAULT_CASES_ROOT / '<id>'); print(validate_case(c))"` — must print `[]`.
4. Run it in isolation: `python -m patchfrog.cli eval run --case <id> --json`.
5. Run the full corpus and `eval compare` before committing, to confirm
   the new case doesn't regress anything else.

## Security review quality (post-Phase-8 refinement)

A separate refinement on top of Phase 8 (branch `feat/security-review-quality`)
extended the existing AI-finding representation with explicit security-quality
concepts, rather than building a parallel security-only reviewer stack.

**Analysis representation.** `AIReviewFinding` (`patchfrog/review/domain.py`)
already distinguished `message` (identification) and severity/confidence/
evidence; this refinement added two nullable fields carried the same way:

- `reasoning_summary` — the technical mechanism/root cause (e.g. "the
  value reaches the response text without redaction"), not a restatement
  of the category.
- `impact` — a realistic, code-grounded consequence, `None` when it
  genuinely cannot be established from the given context (never a
  fabricated filler sentence). `suggested_fix` already existed and is
  unchanged.

These are concise final-answer fields (1-3 sentences), never a
chain-of-thought transcript — enforced by the reviewer/critic system
prompts (`patchfrog/review/prompt.py`) and by deterministic validation
(`patchfrog/review/validation.py`: `message`/`reasoning_summary` must be
non-empty, or the finding is rejected as `INCOMPLETE_ANALYSIS` before it
ever reaches the critic).

**Confidence and severity.** No new confidence system — the existing
three-level `Confidence` enum (`HIGH`/`MEDIUM`/`LOW`) is reused as-is.
The reviewer prompt calibrates it explicitly (how sure the model is this
is a real bug, not how important it is) and requires conditional wording
("if callers can supply X, this allows Y — verify the caller contract")
for anything below `HIGH`, never confirmed-vulnerability language.
Severity is calibrated against exploitability/impact/reachability in
context, never inferred from category alone (`eval()` is not
automatically critical; a hardcoded path is not path traversal).

**Critic.** The critic (`patchfrog/review/critic.py`, prompt in
`prompt.py`) rejects vague identification, unsupported root cause,
exaggerated impact claims, symptom-only fixes, and generic
best-practice advice; it downgrades (rather than rejects) a real finding
whose severity/confidence overreaches. It never rejects for brevity — a
short, precise finding is the target, not a defect.

**Static findings.** `patchfrog/analysis/security_rule_metadata.py` is a
small, hand-curated, closed lookup table (keyed by `rule_id`) giving a
handful of well-understood static rules (`eval()` usage, bare `except:`,
unsafe `strcpy`/`gets`/`system()`) a `reason` and `remediation` — applied
at presentation time only (`patchfrog.publishing.queries.publishable_finding_from_static_finding`),
never at analysis time. `impact` is deliberately never set from a static
rule alone: a rule firing proves the mechanism, never real-world
reachability or attacker control.

**Comment rendering.** `patchfrog/publishing/body.py` folds
identification/reason/impact/solution into one flowing paragraph — never
a mechanical `Identification:`/`Reason:`/`Impact:`/`Solution:` heading
list — skipping any field that's empty or that would just repeat a
sentence already included. A `MEDIUM`/`LOW`-confidence finding gets one
short parenthetical qualifier next to its severity badge (e.g. "medium
confidence, verify before treating as confirmed") — never a numeric
confidence score shown to a GitHub user.

**Compatibility.** `REVIEW_PROMPT_VERSION`/`REVIEW_POLICY_VERSION`
(`patchfrog/review/config.py`) and `COMMENT_FORMAT_VERSION`
(`patchfrog/publishing/config.py`) were each bumped by one, so this
refinement's stricter prompt/critic/rendering behavior never silently
reuses a canonical review run or publication produced under the old
behavior. Finding identity for Phase 7 review memory and Phase 6
publication idempotency is unaffected: identity is keyed on evidence/
category/location, never on the prose of `reasoning_summary`/`impact`
(a wording-only change to those fields does not make an existing finding
look new). The new `impact` column on `ai_finding_proposals`/`ai_findings`
(migration `0011_security_review_quality`) is nullable, so every
pre-existing row remains readable as `impact=None`, never a fabricated
backfill.

**Evaluation.** `patchfrog/evaluation/security_quality.py` adds a second,
deterministic scoring pass — never an LLM judge — over predictions the
core matcher already confirmed as true positives: identification/
root-cause presence, actionable-fix presence (only where one is
expected), impact groundedness (checked against a curated list of
forbidden exaggerated claims per case), severity overstatement (against
`max_justified_severity`), and two explicitly-heuristic checks (generic-
advice phrases, low/medium-confidence overclaiming). 15 new `secq-*`
cases (`tests/fixtures/evaluation/cases/secq-*`) cover credential
exposure, direct and uncertain-attacker-control path traversal, an
inverted auth check, command/SQL injection, a secret in an exception
message, security-sensitive vs. correctness-only races, unsafe `strcpy`,
use-after-free, an integer-overflow allocation, a safe constant-time
comparison, and a prompt-injection attempt embedded in a source comment
— each carrying `expected_root_cause_concept`/`expected_impact_concept`/
`acceptable_remediation_direction`/`max_justified_severity`/
`forbidden_exaggerated_claims` ground truth alongside the existing
match fields. The oracle (`patchfrog/evaluation/oracle.py`) echoes this
ground truth verbatim when present, so a corpus run proves the
plumbing — schema, validation, persistence, publication — carries these
fields end-to-end; it is not evidence of real semantic quality (see "Two
kinds of AI numbers" above — the same oracle-vs-live distinction applies
here unchanged).

## What Phase 8 deliberately does not do

- **No LLM-as-judge for the canonical score.** Ever.
- **No database persistence of results.** Evaluation results are file
  artifacts (JSON/Markdown) by default — no new production DB schema.
- **No GitHub publishing during evaluation.** Every case runs against
  its own throwaway temp-directory git repo; nothing is ever pushed or
  commented anywhere real.
- **No automatic baseline updates**, ever, from any run — live-provider
  or otherwise.
