# Executable Verification Foundation

`patchfrog/executable_verification/` is the first Intelligence-lineage
package that is not a pure structural-evidence layer -- it genuinely
executes repository code, inside a hardened sandbox, to check whether a
reviewer's own hypothesis holds up against a real, already-existing
test.

**Product principle**: move from "the code evidence strongly suggests
this bug" toward "where safely possible, PatchFrog executed a bounded,
targeted verification and observed evidence supporting or contradicting
the hypothesis." This is explicitly **not**: arbitrary code execution, a
CI replacement, an unrestricted test runner, a shell agent, or an
autonomous code-modification system.

See `validation/executable_verification/latest-summary.md` for the full
pre-implementation audit, threat model, and scope decision behind every
constant and design choice below.

## What v1 does

Exactly one verification kind is implemented:
**`EXISTING_TARGETED_TEST`**. For a candidate that already has a real
specialist-proposed finding, PatchFrog checks whether a real,
already-indexed test file exists for the changed file
(`patchfrog.test_intelligence.expectations.derive_test_surfaces`, reused
directly -- never a filename-similarity guess). If one exists, PatchFrog
runs *only that one test file* -- never the full suite -- inside an
isolated sandbox, against the exact PR head, and folds the result into a
bounded, neutral-wording evidence section the **critic** (never the
original specialist) sees.

`STATIC_REPRODUCTION` and `GENERATED_TARGETED_TEST` are defined on
`VerificationKind` for forward documentation only and are not
constructed by anything in v1 -- see latest-summary.md section 5 for
why: a generated test's own correctness is an unverified variable, and
validating that correctly is a materially larger project than this
foundation.

## Execution order

Verification runs **after** a real specialist proposal exists for a
candidate, and **before** critic verification -- integrated at the
existing critic-decision point in
`patchfrog.review.orchestration.AgentOrchestrator._critique` (right
after the existing "no candidates to critique, return early" check). It
reuses the existing critic-call mechanism entirely; there is no second
critic path. **No candidate, no execution**: a docs-only PR, a candidate
that never gets a real proposal, or a candidate with no eligible test
target never triggers a sandbox at all.

## Sandbox

`patchfrog.executable_verification.sandbox.VerificationSandbox` wraps
the existing, unmodified
`patchfrog.analysis.subprocess_sandbox.run_sandboxed` with a fixed
isolation prefix built on `bwrap` (bubblewrap) -- the same purpose-built,
widely-audited unprivileged-sandboxing primitive Flatpak uses, not
hand-rolled `pivot_root`/mount-namespace logic:

```
bwrap --unshare-all --die-with-parent --new-session --clearenv
  --setenv HOME /tmp/home --setenv TMPDIR /tmp ...
  --ro-bind /usr /usr --symlink usr/bin /bin ...
  --tmpfs /tmp --dir /tmp/home
  --bind <disposable workspace> <disposable workspace>
  --proc /proc --dev /dev --chdir <disposable workspace> --
    prlimit --nproc=<N> --as=<BYTES> --cpu=<SECONDS> --
      <the real, already-built argv>
```

A fresh mount namespace exposes exactly: `/usr` read-only (the entire
Python/pytest runtime -- the worker image's own system Python, with
pytest installed as a base dependency, lives under `/usr/local`, nested
inside `/usr`), the usr-merge symlinks recreated (`/bin`, `/lib`, etc.),
a local/CLI dev environment's own venv root read-only when the running
interpreter lives outside `/usr` (a no-op in production), the disposable
verification workspace read-write, and a private `tmpfs` at `/tmp`
hosting a throwaway sandbox-owned `HOME` -- never the real worker home,
and requiring no host-side directory a caller must create or clean up.
No other host path is bound. Network/PID isolation are unchanged from
this module's first version.

**Empirically verified, including a real filesystem escape found and
fixed** (not merely asserted -- see latest-summary.md sections 3 and 22
for the full transcript): the exact same malicious pytest target that
previously read the real `$HOME`, an arbitrary file outside the
disposable workspace (directly and via a planted symlink), and the real
Docker socket now sees none of them; `/proc/self/environ` shows only the
sandbox's own minimal environment; network (including loopback) remains
unreachable; `prlimit` resource limits still genuinely enforce when
applied *inside* the `bwrap` sandbox (a memory bomb past `--as` raises
`MemoryError`, a fork bomb past `--nproc` raises `OSError`) -- applying
`prlimit` *outside* `bwrap` was tried first and found to break `bwrap`'s
own setup, since `RLIMIT_NPROC` counts against the calling real UID's
entire host process count until a fresh user namespace scopes it.

