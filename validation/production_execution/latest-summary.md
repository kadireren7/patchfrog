# Milestone S6: Production Execution Enablement -- Pre-Implementation Audit

Baseline: `main` @ `4f37ac5bdb22ffdc08b3d115a94e950eb7a8efd9` (Milestone S
Foundation, merged).

## 0. Problem restated

Milestone S's own correction round proved two things empirically: (1) real
filesystem confinement via `bwrap` genuinely works as an unprivileged user
process, and (2) the review worker's own default, non-privileged Docker
container cannot create the namespaces `bwrap` needs, under Docker's own
default seccomp/AppArmor profile -- confirmed exhaustively (no combination of
`--cap-add SYS_ADMIN`, `--security-opt seccomp=unconfined`, `--security-opt
apparmor=unconfined`, individually or together, was sufficient; see
`validation/executable_verification/latest-summary.md` section 22.5). S6's
job is to make the *hardened* sandbox actually runnable in a supported
production shape, without weakening it, and without pretending the Docker
container problem doesn't exist.

The other half of the problem, independent of the namespace question: the
review worker process that currently *would* run verification holds the
GitHub App private key, both LLM provider keys, and the database credential.
Even if the namespace problem were solved by loosening the worker
container's own security, that would still mean hostile, repository-
controlled test code executes in the same process/container as PatchFrog's
most valuable credentials. That is a real problem independent of the
namespace-creation problem, and S6 must solve both.

## 1. Repository audit (existing infrastructure)

- **`docker-compose.yml`**: `api`, `worker`, `postgres`, `redis`. `api`
  explicitly blanks `ANTHROPIC_API_KEY`/`GEMINI_API_KEY` even though
  `env_file: .env` would otherwise set them (existing least-privilege
  precedent this milestone extends, not invents). `worker` holds
  `DATABASE_URL`, `REDIS_URL`, `GITHUB_PRIVATE_KEY(_PATH)`,
  `GITHUB_WEBHOOK_SECRET` (via `.env`), `ANTHROPIC_API_KEY`/`GEMINI_API_KEY`
  (via `.env`). No `verifier` service exists yet.
- **`docker/Dockerfile`**: two targets, `api` and `worker`, sharing a `base`
  stage. `worker` additionally installs `git`, `cppcheck`, `clang-tidy`, and
  (since Milestone S) `bubblewrap`. Both run as non-root `patchfrog`.
- **`apps/worker/celery_app.py`**: `settings = get_settings()` at *module
  import time* -- meaning any process that imports this module (including
  a hypothetical new task registered here) must already have every
  `Settings` field populated, since `Settings` has no optional GitHub/DB
  fields. This is a real, structural reason a genuinely separate-credential
  verifier process needs its **own** settings class and its **own** Celery
  app instance, not a new task bolted onto `apps.worker.celery_app`.
- **`patchfrog/config/settings.py`**: one monolithic `Settings`
  (`DATABASE_URL`, `REDIS_URL`, `GITHUB_APP_ID`, `GITHUB_PRIVATE_KEY(_PATH)`,
  `GITHUB_WEBHOOK_SECRET` all required, no defaults) plus provider keys
  (optional, blank by default). Confirms the "one settings object for
  everything" pattern that a minimal-credential verifier must deliberately
  not use.
- **`patchfrog/github/auth.py`**: `InstallationTokenProvider` -- the *only*
  thing in this codebase that turns the GitHub App private key into a
  usable, short-lived (~1h, GitHub-controlled) installation access token.
  Lives entirely in the review worker's own call path
  (`apps/worker/tasks/review_pull_request.py`). Confirms: only the review
  worker ever needs the GitHub App private key; a verifier never needs it if
  it never calls the GitHub API itself.
- **`patchfrog/repository/snapshot.py`**: `RepositorySnapshotProvider`.
  `acquire(clone_url, commit_sha, token=...)` needs a token (production);
  `acquire_local(root_path=...)` needs **no credential at all** -- it wraps
  an already-materialized checkout. This is the reuse seam: a verifier can
  use `acquire_local`-shaped input (a directory already on disk) without
  ever needing a GitHub token itself, *if* something else (the review
  worker, which already holds the token) materializes that directory first.
