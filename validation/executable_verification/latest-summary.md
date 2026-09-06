# Milestone S: Executable Verification Foundation -- Pre-Implementation Audit

Branch: `feat/executable-verification`, off `main` @
`63924f9bab14f6ea8ef822e6c9739e373de0efe2` (Milestone R, Cross-Repo
Intelligence Foundation, merged).

## 0. Product principle (restated)

Move from "the code evidence strongly suggests this bug" toward "where
safely possible, PatchFrog executed a bounded, targeted verification and
observed evidence supporting or contradicting the hypothesis." This is
**not** arbitrary code execution, a CI replacement, an unrestricted test
runner, a shell agent, or an autonomous code-modification system.

## 1. Audit: existing execution infrastructure

- **`patchfrog/analysis/subprocess_sandbox.py::run_sandboxed`** already
  provides exactly the process-level guarantees this milestone needs:
  argv-array execution (never `shell=True`, never a caller-assembled
  string), an *allowlisted* environment (`PATH`/`HOME`/`LANG`/`LC_ALL`
  only -- no `DATABASE_URL`/`GITHUB_*`/`REDIS_URL`/provider keys ever
  visible), an explicit timeout with process-*group* kill (not just the
  immediate child, via `start_new_session=True` + `os.killpg`), and a
  genuine streaming cap on stdout/stderr (reading stops at the cap, never
  buffers unbounded output). **Reused directly, not reimplemented** --
  this milestone wraps its own isolation prefix around the same
  `run_sandboxed` call.
- **`patchfrog/repository/snapshot.py::RepositorySnapshotProvider`**
  already provides exactly the disposable-checkout model this milestone
  needs: a fresh temp directory per acquisition, cleaned up via a context
  manager, submodules **never** initialized ("would fetch and potentially
  execute config from arbitrary, untrusted URLs" -- the module's own
  docstring), and a path-traversal-safe `resolve_path`. **Reused
  directly** -- verification acquires its own fresh, exclusive snapshot
  exactly like `ContextService.build_context` already does per
  candidate, never sharing or mutating the snapshot anything else is
  using.
- **No existing Docker-level or namespace-level isolation** for any
  subprocess in this codebase today -- `run_sandboxed` is process-level
  only (allowlisted env + timeout + output caps), never network- or
  filesystem-namespaced. Static analyzers (ruff, semgrep, cppcheck,
  clang-tidy) only ever *read* source files; they never execute
  repository code, so this gap has never mattered before. **Executable
  Verification is the first PatchFrog feature that actually executes
  repository-controlled code**, which is a materially larger threat
  surface than anything reviewed in Milestones J-R.