`is_sandbox_available()` does not stop at checking whether
`bwrap`/`prlimit` are discoverable on `PATH` -- binary presence alone is
not sufficient evidence the sandbox actually works. It runs a real,
side-effect-free probe exercising the same categories of operation real
execution depends on (fresh mount namespace, read-only bind, private
`tmpfs`, fresh `/proc`/`/dev`) via the same cached helper the real
sandbox uses, so the probe can never drift from what execution actually
requires. When the probe fails, verification fails closed to
`SANDBOX_ERROR` before any pytest command is ever attempted -- never an
unisolated fallback, and never misclassified as `UNSUPPORTED` (which
means something different: the target repository's own test collection
failed). Two independent, empirically confirmed hosts fail this probe
today: GitHub Actions' `ubuntu-latest` runner, and PatchFrog's own
default, non-privileged worker Docker container -- see "Production
readiness" below.

## Architecture (Milestone S6: Production Execution Enablement)

The review worker holds PatchFrog's most valuable credentials -- the
GitHub App private key, both LLM provider keys, the database connection.
Milestone S (above) already hardened *how* a test executes; Milestone S6
addresses *where*: hostile, repository-controlled test code must never
run in the same process/container as those credentials, full stop. See
`validation/production_execution/latest-summary.md` for the complete
trust-boundary audit.

The review worker and a separate, deliberately credential-minimal
**verifier** process (`apps/verifier/`, its own Celery app) split the
work:

```
Review worker (trusted plane)              Verifier (execution plane)
  - GitHub App key, provider keys,
    DB -- never a hostile test          -- NO provider key
  - determines eligibility (pure,       -- NO GitHub App key/token
    no I/O)                             -- NO DATABASE_URL
  - exports a credential-free,          -- only a Redis credential
    exact-head artifact (git archive,      (its own dedicated queue)
    no .git/, no remote, no token) --
    once per review run, into a         -- resolves the request's opaque
    shared staging volume                  artifact_id strictly beneath
  - dispatches a bounded                   its own configured staging
    VerificationExecutionRequest             root (rejects traversal/
    (opaque artifact_id + content            symlink escape)
    digest, never a filesystem path)     -- copies it into a fresh
    by name onto the verifier's              disposable workspace, then
    own queue                                recomputes the content
  - awaits a bounded                         digest over THAT copy
    VerificationExecutionResult          -- runs the *exact same*,
    (never falls back to running             completely unmodified
    the test itself on any failure)          bwrap sandbox Milestone S
                                              already built
```

**Security correction (post-S6 review round).** The first version of
this design staged the *entire git checkout* -- `.git/` included -- into
the shared volume. `RepositorySnapshotProvider` stores the GitHub
installation token directly in the git remote URL, which git writes in
plaintext to `.git/config`; a real, synthetic-token reproduction proved a
sandboxed hostile pytest test could read that file and recover it (see
`validation/production_execution/latest-summary.md` section 11 for the
full empirical transcript). Network denial does not mitigate this kind of
leak -- a credential read from disk and printed to bounded stdout still
crosses back into the trusted review worker as "evidence." The fix,
described in the diagram above and implemented in
`patchfrog.executable_verification.snapshot_staging.export_artifact`, is
that the worker never stages anything with `.git/` in it at all: `git
archive` -- a real git primitive with no concept of remotes or
credentials -- exports only the tracked, committed content at the exact
commit, directly from the object database. There is nothing to leak by
construction, not merely by careful exclusion. The credential-bearing
clone the worker still needs internally (to reach the commit in the first
place) lives in an isolated, never-shared temp directory and is deleted
before any verifier-visible artifact or request exists.

The verifier's own task
(`apps/verifier/tasks.py::verify_candidate_task`) calls
`patchfrog.executable_verification.service.execute_against_snapshot` --
the identical "copy into a disposable workspace, then sandbox it"
function Milestone S's own CLI/local path already uses. Nothing about the
sandbox mechanism itself changed for S6; only which process invokes it,
and how the repository content gets there without a GitHub credential.
That same correction also fixed `shutil.copytree`'s default
(`symlinks=False`) *dereferencing* symlinks during this copy -- a
committed symlink pointing outside the repository tree previously had its
**target's content** silently copied in under the symlink's own name.
Every `copytree` call in this path now passes `symlinks=True`: a symlink
is preserved and hashed as a symlink, never dereferenced.