- **`patchfrog/executable_verification/service.py`**:
  `build_executable_verification_report(local=True, root_path=...)` already
  does exactly "copy `root_path` into a disposable workspace, then sandbox
  it" with **zero** GitHub/provider/DB dependency of its own (confirmed:
  no import of `patchfrog.config.settings`, `patchfrog.github.*`, or any
  provider module anywhere in `patchfrog/executable_verification/`). This
  is the critical reuse seam for S6: the *entire* Milestone S sandbox
  implementation (`sandbox.py`, `pytest_adapter.py`, `eligibility.py`,
  `evidence.py`, `telemetry.py`, `domain.py`) needs **zero code changes**.
  S6 only needs to change *which process* calls this function, and *how*
  the `root_path` it operates on gets there.
- **`patchfrog/review/service.py`**: the exact integration seam is the
  `_verify()` closure (around line 1454), a callback passed as
  `executable_verifier` to `AgentOrchestrator.review_candidate` ->
  `_critique`, invoked at most once per candidate, only when a real
  proposal already exists. `AgentOrchestrator`, the critic, telemetry, and
  persistence never need to change at all -- `_verify()`'s own
  *implementation* is the only thing S6 touches, and its return type stays
  exactly `ExecutableVerificationReport`.
- **Two review entrypoints**: `review_local` (`local=True`, CLI/dev, an
  already-on-disk `root_path`, single machine, single trust domain -- the
  operator already has full access to their own machine) and
  `review_pull_request` (`local=False`, production Celery task, `clone_url`
  + `token`). **Only `local=False` is a real multi-container trust
  boundary problem.** CLI/local review has no separate untrusted network
  boundary to enforce; it stays exactly as it is today.
- **`patchfrog/ops/doctor.py`**: existing pattern for a comprehensive,
  secret-safe deployment diagnostic (`patchfrog ops doctor`) -- the natural
  place to add verifier-availability reporting (Part V).
- **Celery**: broker and result backend are both the same Redis instance
  (`celery_app = Celery("patchfrog", broker=settings.redis_url,
  backend=settings.redis_url)`). `celery_app.send_task(name, ...)` can
  enqueue a task **by name** without importing its implementation module --
  the review worker can dispatch to a verifier-only task without ever
  importing verifier code, and a separate Celery app instance sharing the
  same Redis result backend can fetch that task's result once complete.
  This is the existing primitive S6 reuses instead of building a new
  distributed system.

## 2. Trust-boundary inventory (Part B)