- **`pytest` is a `dev`-only optional dependency** (`pyproject.toml`
  `[project.optional-dependencies].dev`), **not installed in the
  production worker image** (`docker/Dockerfile`'s `RUN pip install
  --no-cache-dir .` installs base dependencies only). This is a real,
  concrete gap: the production verifier environment cannot run pytest at
  all until this changes. Fixed by moving `pytest` into the base
  `dependencies` list, mirroring the exact precedent already established
  for `ruff`/`semgrep` ("invoked as analyzer subprocesses at runtime, not
  just used to lint PatchFrog's own source -- must be present outside a
  dev install too").
- **Test Intelligence (M) already derives exactly the "current candidate
  + real, structurally-discovered test file" relationship** this
  milestone needs (`patchfrog.test_intelligence.expectations.derive_test_surfaces`,
  producing `TestSurface.known_test_file_paths` per changed file, derived
  purely from Change Intelligence's own `TEST_NOT_UPDATED` companions --
  never a filename-similarity guess). **Not currently exposed on
  `TestIntelligenceReport`** (only `expectations`/`gaps`/`test_story` are)
  -- this milestone calls `derive_test_surfaces` directly, reusing the
  same pure function M's own service already calls internally, rather
  than duplicating its logic or waiting for M's own report shape to
  change.
- **Language/toolchain detection precedent**:
  `patchfrog.analysis.analyzers.base.Analyzer.discover()` establishes the
  exact pattern this milestone's own eligibility check follows -- "must
  never raise; represented as unavailable/reason, not an exception."
- **`.patchfrog.yml` is read from the PR's own untrusted head commit**
  (confirmed again, same finding as Milestone R's own audit,
  `patchfrog/review/config_resolution.py`) -- repository config can
  legitimately *reduce* review cost (capped by `apply_operator_hard_caps`)
  but must never expand what PatchFrog executes. This governs section 12
  below.

## 2. Threat model

Repository contents are treated as hostile, exactly like every other
untrusted input PatchFrog already handles (diff content, static-analyzer
input, indexed source). Explicitly modeled threats and their mitigation:

| Threat | Mitigation |
|---|---|
| Fork PR malicious code | Verification only ever targets one already-discovered test *file* (never arbitrary repository code), inside full process/network/PID isolation |
| Package install scripts | **Never installed** (section 1's dependency finding; no `pip install`/`npm install`/lifecycle scripts of any kind triggered by verification, ever) |
| Makefile arbitrary commands | Never invoked -- verification's only command shape is a fixed, deterministic `python3 -m pytest` argv, never a Makefile target |
| pytest plugins / conftest.py / test fixtures / compiler hooks | Not specifically defended against beyond the sandbox boundary itself (see section 3's own limitation) -- a plugin/fixture executes as the same unprivileged, network-isolated, resource-capped process as the test itself |
| Shell injection | Structurally impossible -- argv arrays only, `shell=True` never used (reuses `run_sandboxed`'s own existing guarantee) |
| Symlink / filesystem escape | Verification runs against a **fresh, disposable copy** of the snapshot (never the original), inside a temp directory it owns; a symlink inside the repository pointing outside that directory can still be *followed* by the OS (no mount-namespace/chroot in v1 -- see section 3), but there is nothing sensitive to read (no secrets in the sandboxed env) and no network to exfiltrate over |
| Environment / credential theft | The sandboxed process receives only `PATH`/`HOME`/`LANG`/`LC_ALL` (`run_sandboxed`'s existing allowlist) -- no `GITHUB_*`, no `DATABASE_URL`, no `REDIS_URL`, no `ANTHROPIC_API_KEY`/`GEMINI_API_KEY`, no Cloud credentials of any kind |
| Network exfiltration | `unshare --net --map-root-user` gives the process its own, unconfigured network namespace -- verified empirically (see section 4): even loopback is down by default, and a raw socket connection attempt to an external host fails with `Network is unreachable` |
| Fork bombs / process explosion | `unshare --pid --fork --mount-proc` gives the process its own PID namespace (verified: the sandboxed command becomes PID 1 inside it), and `prlimit --nproc=N` caps the process count -- verified: exceeding it produces "Cannot fork" |
| Memory / CPU exhaustion | `prlimit --as=BYTES --cpu=SECONDS` caps address space and CPU time; `run_sandboxed`'s own wall-clock timeout is the final backstop |
| Disk exhaustion | Not separately capped in v1 (no disk-quota mechanism exists in this codebase) -- documented limitation, mitigated by the same wall-clock timeout and by the disposable workspace being deleted immediately after |
| Docker socket / host mounts / cloud metadata endpoints | None are ever passed to the sandboxed process -- it inherits no Docker socket, no host bind mount, and the network-namespace isolation above also blocks reaching a cloud metadata endpoint (e.g. `169.254.169.254`) |
| Git credentials / GitHub App tokens / provider keys | Never present in the sandboxed environment at all (allowlist, section above) |

## 3. Absolute security requirements -- verified, not assumed

Verification execution receives **no** GitHub App private key, no GitHub
installation token, no Anthropic/Gemini/OpenAI key, no database password,
no Redis credentials, no Cloud credentials, no host SSH agent, no Docker
socket, no host home directory beyond its own disposable copy, no
arbitrary worker environment variable. Network is disabled by default.
Every one of these was **empirically tested** in this sandbox environment
before being relied upon (not merely asserted):

- `unshare --net --map-root-user` (unprivileged user namespaces are
  enabled on this host) genuinely isolates network: `curl` to an
  external host fails (`couldn't connect`, exit 7), and even loopback is
  `DOWN` by default (`ip link show` inside the namespace).
- `unshare --pid --fork --mount-proc` genuinely isolates the process
  tree: the sandboxed command becomes PID 1 inside its own namespace.
- `prlimit --nproc=N --as=BYTES --cpu=SECONDS` genuinely constrains
  resources (verified: an over-restrictive `--nproc` produces "Cannot
  fork").
- The full composed command (`unshare --pid --fork --mount-proc --net
  --map-root-user -- prlimit ... -- python3 -m pytest ...`) was run
  end-to-end against a real two-test file: `--collect-only` correctly
  discovers both tests (exit 0), the real run correctly reports "1
  failed, 1 passed" (exit 1), and a missing-dependency `ImportError`
  during collection correctly produces a collection error (exit 2) --
  never a crash, never a false "confirmed failure."

**Known, honestly-documented limitation**: this is process/network/PID
-namespace isolation, **not** a full container (no mount namespace, no
chroot, no pivot_root). The sandboxed process still sees the real host
filesystem outside its own disposable workspace and runs as the same
non-root user as the worker process itself (defense in depth: the Docker
worker image already runs as `USER patchfrog`, never root). There is
nothing sensitive for it to read (no secrets in its environment) and no
network to exfiltrate anything over, but a sufficiently determined
malicious test *could* still attempt to read arbitrary host files the
invoking OS user already has permission to read. Full filesystem
isolation (mount namespace + bind-mounted, read-only source +
tmpfs-backed scratch) is deferred -- doing it *correctly* is
meaningfully more infrastructure than a v1 foundation should attempt, and
getting it subtly wrong would be worse than not attempting it. **This
sandbox's strength is also host/container-dependent**: `unshare`
creating new namespaces may be blocked by a hardened container's default
seccomp/AppArmor profile. Verification therefore **fails closed**: if
`unshare`/`prlimit` are not both discoverable (`shutil.which`) at
eligibility-check time, the result is `SANDBOX_ERROR`/no execution --
never a silent, unisolated fallback.

## 4. No dependency installation in v1

Confirmed as a hard v1 rule, not merely a preference: verification never
runs `pip install`/`npm install`/`cargo install`/`go get`, or any package
manager lifecycle script, regardless of what the repository asks for.
The only dependency guaranteed present is `pytest` itself (moved into the
base `dependencies`, section 1) -- the target repository's *own* test
dependencies are never installed. This is detected, not assumed: a
`--collect-only` dry run always precedes the real test run; a
`ModuleNotFoundError`/collection error there classifies the whole
attempt as `UNSUPPORTED`, never `SANDBOX_ERROR`, never a guessed
`CONFIRMED_FAILURE`.

## 5. First verification primitive: EXISTING_TARGETED_TEST only

Per the spec's own preferred order (`EXISTING_TARGETED_TEST` >
`STATIC_REPRODUCTION` > `GENERATED_TARGETED_TEST`), v1 implements only
`EXISTING_TARGETED_TEST`. `STATIC_REPRODUCTION` and
`GENERATED_TARGETED_TEST` are deferred -- kept on
`VerificationKind` for forward documentation only, mirroring every prior
Intelligence package's own "one safely provable pattern, defer the rest"
precedent. Generated tests are explicitly **not** implemented (spec
section E20): a generated test's own correctness is an unverified
variable, and validating that correctly is a materially larger project
than this foundation.

## 6. Existing-test flow

```
current candidate (already selected for review)
  + real reviewer proposal (a specialist actually raised a concern)
  + a real TestSurface.known_test_file_paths entry for the candidate's
    own file_path (from patchfrog.test_intelligence.expectations.derive_test_surfaces,
    reused directly -- never re-derived, never guessed from filename similarity)
  + eligible sandbox (unshare + prlimit both discoverable)
  + verification budget remaining
  -> isolated sandbox execution of exactly that one known test file
     (never the full suite)
  -> PASSED / CONFIRMED_FAILURE / TIMEOUT / UNSUPPORTED / INCONCLUSIVE / SANDBOX_ERROR
  -> bounded evidence enriches the critic's own prompt for that proposal
```

## 7. Execution order: after reviewer proposal, before critic

Chosen over "run for every structural candidate before reviewer
reasoning" for two reasons: (a) cost -- most candidates never receive a
real specialist proposal at all, so unconditional pre-reviewer execution
would waste the execution budget on candidates nobody raised a concern
about; (b) product principle -- spec section E9 wants verification to
*enrich a hypothesis*, not to run speculatively before one exists. The
integration point is `AgentOrchestrator`'s own critique-decision step
(`patchfrog/review/orchestration.py`, immediately before its existing
`build_critic_prompt` call) -- the exact point where a real,
validated, not-yet-suppressed proposal is known to exist and critique is
about to happen. Reuses the *existing* critic mechanism entirely; no
second critic path.

## 8. Result classification (empirically grounded, section 3)

- Sandbox unavailable (`unshare`/`prlimit` not discoverable) or snapshot
  acquisition fails -> `SANDBOX_ERROR`, no execution attempted.
- `--collect-only` exits non-zero (collection error, missing dependency,
  usage error, no tests found) -> `UNSUPPORTED`, real run never attempted.
- Real run: exit 0 -> `PASSED`. Exit 1 -> `CONFIRMED_FAILURE` (the known
  test file the candidate is linked to genuinely fails on this exact
  head). Any other exit code -> `INCONCLUSIVE` (defensive; should not
  normally occur once collection already succeeded).
- `run_sandboxed`'s own `timed_out=True` takes priority over exit-code
  interpretation in all cases -> `TIMEOUT`.

`PASSED` is never treated as universal proof the hypothesis is wrong
(the test may not actually exercise the specific claim); `TIMEOUT` is
never treated as a bug. Both are handed to the critic as neutral
evidence with an explicit "verify independently" instruction, mirroring
every other Intelligence package's own evidence-wording discipline.

## 9. Bounds

`MAX_VERIFICATIONS_PER_REVIEW = 5` (shared across the whole review run,
mirrors the existing `budget_lock`/`budget_state` pattern already used
for token budgets), `MAX_VERIFICATION_SECONDS = 30.0` (single-attempt
wall clock, passed to `run_sandboxed`), `MAX_TOTAL_VERIFICATION_SECONDS = 90.0`
(review-run-wide ceiling, checked before each new attempt),
`MAX_STDOUT_BYTES = 8192` / `MAX_STDERR_BYTES = 8192` (evidence-text
excerpt bound -- far below `run_sandboxed`'s own 5 MiB streaming cap;
verification's evidence is meant to be a short excerpt, not a log dump).
`MAX_VERIFICATIONS_PER_CANDIDATE` is effectively `1` by construction --
there is only ever one known test file target per candidate's file in
v1, never a search over multiple candidate targets. All are fixed
Python constants, never `.patchfrog.yml`-controlled, never
environment-variable-controlled -- the strongest possible security
posture for a security-critical bound is having no configuration
surface for it at all (spec section E12's own "repository config must
never expand execution capability," taken to its logical conclusion for
v1).

## 10. Repository config

Repository-controlled `.patchfrog.yml` has **no** verification-related
field in v1 -- not "disable," not "reduce scope," nothing. Given section
1's confirmed finding that `.patchfrog.yml` is read from the PR's own
untrusted head, and given even a "disable verification" flag would need
careful handling to avoid a confusing opt-out-then-opt-back-in race,
the safest and simplest v1 choice is **no repository-config surface for
verification at all** -- it is entirely operator/architecture-controlled
via the fixed bounds in section 9. A future milestone could add an
explicit, narrow "disable" flag (verification can only ever be reduced
by repository config, never expanded, per spec section E12) once real
operational experience shows it's needed.

## 11. Command construction

No shell interpolation, ever. Every command is a fixed Python list
(`["unshare", "--pid", "--fork", "--mount-proc", "--net",
"--map-root-user", "--", "prlimit", f"--nproc={N}", f"--as={BYTES}",
f"--cpu={SECONDS}", "--", "python3", "-m", "pytest", "-q", ...,
test_target_path]`), built entirely from fixed strings and the one
repository-derived value that's already validated against the actual
snapshot's own file inventory (`test_target_path`, taken verbatim from
`TestSurface.known_test_file_paths`, itself derived from the real
indexed repository graph -- never taken from `.patchfrog.yml` or PR
text). No adapter ever accepts an arbitrary command from repository
config.

## 12. Language adapter: Python/pytest only

Chosen because PatchFrog's own indexing/context/test-intelligence
infrastructure already has the strongest existing Python support, and
`pytest` itself can now be a base dependency (section 1) without any new
system-package installation. C/C++ execution is deferred -- there is no
existing "how do I build and run a C/C++ test" infrastructure in this
codebase today (cppcheck/clang-tidy are static analyzers, never
compilers/linkers), and building that safely is out of scope for this
foundation.

## 13. Sandbox abstraction

`VerificationSandbox` (`patchfrog/executable_verification/sandbox.py`)
owns: a disposable working-directory copy (never the original snapshot),
the fixed isolation-prefix argv construction, delegating actual process
execution to the *existing*, unmodified `run_sandboxed` (never a second,
parallel subprocess-execution implementation), timeout, output bounds,
and cleanup (`shutil.rmtree` in a `finally` block, mirroring
`RepositorySnapshot.cleanup()`'s own discipline). Availability
(`unshare`/`prlimit` discoverable) is checked once via `shutil.which`,
mirroring `Analyzer.discover()`'s own "never raise, represent as
unavailable" contract.

## 14. Execution never modifies the reviewed source

Verification's own workspace is a **full, disposable copy** (`shutil.copytree`)
of the already-disposable `RepositorySnapshot` used elsewhere in the same
review run -- never the original snapshot object, never the checkout any
other Intelligence layer or the Context Engine is using. No commit, no
push, no fix application, no PR write of any kind happens anywhere in
this package.

## 15. Exact-head binding

Every `ExecutableVerificationEvidence` carries the exact `commit_sha` it
ran against. There is no cross-head caching in v1 (section 16) -- a
result is only ever used for the exact review run that produced it, and
is never persisted as raw evidence beyond that one run's own
telemetry/critic-prompt lifetime.

## 16. Caching

None in v1. Every eligible candidate gets a fresh execution, bounded by
the budget in section 9. A same-exact-head-and-target cache is
deliberately deferred -- the added complexity of proving "safe to reuse"
correctly is not justified until real usage shows repeated executions
are common (e.g. retries of the same commit).

## 17. Persistence

No new table -- mirrors every prior Intelligence package's "count-only
telemetry, nothing raw persisted" precedent, taken further here since
even the report itself is per-candidate and never the review run's own
top-level report shape. Six nullable-default count columns on
`review_runs` (migration `NNNN_executable_verification`):
`executable_verification_attempted_count`,
`executable_verification_confirmed_failure_count`,
`executable_verification_passed_count`,
`executable_verification_timeout_count`,
`executable_verification_unsupported_count`,
`executable_verification_inconclusive_count`. **Never persisted**: full
unrestricted logs, environment dumps, credentials, arbitrary binary
output -- only the bounded excerpt lives transiently in the
`ExecutableVerificationEvidence` object for the duration of one review
run's own critic-prompt construction.

## 18. Finding ownership

Executable Verification is a verifier, never a standalone finding
generator. It never publishes "test failed" on its own -- it only ever
enriches the critic's own evaluation of an already-real, already-proposed
specialist finding. `PASSED` may allow the critic to weigh the hypothesis
as less likely (never automatically suppress it -- the critic decides,
exactly as it already does for every other kind of evidence).
`CONFIRMED_FAILURE` strengthens the critic's evidence but never bypasses
critic judgment.

## 19. Review Effectiveness Benchmark (Part F)

A separate, durable foundation (`validation/review_effectiveness/`)
measuring reviewer quality independent of implementation-test
correctness. Uses the existing FakeLLM/oracle evaluation harness
(`patchfrog/evaluation/`) -- **FakeLLM proves deterministic orchestration
and evaluation correctness; it does not prove real-model review
quality.** No live Anthropic/OpenAI calls; Gemini likewise not called
for this benchmark (no zero-cost, explicitly-safe automated reason to).

## 20. Scope decision (v1)

**Implemented**: S1 (secure execution foundation: `VerificationSandbox`,
empirically-verified isolation), S2 (existing-targeted-test verification,
Python/pytest only), S3 (bounded critic-only runtime evidence
integration), S4 (review-effectiveness benchmark foundation -- schema +
a handful of example cases + metrics reusing the existing evaluation
harness), S5 (bounded, count-only verification telemetry).

**Deferred**: `STATIC_REPRODUCTION`, `GENERATED_TARGETED_TEST`, C/C++
adapters, dependency installation of any kind, cross-head result caching,
repository-config-controlled verification scope, mount-namespace/chroot
filesystem isolation, disk-quota enforcement.

## 21. Explicitly not started

Agent Handoff / MCP, OpenAI provider, Model Router, Merge Readiness,
Cloud. Implementation proceeds now that this audit is complete.