**Content-manifest integrity, not git-metadata integrity, and no
signing.** The earlier design tried `git rev-parse HEAD` (identity) plus
`git diff-index --quiet HEAD --` (tracked content unchanged) -- a false
integrity signal, because it only proves *tracked* content matches; it
says nothing about an injected *untracked* file, which can still
influence pytest/import behavior. The corrected design instead computes a
deterministic content-manifest digest
(`snapshot_staging.compute_artifact_digest`) directly over the exported
artifact's actual bytes -- relative path, entry kind (file/symlink/
directory), and content hash (files) or raw link target (symlinks, never
dereferenced) -- which represents every byte the sandbox could possibly
see, tracked or not, by construction (there is no untracked-vs-tracked
distinction left once `.git/` itself is gone). **This is also the TOCTOU
fix**: the verifier does not check one tree and then execute a separately
mutable one. It first copies the shared artifact into its own fresh,
private, disposable workspace, and *only then* recomputes the digest --
over that already-private copy, not the shared source -- immediately
before running anything. The bytes that get checked are, by construction,
the exact bytes about to execute; nothing else can mutate them in
between. A digest mismatch (whether from real tampering or a corrupted
shared volume) is `SANDBOX_ERROR`, never executed. No cryptographic
signing on top of this (the transport is the operator's own internal
Redis, not a public network) -- integrity is content-addressed, not
merely asserted.

**Staging-root path trust.** A request's `artifact_id` is never trusted
as a filesystem path. It must be a bare directory name (no path
separators, no `.`/`..`) directly under the verifier's own
operator-configured `VERIFIER_STAGING_ROOT` (`VerifierSettings.
staging_root`); the *canonically resolved* result must still be beneath
that root, or the request is rejected (`SANDBOX_ERROR`) before anything
is read. This closes off both `../` traversal and a symlink planted
inside the staging root pointing outside it. The root itself is a
required, no-default operator setting -- never inferred from a request,
and never repository-controlled.

**Protocol version enforcement, not just carrying.** Every request and
result carries `protocol_version`. The verifier rejects (before any
execution) a request whose version does not exactly equal
`VERIFIER_PROTOCOL_VERSION`; the review worker independently rejects a
result whose `protocol_version` doesn't match what it asked for, as part
of the same identity check described next. No range tolerance for this
still-unreleased v1 contract -- a mismatch is always treated as a bug or
an attack, never smoothed over.

**Strict wire parsing.** `VerificationExecutionRequest.from_wire`/
`VerificationExecutionResult.from_wire` (`patchfrog.executable_
verification.protocol`) validate each field's actual decoded JSON type
before use -- a stray `"false"` string is never silently coerced to the
bool `True` (`bool("false") == True` is exactly the class of trap this
refuses to let through), a bool is never accepted where an int is
expected, and every numeric/string field is bounds-checked. Any violation
raises and is always treated as a fail-closed rejection by the caller,
never a best-effort partial parse.

**Result authenticity**: the review worker checks the returned result's
`request_id`/`protocol_version`/`commit_sha`/`verification_kind`/
`test_target_path` all match what it actually asked for
(`protocol.result_matches_request`) before trusting it -- no
cryptographic signing (the transport is the operator's own internal
Redis, not a public network), but never a blind accept either.

**Off by default** (`PATCHFROG_VERIFIER_ENABLED=false`): the review worker
never attempts production verification at all until an operator
explicitly opts in -- see `docs/deployment.md`'s "Executable Verification
production deployment" section for exactly what that requires. **CLI/
local review is entirely unaffected** -- no queue, no separate process,
exactly Milestone S's original in-process behavior; there is no
multi-container trust boundary to enforce on a single developer's own
machine.

## Production readiness

**Executable Verification's real execution capability is currently
`SANDBOX_ERROR` (unavailable) under PatchFrog's default self-hosted
Docker deployment, and this is by design, not a bug.** Confirmed by
running the real capability probe inside an actual built `verifier` image
container (Milestone S6), launched exactly as it ships (non-root, no
`--privileged`, no added capabilities) -- the identical result already
found for the `worker` image in Milestone S: `bwrap`/`prlimit` are both
present, but the underlying mount-namespace operations they depend on
fail under Docker's own default seccomp/AppArmor profile for a
non-privileged container. The same restriction category blocks it on
GitHub Actions' `ubuntu-latest` runner.

