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

**Clean cases are mandatory, not optional.** ~38% of the corpus (20/53
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

# Critic value measurement (runs the corpus twice: critic off, critic on):
python -m patchfrog.cli eval run --critic both

# Context Engine ablation (normal ranked context vs. target-only):
python -m patchfrog.cli eval run --context-ablation

# Phase 7 incremental-review-memory scenarios:
python -m patchfrog.cli eval run --incremental-scenarios

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
  only, both, or missed by both — measures whether the AI reviewer adds
  real value beyond what a linter already catches.
- **Efficiency**: candidates generated/reviewed/skipped, provider
  calls, input/output tokens — all normalized per true positive so
  models/prompts are comparable.
- **Incremental review memory**: scenarios passed, total provider calls
  avoided, and — most importantly — `unsafe_carry_forward_count`, which
  must always read `0`.
- **`critic_comparison`** (with `--critic both`): precision/recall/FP
  deltas between critic-off and critic-on. The critic is not assumed to
  always help — measure it every time.
- **`context_ablation`** (with `--context-ablation`): overall metrics
  under normal ranked context vs. target-only context. Note this is
  only informative under `--provider live` — the fake oracle doesn't
  actually consult context to decide what to report, so a fake-provider
  ablation run will show no difference by construction.

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

**Static toolchain note**: this development environment has `ruff`
available but not `semgrep`/`cppcheck`/`clang-tidy`. Static analysis
findings for C/C++ fixtures are therefore near-empty here — correctly
reported as a missing capability (Phase 3's own
`AnalyzerExecutionStatus.UNSUPPORTED`), never silently treated as "zero
findings means the code is clean." Every bug case in this corpus is
labeled `ground_truth_source: ai_expected` for exactly this reason.

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

## What Phase 8 deliberately does not do

- **No LLM-as-judge for the canonical score.** Ever.
- **No database persistence of results.** Evaluation results are file
  artifacts (JSON/Markdown) by default — no new production DB schema.
- **No GitHub publishing during evaluation.** Every case runs against
  its own throwaway temp-directory git repo; nothing is ever pushed or
  commented anywhere real.
- **No automatic baseline updates**, ever, from any run — live-provider
  or otherwise.