| Process | Holds | What hostile repository-controlled pytest could attack if executed there |
|---|---|---|
| 1. API process | Webhook secret, DB, Redis (never GitHub App key or provider keys -- already blanked) | N/A -- never executes repository code |
| 2. Review worker | GitHub App private key, installation tokens (short-lived), DB, Redis, both LLM provider keys | Everything: could exfiltrate the GitHub App key (impersonate the App across *every* installation), provider keys (run up API bills / exfiltrate other customers' review content via provider APIs if multi-tenant), and the full DB (every repository's/customer's data) |
| 3. Verifier execution process (proposed) | Nothing valuable by design (see Part I below) -- at most a queue-consumption credential | Its own disposable workspace and, if compromised, whatever the verifier *service* process itself can reach -- deliberately as little as possible |
| 4. Postgres | All persisted application data | Not directly reachable if the verifier never holds `DATABASE_URL` |
| 5. Redis | Celery broker/result backend, cache | A compromised verifier *service* process (not the sandboxed test) could, at most, read/write its own queue -- addressed via queue separation, Part J |
| 6. GitHub | Full App/installation-token capability if the App key leaks | Not reachable from the verifier at all under this design |
| 7. LLM providers | Cost, and potentially other tenants' data if a provider key leaks in a hosted context | Not reachable from the verifier at all |
| 8. Target repository checkout | The exact-head source under review | This *is* the blast radius; already the one true untrusted input |

**Required conclusion, confirmed**: repository-controlled code's blast
radius should be limited to (8) alone. The verifier *service* process (3)
should hold nothing more valuable than a queue-consumption credential, and
the hostile *test subprocess* inside it should hold nothing at all (already
true today via `run_sandboxed`'s existing allowlist -- `PATH`/`HOME`/
`LANG`/`LC_ALL` only, and now the sandbox-owned disposable versions of
those since Milestone S's correction).

## 3. Architecture options considered

**Option 1 -- Dedicated Verifier Worker (selected, see section 4).** A
separate Celery app/process, consuming a dedicated queue, holding only a
Redis credential, executing `build_executable_verification_report`
unchanged against a pre-staged snapshot. Minimal new infrastructure
(reuses Celery/Redis already in this stack), a real credential boundary,
straightforward to reason about and test.

**Option 2 -- Ephemeral Verification Job.** Spinning up a brand-new
container/process per verification attempt (rather than a long-running
verifier worker consuming a queue) would shrink the compromise window
further, but requires the review worker to have Docker-socket-equivalent
launch capability -- which Part I explicitly forbids giving the review
worker, and self-hosted operators do not uniformly have an ephemeral-job
launcher (Kubernetes Jobs, Nomad, etc.) available. **Rejected for v1**:
real security upside, but a materially larger and more failure-prone
self-hosted operational surface than Option 1, and Cloud (with a real job
scheduler) is a much more natural future home for this exact
shape -- flagged as an explicit future-Cloud direction (Part AC), not
built here.

**Option 3 -- Hardened Nested Sandbox Runtime.** A specialized container
runtime purpose-built for unprivileged nested namespace creation (e.g.
`sysbox-runc`) would let the verifier run namespace-creating `bwrap` calls
*inside* a normal `docker run`/compose deployment without `--privileged`.
This is architecturally the "correct" long-term Docker-native answer.
**Not empirically validated in this environment**: installing a new
low-level container runtime changes the *host's* Docker daemon
configuration, not just repository code -- genuinely out of scope for a
sandboxed coding session to install and verify against a host whose Docker
daemon configuration this session does not own. Documented as a real,
credible, narrow (no `--privileged`, no Docker socket, no global seccomp/
AppArmor disable) option an operator can adopt, but not shipped or claimed
"empirically demonstrated" here -- doing so without proof would violate
this milestone's own "no fake completion" instruction.

**Option 4 -- Bare-host verifier process (no Docker nesting at all).**
Running the verifier as a plain host process (systemd service, or a
dedicated non-containerized VM) sidesteps the nested-namespace problem
entirely, because the restriction that blocks `bwrap` is specifically
Docker's *own* container security layering (its default seccomp profile
blocks the `mount()`/`pivot_root`-adjacent syscalls `bwrap` needs even with
narrow added capabilities -- see the exhaustive Milestone S correction
testing). Outside any Docker/container nesting, `bwrap` is a completely
ordinary unprivileged Linux mechanism -- confirmed repeatedly, on this
exact dev host, throughout Milestone S's own corpus and correction testing.
**This is empirically validated in this environment** (this session's own
execution environment *is* exactly this shape: an unprivileged process, not
Docker-nested) and is the one option this milestone can honestly claim
"genuinely executes the hardened sandbox" for.

## 4. Selected architecture

**Option 1 (dedicated verifier worker/process, separate credentials, queue
handoff) as the architecture, deployable in two documented modes:**

- **Containerized (`docker-compose` `verifier` service)**: architecturally
  correct (full credential isolation, queue separation, snapshot integrity)
  but, under the *default* non-privileged container configuration, reports
  `SANDBOX_ERROR` for the same root cause already documented for the
  `worker` container in Milestone S -- **not silently worked around**. An
  operator who solves the namespace-creation problem for their own Docker
  host (Option 3, `sysbox-runc`, or an equivalent) gets a working
  containerized verifier for free, with no PatchFrog code change.
  Given as READY architecture, NOT_READY out-of-the-box execution.
- **Bare-host verifier process** (documented deployment mode, empirically
  validated end-to-end in section 5 below and in the deployment tests):
  the one mode this milestone can honestly claim is operationally
  functional today, without any additional operator infrastructure
  decision beyond "run this process outside Docker."

Both modes use the *exact same* verifier code
(`apps/verifier/`) -- there is no special-cased "containerized" vs.
"bare-host" verifier implementation; only how it's launched differs.

**Why this beats the alternatives**: it is the simplest design that
creates a *real*, empirically demonstrable security boundary today
(Option 4 as the validated deployment mode), while leaving a documented,
narrow, non-`--privileged` path (Option 3) for operators who want a fully
containerized deployment, and explicitly deferring the heavier, more
operationally demanding ephemeral-job model (Option 2) to a future Cloud
milestone where a real job scheduler is a reasonable assumption.

## 5. What S6 does NOT change

`patchfrog/executable_verification/{domain,sandbox,pytest_adapter,
eligibility,evidence,telemetry}.py` -- unchanged, verbatim. The entire
Milestone S security correction (bwrap filesystem confinement, functional
probe, resource limits, disposable HOME/tmp) is reused exactly as-is. S6 is
purely about *which process* calls `build_executable_verification_report`
and *how the repository snapshot gets there* -- never a second verification
engine, never a re-implementation of the sandbox.

## 6. Scope decision (v1)

**Implemented**: verifier protocol domain contract
(`VerificationExecutionRequest`/`Result`, `VERIFIER_PROTOCOL_VERSION = 1`),
minimal verifier-only settings, a separate Celery app/task
(`apps/verifier/`), review-worker-side snapshot staging into a shared
volume with git-native identity/content integrity verification (`rev-parse
HEAD` + `diff-index --quiet HEAD --` -- see
`patchfrog/executable_verification/snapshot_staging.py` for why a naive
tree-hash "digest" was tried first and found to be a false integrity
signal), `_verify()` rewritten to route through the
queue in production mode (`local=False`) only, result authenticity checks,
`docker-compose.yml` `verifier` service + new Dockerfile target, `ops
doctor` verifier-status reporting, a security-acceptance corpus covering
the new handoff-specific properties (request/result authenticity, snapshot
integrity mismatch, stale-head rejection, verifier crash fail-closed,
queue-credential isolation), and deployment-level tests against a real
built verifier image.

**Deferred**: Option 2 (ephemeral per-job execution) and Option 3
(`sysbox-runc`) are documented, not implemented/installed. Cryptographic
result signing (Part L explicitly allows skipping this "unless architecture
genuinely needs it" -- request/SHA/kind/target matching is sufficient for
v1, since the transport is the operator's own trusted internal Redis, not
a public network).

**`PATCHFROG_VERIFIER_ENABLED` -- audited, and added, but not for the
reason first considered.** A "disable execution" *security* toggle is
genuinely unnecessary -- `is_sandbox_available()`'s own functional probe
already gives correct fail-closed behavior with no redundant setting. But
a different, purely operational problem surfaced once the queue-based
design was worked through: without *some* signal that a verifier is
actually part of this deployment, the review worker would enqueue a
request and block for up to `verifier_wait_timeout_seconds` (default 45s)
for *every* eligible candidate in *every* review, even when no verifier
process is consuming the queue at all -- a real, silently wasted-latency
cost for the large majority of self-hosted deployments that will not run
the separate `verifier` service. `PATCHFROG_VERIFIER_ENABLED` (default
**False** -- opt-in, so upgrading to this milestone never silently adds
review latency to an existing deployment) exists solely to skip that
pointless wait; it can never expand what a verifier is allowed to do, is
operator/environment-only exactly like every other `Settings` field, and
is never read from `.patchfrog.yml`.

**CLI/local review (`review_local`, `local=True`) is unchanged** -- no
queue, no separate verifier process, exactly Milestone S's existing
in-process behavior. This is a single-trust-domain context; there is no
multi-container boundary to enforce.

## 7. A real deployment-level finding: Celery's result backend caches by task_id

Building `tests/integration/test_production_execution_corpus.py` (a real,
separate `celery -A apps.verifier.celery_app worker` subprocess, a real
Redis broker, a real producer-side Celery app instance) surfaced a genuine
behavior worth recording, found empirically rather than assumed: dispatching
with an explicit `task_id=` (this milestone's own `request_id`) and then
re-dispatching the *same* `task_id` string later returns Celery's
**previously cached result** from the result backend -- the task is never
actually re-executed at all. First seen as a confusing test failure (a
second run of the test file, reusing a hardcoded literal request id,
received back an old result whose `commit_sha` belonged to the *first*
run's differently-shaped temp checkout, correctly rejected by
`result_matches_request`'s own identity check).

This is not a bug -- it is exactly the "retry idempotency" property Part O
asked for, and it confirms `compute_request_id`'s design is correct: since
`review_run_id` is one of the hashed inputs, a genuinely new review run
(even reviewing the identical `commit_sha` again, e.g. a completely
separate PR re-review days later) always produces a genuinely different
`request_id`, so it can never hit a stale cached result -- Milestone S's
own "no cross-head caching" invariant is preserved. A **retry of the exact
same review run's exact same candidate/target** (e.g. after a transient
Celery-level failure) correctly reuses the cached result instead of
re-running the sandbox a second time, which is the intended behavior, not
an accident. The test file itself was fixed to use a fresh, unique id per
invocation (production always does, via `compute_request_id`) rather than
a fixed literal string.

## 8. Review Effectiveness Benchmark (Part AB)

No new benchmark case was added for S6. Milestone S's own
`validation/review_effectiveness/corpus/executable_verification_case_001.json`
already demonstrates exactly the distinction Part AB asks for --
structural hypothesis alone vs. hypothesis plus real execution evidence
(`expected_execution_outcome: "confirmed_failure"`). S6 changes *where*
that evidence is produced (a separate process) and *how* the snapshot
gets there, never the evidence contract itself
(`ExecutableVerificationEvidence`/`VerificationOutcome` are both
completely unchanged) -- there is no new evidence *shape* for the
benchmark to demonstrate that the existing case doesn't already cover.

## 9. Versioning re-audit (Part AF)

`VERIFIER_PROTOCOL_VERSION = 1` -- introduced
(`patchfrog.executable_verification.protocol`). A real,
independently-versioned wire contract for the
`VerificationExecutionRequest`/`VerificationExecutionResult` pair crossing
the review-worker/verifier process boundary -- this is a genuinely new
contract with no prior version to be compatible with, so `1` is correct,
not a bump of something else.

Every other version constant is **unchanged**, and each decision is
explained rather than assumed:

- `EXECUTABLE_VERIFICATION_VERSION` (1): unchanged -- the execution/report
  semantic contract this version describes (what a `VerificationOutcome`
  asserts) did not change; only *which process* calls
  `execute_against_snapshot` changed, and that function itself is
  byte-for-byte identical to Milestone S's own `local=True` code path.
- `REVIEW_PROMPT_VERSION` (13): unchanged -- no new prompt section, no
  change to the existing `<executable_verification>` block's shape.
- `TELEMETRY_SCHEMA_VERSION` (11): unchanged -- no new exported telemetry
  field; the six existing count columns on `review_runs` still mean
  exactly what they meant before, regardless of which process produced
  the evidence being counted.
- `QUALITY_COST_POLICY_VERSION` (4): unchanged -- verification still
  contributes no signal to `ReviewEffortPolicy.decide_provisional`.
- `REVIEW_ENGINE_VERSION` (3): unchanged -- the critic call shape,
  candidate validation rules, and dedup/aggregation logic are all
  untouched; `_verify()`'s own *internal* implementation changed (queue
  dispatch vs. in-process call), but its external contract (an
  `ExecutableVerificationReport` returned to `AgentOrchestrator`) is
  identical.
- `REVIEW_POLICY_VERSION` (4): unchanged, same reasoning.

## 10. Migrations (Part AG)

**No migration added.** Durable execution-request state was considered
and deliberately rejected: retry idempotency is achieved via Celery's own
`task_id`-keyed result backend (see section 7 above) plus
`compute_request_id`'s deterministic identity, which needs no database
row at all. Stale-head rejection needs no persistence either -- a new
review run's `request_id` is structurally different from an old one's
(both hash `review_run_id`), so an old result can never be looked up
under a new run's identity in the first place. Adding a table here would
have been exactly the "make the architecture look complete" anti-pattern
Part AG explicitly warns against, not a genuine requirement.

## 11. Security correction round: credential leak via staged `.git/config`

A post-merge-review security correction found that the S6 trust-boundary
claim ("the verifier never receives a GitHub credential") was **not yet
true**. `RepositorySnapshotProvider._fetch_commit` (pre-existing code,
reused unchanged by S6's own `stage_snapshot`) injects the GitHub
installation token directly into the git remote URL via `_inject_token`,
and `git remote add origin <that URL>` stores it in **plaintext** inside
`.git/config`. S6's own `stage_snapshot` staged the *entire* checkout,
`.git/` included, into the verifier-visible shared volume, and
`execute_against_snapshot`'s `shutil.copytree` copied that `.git/config`
straight into the hostile pytest execution workspace.

**Empirically reproduced, synthetic token only** (`ghs_SYNTHETIC_SENTINEL_
TOKEN_DO_NOT_USE_...`, never a real credential): a real
`RepositorySnapshotProvider.acquire()` call left the sentinel in plaintext
at `<snapshot>/.git/config`'s `[remote "origin"] url =` line; a real
sandboxed pytest test (`open(".git/config").read()`) run through
`execute_against_snapshot` **successfully read it and passed an assertion
proving the token string was present** inside the bwrap-sandboxed
workspace. This is a real, confirmed credential-exfiltration path, not a
theoretical one.

**A second, related finding** while designing the fix: `shutil.copytree`'s
default (`symlinks=False`) *dereferences* symlinks -- a malicious
repository-committed symlink pointing outside the repository tree gets
its **target's content** silently copied into the disposable workspace,
disguised as an ordinary file (`Path.is_symlink()` is `False` on the
copy). Empirically confirmed with a synthetic outside file. This affected
Milestone S's own original `local=True` CLI path too, not only S6 --
fixed everywhere `execute_against_snapshot` is used.

### Fix: Option A -- credential-free staged artifact

The review worker no longer stages the credential-bearing git checkout
into the shared volume at all. New flow:

1. `RepositorySnapshotProvider.acquire()` clones into an **isolated,
   never-shared** temp directory, exactly as before (still uses the
   GitHub token -- the review worker is still trusted and still needs
   it).
2. `git archive <commit_sha>`, extracted into a **separate, fresh
   directory under the shared staging root** -- `git archive` is a real,
   already-battle-tested git primitive that exports *only tracked,
   committed content* directly from the object database; it has no
   concept of `.git/`, remotes, or credentials at all, so there is
   nothing to leak by construction, not merely by careful exclusion.
3. A deterministic content manifest digest
   (`patchfrog.executable_verification.snapshot_staging.compute_artifact_digest`)
   is computed over the *exported artifact itself* (never git metadata) --
   relative path + entry kind (file/symlink/dir) + content hash (files) or
   raw target string (symlinks, never dereferenced), sorted for stable
   ordering.
4. The isolated, credential-bearing clone from step 1 is deleted
   immediately after the archive is extracted -- it never touches the
   shared volume and its lifetime ends before any request is even built.
5. The wire request carries an opaque `artifact_id` (a directory name
   under a configured staging root -- never a full filesystem path) plus
   the digest -- see section 13 below for why a bare path was rejected
   too (Blocker 5).

The verifier resolves `artifact_id` under its own operator-configured
`VerifierSettings.staging_root`, copies it into a **fresh disposable
workspace first**, then recomputes the digest over that disposable copy
(not the shared source) before ever running pytest -- this is also the
TOCTOU fix (section 12): the content that gets digest-checked is
*exactly* the content that gets executed, because they are the same,
already-private, already-copied bytes; nothing else can mutate them
between the check and the run. A mismatch is `SANDBOX_ERROR`, never
executed.

`shutil.copytree` calls throughout `execute_against_snapshot` now pass
`symlinks=True` -- a committed symlink is preserved *as a symlink*
(and hashed as one, by its target string) in the disposable workspace,
never dereferenced during the copy.

## 12. Correction-round completion pass: two real gaps found and closed

The correction described in section 11 above (credential-free artifact,
content-manifest digest, TOCTOU closure, protocol-version enforcement,
strict wire parsing, staging-root path trust) had already been
implemented when this pass started. Auditing it end to end against the
full security-correction spec (rather than trusting the prior summary
alone) found two remaining, real gaps -- both fixed here, not merely
documented:

**Gap 1 -- a real test-assertion bug, caught by actually running the
corpus.** `test_real_distributed_round_trip_rejects_protocol_version_zero`
asserted `result is not None`, but `apps/verifier/tasks.py::_error_result`
always echoes back the verifier's own current `VERIFIER_PROTOCOL_VERSION`
in its rejection reply, never the mismatched request's `protocol_version`
-- so `protocol.result_matches_request`'s own identity check (which pins
`result.protocol_version == request.protocol_version` *and*
`== VERIFIER_PROTOCOL_VERSION`) correctly never trusts that reply, and
`VerifierDispatcher.dispatch` correctly returns `None`. Running the test
for real reproduced exactly this: `assert None is not None` failed. This
is not a security bug -- both branches (a same-shaped `SANDBOX_ERROR`
result, or a dispatcher-level `None`) guarantee the one property that
actually matters, that the hostile test target is never executed -- it is
a test-assertion bug that hadn't yet been run to confirm. Fixed by
loosening the assertion to match the adjacent
`test_real_distributed_round_trip_rejects_future_protocol_version` test's
own already-correct tolerant assertion (`result is None or
result.outcome is SANDBOX_ERROR`).

**Gap 2 -- `docker-compose.yml`'s `verifier` service never set
`VERIFIER_STAGING_ROOT`.** `VerifierSettings.staging_root` is a required
field with no default (deliberately -- an operator must explicitly decide
the one trusted staging root, never inferred). `apps/verifier/
celery_app.py` calls `get_verifier_settings()` at *module import time*,
so the container as shipped in `docker-compose.yml` would have crashed
immediately on startup (`pydantic.ValidationError: VERIFIER_STAGING_ROOT
Field required`) the moment an operator uncommented the verifier-enablement
block and brought the stack up -- confirmed by actually building the
`verifier` image and running it with only `REDIS_URL` set, which fails
exactly as predicted, then re-running with `VERIFIER_STAGING_ROOT` also
set, which starts cleanly and registers exactly one task
(`patchfrog.verify_candidate`) on exactly one queue
(`patchfrog-verification`). Fixed by adding `VERIFIER_STAGING_ROOT:
/var/lib/patchfrog/verification-snapshots` to the `verifier` service's
`environment:` block in `docker-compose.yml`, matching the shared volume
path already mounted there (and already documented, but not wired, for
the worker side's own `VERIFICATION_SNAPSHOT_ROOT`). `docs/deployment.md`
and `docs/executable-verification.md` are updated to describe the
corrected credential-free/content-digest/staging-root-trust/TOCTOU/
protocol-version architecture in detail -- both previously still
described the pre-correction ("stages the snapshot... still using the
GitHub token", "git rev-parse HEAD" integrity) design.

Both gaps were found by actually executing the corpus and actually
building and running the verifier container -- not by re-reading the
implementation and assuming it matched its own docstrings.

### Fresh full-gate results after this pass

- `ruff check .`: clean (two findings in the corrected test file --
  an unused `shutil` import and an unsorted import block -- fixed).
- `mypy . --strict`: clean, 579 source files.
- `pytest tests/unit`: 1486 passed.
- `pytest tests/integration`: 619 passed (includes the 16-case
  `test_production_execution_corpus.py` security corpus).
- `alembic heads`: single head (`0028_executable_verification`),
  unaffected by this correction (no migration).
- Docker: all three images (`api`, `worker`, `verifier`) build clean;
  the `verifier` image was run standalone against the real Redis
  container with `VERIFIER_STAGING_ROOT` set and reached `celery@...
  ready.`, registering exactly `patchfrog.verify_candidate` on exactly
  `patchfrog-verification`.
- Tracked-file secret scan (git diff of every file this correction
  touched) for GitHub-token-shaped/PEM-shaped/`api_key=`-shaped strings:
  no matches other than the documented, intentional
  `ghs_SYNTHETIC_SENTINEL_TOKEN_DO_NOT_USE_...` test fixture. This proves
  only tracked-file diff content -- it is not a claim about terminal
  scrollback, CI logs, or any other local/UI display surface.
- No live Anthropic/Gemini/OpenAI call made at any point in this pass.