Making real execution available requires an explicit, operator-made
infrastructure decision -- this repository does not make one silently.
Systematic testing found no combination of `--cap-add SYS_ADMIN`,
`--security-opt seccomp=unconfined`, and `--security-opt
apparmor=unconfined` (individually or together) sufficient in a default
`docker run`. Reaching a working *containerized* configuration needs
either granting the verifier container broader capabilities the current
Dockerfile deliberately does not request, or adopting a container runtime
purpose-built for nested unprivileged sandboxing (e.g. `sysbox-runc`,
documented as a credible option but not installed or empirically
validated by this milestone -- doing so changes the *host's* own Docker
daemon configuration, out of scope for this repository to make for an
operator). `bwrap` itself is installed in the verifier image
(`docker/Dockerfile`) precisely so the capability probe is correct the
moment an operator's own deployment grants it -- installing the binary
carries no security cost on its own, since the functional probe (not
binary presence) gates whether it can ever be invoked.

**The one deployment shape this milestone validated end to end**: running
the verifier as a bare, non-containerized host process (a systemd
service, or a dedicated VM) -- outside any Docker nesting, `bwrap` is an
ordinary unprivileged Linux mechanism, confirmed repeatedly throughout
this project's own testing on exactly such a host.

Until real execution is available, every eligible candidate's
verification attempt reports `SANDBOX_ERROR`, folded into the existing
count-only telemetry -- never a crash, never a silently degraded
alternate execution path. Domain model, eligibility, result model,
telemetry, and the Review Effectiveness Benchmark foundation are all
fully functional regardless of sandbox availability.

## No dependency installation, ever

A target repository's own test dependencies are **never installed**. A
mandatory `--collect-only` dry run always precedes the real test run; a
`ModuleNotFoundError` or any other collection failure there classifies
as `UNSUPPORTED` (the real test was never attempted), never a guessed
`CONFIRMED_FAILURE` and never a crash.

## Result classification

Empirically grounded against pytest's own documented exit codes:

| Signal | Outcome |
|---|---|
| Collection succeeds (exit 0), then all tests pass (exit 0) | `PASSED` |
| Collection succeeds, then at least one test fails (exit 1) | `CONFIRMED_FAILURE` |
| Collection itself fails (exit 2/3/4/5) | `UNSUPPORTED` |
| `run_sandboxed`'s own `timed_out` flag fires, at either stage | `TIMEOUT` |
| Sandbox unavailable, or a `GitError`/`OSError`/`ValueError` during acquisition | `SANDBOX_ERROR` |
| Any other unexpected exit code after successful collection | `INCONCLUSIVE` |

A failing command is never automatically a confirmed bug on its own --
the critic is explicitly instructed to verify the failure (or pass)
actually exercises the proposed issue before weighing it.

## Bounds

All fixed Python constants in `domain.py` -- deliberately **not**
`.patchfrog.yml`-controlled or environment-variable-controlled. There is
no repository-config surface for verification at all in v1, not even a
disable flag (a `.patchfrog.yml` is read from the PR's own untrusted
head, so even a "disable" knob could create a confusing race; see
latest-summary.md section 10).

- `MAX_VERIFICATIONS_PER_REVIEW = 5` -- shared, review-run-wide budget.
- `MAX_VERIFICATION_SECONDS = 30.0` -- per-attempt wall-clock ceiling.
- `MAX_TOTAL_VERIFICATION_SECONDS = 90.0` -- whole-review-run ceiling,
  checked before every new attempt.
- `MAX_STDOUT_EXCERPT_BYTES` / `MAX_STDERR_EXCERPT_BYTES = 8192` each --
  far below `run_sandboxed`'s own 5 MiB streaming cap; verification
  evidence is a short excerpt for the critic, never a log dump.
- `MAX_SANDBOX_PROCESSES = 64`, `MAX_SANDBOX_ADDRESS_SPACE_BYTES = 1 GiB`,
  `MAX_SANDBOX_CPU_SECONDS = 20` -- passed straight to `prlimit`.

`VerificationBudget` (`service.py`) is an `asyncio.Lock`-guarded,
review-run-shared object mirroring the existing `budget_lock`/
`budget_state` token-budget pattern already used for provider-call
budgeting -- `try_reserve()` checks both the count and cumulative-seconds
ceiling before incrementing; `record_duration()` accumulates seconds
after every attempt regardless of outcome.

## No source mutation

