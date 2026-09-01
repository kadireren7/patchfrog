# Production Webhook E2E Validation

Real, live validation of PatchFrog's production webhook → review →
publish → feedback → telemetry chain (Milestone H), run against
PatchFrog's own repository (`kadireren7/patchfrog`) via a temporary
dogfood PR (#38, closed unmerged, branch deleted after capture).

See `latest-summary.md` for the full narrative, `telemetry/` for the
three real review runs' privacy-safe telemetry exports (no source,
context, prompts, or secrets -- see
`docs/telemetry-intelligence.md`'s privacy guarantees), and
`docs/production-e2e.md` for the operational/architectural writeup this
run validated.

**REAL vs. SYNTHETIC, never mixed**: every fact in `latest-summary.md`
is explicitly labeled. REAL means an actual GitHub webhook delivery, an
actual Gemini API call, or an actual GitHub API read/write happened.
SYNTHETIC means a locally-constructed request against the real running
process (still exercising real code, real signature verification, real
idempotency logic) without a corresponding real GitHub-side event.

No credentials, installation tokens, webhook signatures, or raw
prompt/context/source content appear anywhere in this directory.
