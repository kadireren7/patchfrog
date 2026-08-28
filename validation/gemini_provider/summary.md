# Gemini Provider Integration — Validation Summary

Branch `feat/gemini-provider`, baseline `main` @ `f31f445b78fd0b21fc502ed6e478e8662a14bf4c`.

Google Gemini is now a first-class, second `LLMProvider` implementation alongside
Anthropic (unchanged, still the production default). This document reports what
was actually validated live, and stops precisely where the free-tier daily quota
did.

## 1. Model choice: `gemini-2.5-flash` → `gemini-3.6-flash`

The task specified `gemini-2.5-flash`. **Live verification showed it returns a
`404 NOT_FOUND`** -- Google's own error body says it is "no longer available to
new users" and explicitly recommends `gemini-3.6-flash`. Confirmed the
replacement works live before proceeding (user approved this substitution
explicitly). `gemini-3.6-flash` is the configured default throughout this
integration.

## 2. Provider implementation

`patchfrog/review/providers/gemini_provider.py` -- `GeminiLLMProvider`,
implementing the exact same `LLMProvider` protocol as
`AnthropicLLMProvider` (`generate_structured`, `identity`). Uses Google's
official `google-genai` SDK (v2.20.0 at implementation time), async client.
Structured output via `response_mime_type="application/json"` +
`response_json_schema` -- PatchFrog's existing `REVIEW_RESPONSE_SCHEMA`/
`CRITIC_RESPONSE_SCHEMA` passed through **unmodified**; every JSON Schema
keyword these schemas use is within Gemini's documented supported subset, so
no schema fork was needed.

## 3. A real bug found and fixed during this provider's own validation

**Gemini's thinking tokens are drawn from the same `max_output_tokens` budget
as the visible JSON answer**, and its default thinking budget is unbounded
("AUTOMATIC"). Live testing reproduced this twice: the model spent nearly the
entire token budget thinking, truncating the JSON mid-object
(`json.loads` failed: "Expecting value: line 1 column 276"). Root-caused via
direct usage-metadata inspection (`thoughts_token_count` vs `candidates_token_count`
against the requested `max_output_tokens`), then fixed at the provider
boundary (not the schema): `thinking_budget` is now explicitly capped to
guarantee at least 1024 tokens of headroom for the answer
(`_MIN_RESERVED_OUTPUT_TOKENS`), never disabled entirely. Verified fixed with
2 consecutive successful live calls afterward. Two regression tests added
asserting the request payload's `thinkingConfig.thinking_budget` is correctly
capped (and never negative for a small `max_output_tokens`).

## 4. Provider factory

`anthropic` → `AnthropicLLMProvider`, `gemini` → `GeminiLLMProvider`, any
other string → a clear `ValueError` naming both supported providers. Missing
`GEMINI_API_KEY` when `provider=gemini` raises the existing
`MissingProviderCredentialsError` -- verified it never falls back to
Anthropic or `FakeLLMProvider` even when an Anthropic key happens to be
present (`test_missing_gemini_credential_never_falls_back_to_anthropic`).

## 5. Config

No second config system: `.patchfrog.yml`'s existing `review:` section
(`ReviewConfig.provider`/`.model`, already a free-form string) is the only
mechanism. Minimal config to select Gemini, **as of the config-semantics
fix landed later in this same PR** (see §16):

```yaml
review:
  provider: gemini
  model: gemini-3.6-flash
```

`ReviewConfig` now fills in provider-coherent effective values for any
omitted field -- `critic_model` defaults to the same value as `model`
(not the Anthropic default), and `request_timeout_seconds` defaults to
120s specifically for `provider: gemini` (30s elsewhere, unchanged).
Both remain overridable. **This is a fix, not the original behavior**:
§9's quality sample below genuinely ran *before* this fix existed, with
a `.patchfrog.yml` that only set `provider`/`model` -- see §9 for exactly
what that produced and why. Anthropic remains the production default;
nothing changes for existing deployments unless `.patchfrog.yml`
explicitly opts into `provider: gemini`.

## 6. Docker wiring

Mirrors `ANTHROPIC_API_KEY` exactly: `worker` receives `GEMINI_API_KEY` via
`env_file: .env` (no explicit passthrough needed); `api` explicitly blanks
it (`GEMINI_API_KEY: ""`) since the API process never instantiates a
provider. No key in the Dockerfile, image layers, `docker history`, or
tracked compose source -- verified via `docker compose config` (presence/
length checks only; see the redaction incident in §15).

## 7. Redaction