Local-mode verification (`local=True`, CLI/local review, single trust
domain) copies the existing checkout (`shutil.copytree`, `symlinks=True`)
into a fresh, disposable temp directory before running anything there --
the caller's own working tree is never written to. Production
(`local=False`) never executes in the review worker process at all (see
"Architecture" above): the worker exports a credential-free artifact and
dispatches to the separate verifier process, which makes its own fresh
disposable copy of that artifact before running anything. Either way, the
workspace actually used for execution is always a fresh disposable copy,
disposed of afterward, success or failure -- never the original checkout
or the shared staged artifact itself.

## Exact-head binding, no caching

Every verification runs against the exact `commit_sha` passed in --
never a branch name, never "latest." There is no cross-head result
caching of any kind in v1: two different commit SHAs never share a
result object, and a stale result is never reused for a new head.

## Critic-only evidence, never shown to the specialist

`patchfrog.review.prompt.build_critic_prompt` gained an optional
`<executable_verification>` section, rendered only when
`evidence_text_for_report()` produces non-empty text -- which only
happens for `PASSED`/`CONFIRMED_FAILURE` outcomes.
`TIMEOUT`/`SANDBOX_ERROR`/`UNSUPPORTED`/`INCONCLUSIVE` all render empty
string: no conclusion, nothing actionable for the critic to weigh.
Wording is strictly neutral -- "treat runtime execution as evidence, not
as an automatic conclusion" -- never "proves," "will break," or "wrong."
This section is never shown to the original specialist prompt; it only
ever reaches the critic, for a proposal that already exists.

## Persistence

No new table. Six bounded, nullable-default *count* columns on
`review_runs` (migration `0028_executable_verification`):
`executable_verification_attempted_count`,
`executable_verification_confirmed_failure_count`,
`executable_verification_passed_count`,
`executable_verification_timeout_count`,
`executable_verification_unsupported_count`,
`executable_verification_inconclusive_count` (the last also absorbs
`SANDBOX_ERROR`, which has no dedicated column). No stdout/stderr
excerpt, no test target path, no commit SHA, and no candidate identity
is ever persisted -- only per-run counts, exactly like every other
Intelligence package's own telemetry discipline.

## Versioning

- `EXECUTABLE_VERIFICATION_VERSION = 1` -- introduced.
- `REVIEW_PROMPT_VERSION` bumped: a real new critic-only prompt section.
- `TELEMETRY_SCHEMA_VERSION` bumped: `ReviewTelemetrySnapshot` gained
  the exported `executable_verification` field.
- `QUALITY_COST_POLICY_VERSION` **unchanged**: unlike
  Trajectory/Cross-PR/Cross-Repo Intelligence, verification never
  contributes a structural signal to
  `ReviewEffortPolicy.decide_provisional` -- it only enriches the
  critic's own prompt for a proposal that is already being critiqued.
- `REVIEW_POLICY_VERSION` / `REVIEW_ENGINE_VERSION` **unchanged**: a new
  bounded evidence *text* section is a prompt-shape change, not a change
  to the deterministic validation/critic-selection rules or call shape
  those versions describe -- the same reasoning that already kept them
  unchanged for every prior Intelligence package.

**Milestone S6 (Production Execution Enablement) introduced
`VERIFIER_PROTOCOL_VERSION = 1`**
(`patchfrog.executable_verification.protocol`) -- a real, independently
versioned wire contract for the `VerificationExecutionRequest`/
`VerificationExecutionResult` pair crossing the review-worker/verifier
process boundary. Every other constant above is **unchanged** by S6: the
sandbox mechanism, result classification, and evidence rendering are all
byte-for-byte identical to Milestone S -- S6 only changes *which process*
calls the same functions and *how* the repository snapshot gets there.

## Finding ownership

Executable Verification never creates a standalone finding and never
competes with any other Intelligence package's own findings -- it only
ever attaches bounded runtime evidence to the critic's evaluation of an
already-real, already-proposed candidate. If the target test file
relationship itself is the finding (e.g. a missing/weakened test), that
finding is owned by Test Intelligence (Milestone M), not by this
package.

## Explicitly deferred

`STATIC_REPRODUCTION`, `GENERATED_TARGETED_TEST`, non-Python language
adapters, dependency installation of any kind, cross-head result
caching, repository-config-controlled verification scope, disk-quota
enforcement, and any generated-test or auto-fix capability. See
`validation/executable_verification/latest-summary.md` section 20 for
the full scope decision.

Filesystem confinement (mount namespace + read-only runtime bind + a
private, disposable `tmpfs`/`HOME`) was originally deferred here but has
since been implemented -- see "Sandbox" above and latest-summary.md
section 22.
