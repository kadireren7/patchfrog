# MCP Architecture

## Current implementation (audited)

`patchfrog/mcp/server.py` is a FastMCP **stdio** server with 7 tools over **PR-review data in
Postgres**: `list_findings`, `get_finding_handoff`, `start_fix_attempt`, `get_fix_attempt`,
`get_merge_readiness`, `list_repository_learnings`, `get_effective_policy`. Its strengths, which
we keep: read-mostly, no file writes, git or GitHub writes; IDs re-validated against the
repository with identical not-found shapes; bounded list sizes; `schema_version` on handoffs.
Its gaps: it needs a DB and a GitHub-shaped `repository_full_name`; errors are untyped
`{"error": "..."}` dicts; there's no progress, no cancellation and no tool versioning; and it
depends directly on persistence repositories instead of an application service.

## Target

- **One process, two tool groups.** The existing review tools stay as they are, and are
  registered only when a DB URL is configured. The new `scalability` tools are always available
  and need no DB.
- **Thin adapter.** Every new tool maps 1:1 to a `patchfrog.app` use case. The validation that
  lives in the adapter is limited to wire-schema checks.
- **Repository access boundary.** The server starts with `--root <path>` (default: CWD), or with
  the roots the client provides through MCP `roots`. All paths are resolved, symlinks included,
  and must stay inside an allowed root. Anything else returns `path_outside_root`.

## Proposed tools (v1)

Fewer, composable tools. `trace_bottleneck` and `suggest_optimization` are folded into
`get_finding`, because a separate round trip adds nothing. Names are prefixed to avoid client
collisions.

| Tool | Side effects | Input (summary) | Output (summary) |
|---|---|---|---|
| `scalability_analyze` | writes a run artifact to the cache | `root?`, `paths?[]`, `rules?[]`, `diff_base?`, `budget?{max_files,max_seconds}`, `include_non_request?` | `run_id`, `status` (`succeeded`/`partial`/…), `revision`, `coverage`, `summary_counts`, the first ≤ 20 findings (brief) |
| `scalability_get_run` | none | `run_id` | run metadata, status, budget usage, partial reasons |
| `scalability_list_findings` | none | `run_id`, `rule?`, `min_confidence?`, `cursor?`, `limit≤50` | paged brief findings |
| `scalability_get_finding` | none | `run_id`, `finding_id` | full finding: trace, evidence, assumptions, FP notes, remediation (templated, with code-pattern before/after), limitations, `verification_status` |
| `scalability_plan_verification` | writes plan | `run_id`, `finding_ids[]`, `db_url_ref?` (env var **name**, never a value) | `plan`, `plan_hash`, `requires_execution: true`, the exact commands |
| `scalability_run_verification` | **executes code** | `plan_hash`, `revision_label` (`baseline`/`candidate`) | `measurement_run_id`, status |
| `scalability_compare` | none | `baseline_run_id`, `candidate_run_id` | per-finding verdicts, statistics, comparability problems |

Design notes:

- **No `apply_fix` tool.** The coding agent edits files with its own tools, under the client's own
  approval UI. PatchFrog provides the remediation and then verifies.
- `scalability_run_verification` is refused (`execution_not_authorized`) unless user-level
  execution enablement exists (ADR-008). The client's per-call tool approval is a second,
  independent gate. The tool description says, in plain words, that it runs project code.
- Output size is bounded (≤ 64 KiB per response, with cursors for the rest).

## Contracts

- **Versioning.** Every response contains `schema_version` (semver, starting at `1.0.0`).
  Additive fields bump the minor version. Removing or renaming a field, or changing its meaning,
  bumps the major version and gets a new tool name suffix (`_v2`) for one deprecation window.
  JSON Schemas generated from the pydantic contract models are committed under
  `patchfrog/app/contracts/schemas/` and snapshot-tested.
- **Errors.** Tools return a successful result with `{"error": {"code", "message", "retryable",
  "details"}}`. Codes form a closed enum: `invalid_argument`, `path_outside_root`, `not_found`,
  `budget_exhausted`, `unsupported_project`, `execution_not_authorized`, `plan_mismatch`,
  `non_comparable`, `cancelled`, `internal`. Protocol-level errors are reserved for malformed MCP
  requests. Not-found responses never confirm that an ID exists elsewhere (as today).
- **Progress.** If the request carries a `progressToken`, the server emits `notifications/progress`
  at phase boundaries (discover, parse, graph, rules) and every K files.
- **Cancellation.** `notifications/cancelled` cancels the tool's asyncio task. The job checkpoints
  persist status `cancelled` and partial results are discarded (never reported as succeeded).
- **Long-running jobs.** Analysis runs synchronously within `budget.max_seconds` (default 60 s).
  Past the budget it returns `partial`, with what is done and an explicit reason. Verification
  runs can take minutes: the tool returns once the run *starts*, and the client polls
  `scalability_get_run`. The job keeps running in the server process. If the server restarts, an
  orphaned run is marked `failed: interrupted` on the next start. (Background jobs that survive a
  restart are a server-mode feature.)

## Transport

- **Local (now):** stdio, `patchfrog mcp serve [--root PATH]`. Client configs for Claude Code,
  Codex and Cursor are documented in Phase 6, with tested examples.
- **Remote (later, Cloud or self-hosted server):** Streamable HTTP with OAuth 2.1, as the MCP spec
  requires for HTTP transports. Repository access then comes from server-side checkouts, never
  client paths. Execution happens only in the hosted sandbox. That work belongs to Phase 8 and
  the Cloud repo, and **isn't built here**.

## Standalone distribution

`patchfrog-mcp` stays a console entry point and install profile of this package (ADR-007), not a
repository. Phase 6 produces `uvx patchfrog mcp serve` with a slim dependency set (see ROADMAP
Phase 6). Repository split criteria: ADR-007.