The existing `*apikey`-suffix redaction (added for `anthropic_api_key` in an
earlier session) already covers `gemini_api_key`/`GEMINI_API_KEY`/
`google_api_key`/`GOOGLE_API_KEY` with **no code change needed** -- verified
and covered by a new regression test. No value-shape pattern was added for
Gemini keys: the real key in this environment doesn't match the well-known
`AIzaSy...` Google API key convention, so there was no *stable, verified*
shape to match safely (the task's own guidance: only add one "if there is a
stable safe pattern worth matching"). Token-usage telemetry fields
(`reviewer_input_tokens` etc.) remain visible, confirmed by the same
regression test suite as the Anthropic redaction fix.

## 8. Live smoke test — PASSED (after the model swap and thinking-budget fix)

Using `provider_factory.build_reviewer_provider` + the real
`build_reviewer_prompt`/`REVIEW_RESPONSE_SCHEMA` against a synthetic
plaintext-password-logging bug: auth succeeded, schema validated, 1 correct
`HIGH`/`security` finding with grounded identification/root cause/impact/fix,
indistinguishable in quality from the equivalent Anthropic smoke test result.

## 9. Live quality sample — **PARTIAL** (4 of 9 planned cases; daily quota exhausted)

Planned 9 cases (Python bug/clean, C bug/clean, C++ bug/clean, security,
multi-file, extern case11). **The free-tier key's daily quota
(`GenerateRequestsPerDayPerProjectPerModel-FreeTier`, `quotaValue: 20`,
confirmed via the live `429 RESOURCE_EXHAUSTED` error body) was exhausted
partway through case 3 of 8** (case11 was planned as a separate step
afterward and never reached). The batch was stopped immediately once this
was confirmed -- per the session's cost-guard rules, no further calls were
attempted once quota exhaustion was confirmed persistent, and no calls were
retried against it.

**Important caveat discovered during this same batch**: the `.patchfrog.yml`
used only set `provider`/`model`, not `critic_model` -- every critic call
therefore requested Gemini's API for a nonexistent `claude-opus-5` model
(the Anthropic default), failing with a clean `404` that the existing review
service correctly classified as fatal and gracefully degraded from (no
crash, no silent accept). **This sample remains reviewer-only Gemini
data, not reviewer+critic -- this fact is not rewritten.** The
underlying config-semantics gap this run exposed has since been fixed at
the `ReviewConfig` normalization layer in this same PR (§5, §16); a
corrected re-run for real critic-path data is still needed once quota
resets (§17) -- this historical sample was not, and will not be,
retroactively relabeled as having critic data it didn't have.

| Case | Type | Result |
|---|---|---|
| `py-off-by-one-loop` | Python bug | **TP** -- found the exact target bug (correctness, `src/batch.py:4`) |
| `clean-py-defensive-check` | Python clean | Clean pass -- 0 findings across all 5 candidates |
| `c-memory-leak` | C bug | **TP** -- found the exact target bug (`buffer.c:13`, 2 of 4 candidates quota-blocked) |
| `clean-c-correct-realloc-pattern` | C clean | Clean pass on partial coverage -- 0 findings in the 2 of 4 candidates reviewed before quota exhaustion |

**Not attempted** (quota exhausted before reaching them): C++ bug
(`cpp-missing-lock-race`, indexed only), C++ clean, security case, multi-file
case, and the extern case11 re-check.

**Summary metrics for what was genuinely reviewed**: target bugs found 2/2,
missed 0, hallucinated/unsupported findings 0, real-but-out-of-scope
findings 0, duplicates 0.

## 10. Extern context case (case11) — **NOT RUN** (quota exhausted first)

Planned as a direct comparison against the Anthropic result from the prior
session (`validation/live_provider/context_ablation.json`: expected finding
missed there too, root-caused to the Context Engine's 1-hop-only call-graph
limitation, not a model-reasoning defect). Could not be attempted with
Gemini before quota exhaustion. **This limitation is a repository/
Context-Engine property, not provider-specific** -- when this case is
re-run with Gemini (once quota resets), the same missing evidence
(`retry_policy.h`'s macro two hops away) will apply regardless of which
model reviews it; the comparison that's actually missing is whether
Gemini's *reasoning given the same incomplete context* differs from
Anthropic's (§8 of the Anthropic validation), which remains open.

## 11. Natural PR dry-run — **NOT RUN** (quota exhausted first)

Not attempted; same blocking cause as §9-10.

## 12. Failure safety / error classification

Fully covered by unit tests against real observed error shapes (401/403
auth, 429 rate-limit-or-quota, 400 invalid-request, 404 unknown-model, 5xx
server error, timeout, connection error, safety-refusal finish reasons) --
`tests/unit/test_review_gemini_provider_contract.py`, 24 tests, all mocked
at the HTTP transport level via `respx` (no real network calls in normal
`pytest`). The quota-exhaustion classification (`ProviderTransientError` for
429) was validated against a **real** 429 response body live during §9 --
confirmed the adapter's classification matches reality exactly.

## 13. Tests added

- `tests/unit/test_review_gemini_provider_contract.py` (24 tests): every
  response shape, the thinking-budget fix, no-secret-in-request-body.
- `tests/unit/test_provider_factory.py` (7 tests): anthropic/gemini routing,
  missing-credential errors, no silent fallback, unknown-provider error.
- `tests/unit/test_logging_redaction.py` (+1 test): Gemini/Google key-name
  redaction.
- `tests/unit/test_settings.py` (+1 test): `Settings.__repr__` never leaks
  either provider's real key value.

## 14. Readiness

`READY_FOR_INTERNAL_DOGFOOD_ONLY` -- the provider itself is implemented
correctly, contract-tested, and proven live (auth, structured output, error
classification, a real bug found and fixed). It is **not** ready for a
broader quality verdict: the live quality sample is 4/9 planned cases, has
no critic-path data, and the free tier's daily quota (20 requests) makes it
unsuitable for anything beyond very light internal dogfood without a paid
tier. See §17 for the exact remaining steps.

## 15. Incident: a real secret was printed mid-session

While verifying `docker compose config`'s resolved environment for the
`api`/`worker` services, a check meant to confirm `api` correctly receives
an empty `GEMINI_API_KEY` also printed `worker`'s **real, resolved key
value** into tool output (this session's transcript). Disclosed to the user
immediately; the user rotated the key and confirmed before work continued.
Corrected going forward to check presence/length only, never print a
resolved value. See the corresponding feedback/memory entry for the lesson
(does not affect anything committed to the repository -- the exposure was
transcript-only, not a commit, log file, or artifact).

## 16. Config-semantics fix (post-CI-review, same PR)

§9's live run exposed a real config-normalization gap: `ReviewConfig`'s
`critic_model` defaulted to the class-level Anthropic default
(`claude-opus-5`) regardless of `provider`, so a `.patchfrog.yml` setting
only `provider`/`model` produced an impossible provider/model pair for
the critic call. Fixed at the config-normalization boundary (not in
`provider_factory`, per the reviewer's explicit preference for that
boundary): `ReviewConfig` gained a `model_validator(mode="after")` that
fills in `critic_model` (defaults to the same value as `model`) and
`request_timeout_seconds` (120s specifically for `provider: gemini`, 30s
elsewhere unchanged) **only for fields the caller genuinely omitted** --
distinguished via pydantic's `model_fields_set`, never by comparing
against the old default string (a user may deliberately choose
`claude-opus-5` explicitly, and that must still be respected).
`CONFIG_SCHEMA_VERSION` bumped `1` → `2` since two YAML configs that look
identical to the older schema version can now produce a materially
different effective config -- any prior canonical run is never silently
reused across this boundary. 10 new regression tests (`tests/unit/test_review_config.py`)
plus 1 in `test_provider_factory.py` cover: omitted-vs-explicit for both
fields, Anthropic behavior provably unchanged, the minimal Gemini YAML
normalizing correctly end-to-end, and the fingerprint distinguishing the
old broken default from the new correct one. No live call was needed or
made to verify this fix -- unit/integration tests are sufficient, and the
free-tier daily quota from §9 had not reset.

## 17. Remaining limitations / next steps

1. **Free-tier daily quota (20 requests/day)** is the primary blocker to
   completing the quality sample, the extern case11 comparison, the natural
   PR dry-run, and any critic-enabled data. Needs either a wait for daily
   reset or a paid tier.
2. A fresh live run (now needing no config workaround at all, per §16) to
   get real critic-path data -- the minimal documented config is now
   sufficient by itself.
3. The remaining 5 of 9 planned quality-sample cases (C++ bug/clean,
   security, multi-file, extern case11).
4. Natural PR dry-run (0 of the planned 1-2 attempted).
5. A direct Gemini-vs-Anthropic comparison on identical cases once both
   have complete data.

## PR

`feat/gemini-provider` → `main`. **Not merged** -- explicit confirmation in
the final report.
