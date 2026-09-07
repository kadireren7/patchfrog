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
| Fork PR malicious code | Verification only ever targets one already-discovered test *file* (never arbitrary repository code), inside full process/network/PID/filesystem isolation |
| Package install scripts | **Never installed** (section 1's dependency finding; no `pip install`/`npm install`/lifecycle scripts of any kind triggered by verification, ever) |
| Makefile arbitrary commands | Never invoked -- verification's only command shape is a fixed, deterministic `python3 -m pytest` argv, never a Makefile target |
| pytest plugins / conftest.py / test fixtures / compiler hooks | Not specifically defended against beyond the sandbox boundary itself -- a plugin/fixture executes as the same unprivileged, network-isolated, filesystem-confined, resource-capped process as the test itself |
| Shell injection | Structurally impossible -- argv arrays only, `shell=True` never used (reuses `run_sandboxed`'s own existing guarantee) |
| Symlink / filesystem escape | **Corrected (section 22)**: a symlink inside the workspace pointing outside it was empirically confirmed followable and readable in this package's first version -- now blocked by real mount-namespace confinement (`bwrap`): the symlink target simply does not exist inside the sandbox's own filesystem view, confirmed empirically against the exact same escape attempt that previously succeeded |
| Environment / credential theft | The sandboxed process receives only a fixed, sandbox-owned `PATH`/`HOME`/`TMPDIR`/`XDG_CACHE_HOME`/`PYTHONPYCACHEPREFIX`/`LANG`/`LC_ALL` -- no `GITHUB_*`, no `DATABASE_URL`, no `REDIS_URL`, no `ANTHROPIC_API_KEY`/`GEMINI_API_KEY`, no Cloud credentials of any kind, and (since section 22) no real worker `HOME` either |
| Network exfiltration | `bwrap --unshare-all` (without `--share-net`) gives the process its own, unconfigured network namespace -- verified empirically (see section 4 and 22.3): even loopback is down by default, and a raw socket connection attempt to an external host fails with `Network is unreachable` |
| Fork bombs / process explosion | The fresh PID namespace (verified: the sandboxed command becomes an early PID inside it), and `prlimit --nproc=N` (applied *inside* the sandbox -- section 22.3) caps the process count -- verified: a real fork loop is blocked by `OSError` at the configured ceiling |
| Memory / CPU exhaustion | `prlimit --as=BYTES --cpu=SECONDS` (applied inside the sandbox) caps address space and CPU time -- verified: a real memory allocation past the `--as` ceiling raises `MemoryError`; `run_sandboxed`'s own wall-clock timeout is the final backstop |
| Disk exhaustion | Not separately capped in v1 (no disk-quota mechanism exists in this codebase) -- documented limitation, mitigated by the same wall-clock timeout, the disposable workspace being deleted immediately after, and `/tmp` now being a private, disposable `tmpfs` rather than shared host disk |
| Docker socket / host mounts / cloud metadata endpoints | None are ever passed to the sandboxed process, and (since section 22) the mount namespace no longer exposes the host filesystem at all outside the explicit runtime/workspace binds -- empirically confirmed `/var/run/docker.sock` is not visible; the network-namespace isolation above also blocks reaching a cloud metadata endpoint (e.g. `169.254.169.254`) |
| Git credentials / GitHub App tokens / provider keys | Never present in the sandboxed environment at all (allowlist, section above) |
| Real host filesystem outside the workspace (e.g. worker `$HOME`, application state, arbitrary host paths) | **Corrected (section 22)**: this package's first version had no defense here at all -- a real escape was empirically confirmed and fixed by `bwrap`'s mount-namespace confinement, which now exposes only `/usr` (read-only, for the Python/pytest runtime), the disposable workspace (read-write), and a private, disposable `tmpfs`/`HOME` |

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

**Corrected (see section 22 for the full narrative): this is now real
filesystem confinement, not merely process/network/PID isolation.** An
earlier version of this document claimed process/network/PID isolation
alone was an acceptable v1 boundary because "there is nothing sensitive
for [a malicious test] to read (no secrets in its environment)." A
security correction round found that claim insufficient and demanded
empirical proof -- the proof showed a real escape (the actual worker
$HOME, an arbitrary file outside the disposable workspace, and the real
Docker socket were all readable), which is now fixed by routing
execution through `bwrap` (bubblewrap) with an explicit, minimal
read-only runtime bind plus a private `tmpfs`, rather than the bare
`unshare`/`prlimit` composition described above. Section 22 has the full
empirical transcript, including a critical finding: the standard,
non-privileged worker Docker container **cannot** create the required
namespaces at all under its current default security configuration --
true of both the original mechanism and this correction -- so
verification fails closed there too, honestly, rather than silently
degrading to unisolated execution. **This sandbox's strength is
host/container-dependent, confirmed in two independent, empirically
tested ways**: GitHub Actions' `ubuntu-latest` runner (AppArmor's
unprivileged-`CLONE_NEWUSER` restriction) and PatchFrog's own default,
non-privileged worker Docker container (Docker's own default seccomp
profile blocking the `mount()` operations bwrap needs). Verification
therefore **fails closed**: :func:`is_sandbox_available` runs a real
functional probe exercising the same mount-namespace/bind/tmpfs
operations real verification depends on -- binary presence
(`shutil.which`) alone is never sufficient evidence. See section 22 for
exactly what additional infrastructure would be required for production
execution to actually activate, and why that decision is left to the
operator rather than made silently here.

**Correction found via real CI, not merely local testing**: the first
push of this milestone's PR failed 7 real integration tests on GitHub
Actions' own `ubuntu-latest` runner. Root cause: `shutil.which` binary
-presence alone is not sufficient evidence the sandbox actually works.
Ubuntu 24.04+'s default AppArmor restriction on unprivileged
`CLONE_NEWUSER` blocks `unshare --map-root-user` even when both
`unshare`/`prlimit` are present on `PATH` -- confirmed exactly on that
runner (`unshare: write failed /proc/self/uid_map: Operation not
permitted`). Before the fix, this failure surfaced *inside* the wrapped
command, so the pytest adapter's own collect-only step saw a non-zero
exit code and misclassified it as `UNSUPPORTED` ("this test's own
dependencies are missing") rather than `SANDBOX_ERROR` ("the sandbox
itself could not run") -- two structurally different claims. Fixed by
adding a real, side-effect-free functional probe
(`patchfrog.executable_verification.sandbox._probe_isolation`, an actual
`unshare --pid --fork --mount-proc --net --map-root-user -- true`
invocation) to `is_sandbox_available()`, so a host that cannot actually
create the isolation now fails closed *before* any pytest command is
ever attempted, honestly reported as `SANDBOX_ERROR`. This also means
`tests/integration/test_executable_verification_corpus.py`'s own
module-level `skipif` now correctly skips the entire behavioral corpus
on such a host (this is why the corpus was proven end-to-end on this
development environment, which does support unprivileged user
namespaces, but is expected to skip -- not fail -- on GitHub Actions'
current runner image). New unit coverage:
`tests/unit/test_executable_verification_sandbox.py` (10 tests) proves
`is_sandbox_available`/`_probe_isolation`'s own logic deterministically,
independent of host capability.

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

## 22. Filesystem confinement -- security correction round

This section documents a full correction round that happened **after**
this milestone's PR was opened and CI-green, prompted by an explicit
security review that refused to accept process/network/PID isolation
alone as a credible boundary for executing hostile, repository-controlled
code. It found the refusal correct.

**22.1 -- Empirical escape, confirmed before any fix.** Using the
merged, CI-passing `VerificationSandbox` exactly as it shipped (`unshare
--pid --fork --mount-proc --net --map-root-user -- prlimit ... --
python3 -m pytest ...`), a real pytest target running inside it was
able to:

- Read the real invoking user's `$HOME/.bashrc` in full.
- Read an arbitrary synthetic-sentinel file placed outside the
  disposable verification workspace, both via a direct absolute path and
  via a symlink planted inside the workspace pointing at it.
- Observe that `/var/run/docker.sock` exists (visible, not merely
  guessed at).
- Read `/proc/self/environ` and see the real `PATH`/`HOME` of the
  invoking process (not secrets -- those were never in the allowlisted
  environment to begin with -- but real host paths, confirming no
  filesystem confinement existed at all).

No credential or real secret was ever read (none was ever placed in the
sandboxed environment, by design, both before and after this
correction) -- but the filesystem boundary itself was proven absent, not
merely theoretical.

**22.2 -- Fix: bubblewrap-based mount-namespace confinement.**
`patchfrog/executable_verification/sandbox.py` now routes every
execution through `bwrap` (bubblewrap) -- the same purpose-built,
widely-audited unprivileged-sandboxing primitive Flatpak uses -- rather
than hand-rolled `pivot_root`/mount-namespace logic. A fresh mount
namespace exposes exactly:

- `/usr`, read-only (covers the entire Python/pytest runtime -- the
  worker image's own system Python, with pytest installed as a base
  dependency, lives under `/usr/local`, itself nested inside `/usr`).
- The usr-merge symlinks (`/bin`, `/sbin`, `/lib`, `/lib64` -> `/usr/...`)
  recreated as symlinks, not bound as directories (binding the symlink
  file itself, rather than recreating it, was empirically found to break
  the dynamic linker -- `execvp: No such file or directory` -- during
  this correction).
- A local/CLI dev environment's own venv root, read-only, *only* when
  the running interpreter lives outside `/usr` (computed once from
  `sys.prefix`, shared by the real sandbox and its own probe so they can
  never drift apart) -- a no-op in production, where the system Python
  already covers this.
- The disposable verification workspace itself, read-write, and nothing
  else from the host.
- A private `tmpfs` at `/tmp`, hosting a throwaway, sandbox-owned `HOME`
  (`/tmp/home`, created via `bwrap --dir`) -- never the real worker home,
  and requiring no host-side directory for a caller to create or clean
  up (it vanishes with the sandbox). `TMPDIR`, `XDG_CACHE_HOME`, and
  `PYTHONPYCACHEPREFIX` all point inside this same private tmpfs.
- A fresh `/proc` and minimal `/dev`.

No other host path is bound, read-only or otherwise.

**22.3 -- Empirical re-verification after the fix, same exact attack
attempted.** The identical malicious test file used in 22.1 was re-run
through the corrected sandbox:

- Real `$HOME` read attempt: `FileNotFoundError` (the disposable
  `/tmp/home` has no `.bashrc`).
- Outside-workspace sentinel read attempt (direct path): not visible.
- Outside-workspace sentinel read attempt (via a symlink planted inside
  the workspace): not visible -- the symlink target resolves to nothing
  inside the sandbox's own mount namespace.
- `/var/run/docker.sock`: `os.path.exists` returns `False`.
- `/proc/self/environ`: contains only the sandbox's own minimal,
  disposable environment (`PATH`/`HOME`/`TMPDIR`/`XDG_CACHE_HOME`/
  `PYTHONPYCACHEPREFIX`/`LANG`/`LC_ALL`/`PWD`) -- no real host path, no
  real username.
- Network: still denied (`Network is unreachable`), including loopback.
- `prlimit` resource limits still genuinely enforce **when applied
  inside the `bwrap` sandbox** (a memory allocation past `--as` raises
  `MemoryError`; a fork loop past `--nproc` raises `OSError` after
  ~62 successful forks against a `--nproc=64` ceiling) -- applying
  `prlimit` *outside* `bwrap` was tried first and found to break
  `bwrap`'s own internal setup entirely (`Creating new namespace failed:
  Resource temporarily unavailable`), because `RLIMIT_NPROC` counts
  against the calling **real UID's entire host process count** (every
  unrelated process this developer's desktop was already running) until
  a fresh user namespace exists to scope it -- exactly why `prlimit` must
  run *inside* `bwrap`, never around it.
- Timeout/kill: unchanged and re-verified -- a 30-second sleep under a
  2-second sandbox timeout is killed in ~2s (not ~30s), `timed_out=True`,
  and a `pgrep` sweep after the kill finds no leaked process.
- Cleanup: unchanged and re-verified -- the internal disposable
  workspace copy (a fresh `tempfile.mkdtemp`) is actually removed after
  both a successful and a timed-out run, and the original `root_path`
  passed in by the caller is never mutated (file mtimes unchanged, no
  `.pytest_cache` written back into it).
- A normal, non-malicious pytest run (collect-only then real run) still
  passes end-to-end through the exact production code path
  (`build_executable_verification_report` -> `run_pytest_verification` ->
  `VerificationSandbox`), including the disposable `HOME`/`tmpfs` being
  writable when pytest wants to write there.

All of the above was run through the real, unmocked code path -- not a
hand-constructed `ExecutableVerificationEvidence` standing in for a
subprocess round trip -- and is now pinned by
`tests/integration/test_executable_verification_corpus.py` sections
26-37 (12 new scenarios) plus `tests/unit/test_executable_verification_sandbox.py`
(18 tests covering `is_sandbox_available`/`_probe_isolation`/
`_runtime_bind_args` deterministically).

**22.4 -- The functional probe was extended to match, not just checked
again.** `is_sandbox_available` no longer probes a bare
`unshare --map-root-user`; `_probe_isolation` now runs the *exact same
category* of operation the real sandbox depends on (fresh mount
namespace, read-only `/usr` bind plus usr-merge symlinks, private
`tmpfs`, fresh `/proc`/`/dev`) via the same cached `_runtime_bind_args()`
helper the real sandbox uses, so the probe can never silently drift from
what execution actually requires. A host that can create a plain PID/net
namespace but cannot establish the filesystem confinement is correctly
reported unavailable, not merely "isolated enough."

**22.5 -- Critical finding: the standard worker Docker container cannot
run this at all, and never could.** Testing this correction inside a
*real* `docker run` of the actual `worker` image target -- not this
development host -- with no extra flags beyond running as the same
non-root `patchfrog`-equivalent user the Dockerfile already uses
(`--user 1000:1000`, no `--privileged`, no added capabilities) found
that even the plain `unshare --pid --fork --net --map-root-user`
call **already merged before this correction** fails there
(`unshare: unshare failed: Operation not permitted`) -- before ever
reaching the filesystem-confinement logic this correction adds. This is
a pre-existing limitation of the deployment shape, not a regression
introduced here: the first version of this sandbox never actually
worked inside PatchFrog's own default worker container either, and its
own audit (section 3, original text) never tested that specific
context -- only this bare development host.

Systematic empirical testing of what it would take to make this work in
a default `docker run` (no `--privileged`, no Docker-in-Docker) found no
combination of `--cap-add SYS_ADMIN`, `--security-opt seccomp=unconfined`,
and `--security-opt apparmor=unconfined` -- individually or all three
together -- sufficient. Loosening AppArmor alone gets past an initial
"cannot change root filesystem propagation" failure to a later "mount
/proc failed: Operation not permitted," and adding `CAP_SYS_ADMIN` and an
unconfined seccomp profile on top still does not resolve that second
failure. Reaching a working configuration would require either
`--privileged` (explicitly out of scope per this correction's own
instructions), Docker-in-Docker (also explicitly out of scope), or moving
to a container runtime purpose-built for nested unprivileged sandboxing
(e.g. `sysbox-runc`) or a dedicated, non-containerized execution host --
all of which are deployment/infrastructure decisions for an operator to
make explicitly, not something this engine PR silently assumes, requests,
or downgrades isolation to work around.

**22.6 -- Production-readiness conclusion.** `is_sandbox_available()`'s
real functional probe correctly returns `False` inside PatchFrog's own
default, non-privileged worker Docker image (confirmed directly inside a
built image, not inferred) -- meaning **Executable Verification's
EXISTING_TARGETED_TEST execution reports `SANDBOX_ERROR` for every
eligible candidate under the default self-hosted deployment, exactly as
designed, and never runs unsandboxed.** This is the correct, safe,
honest behavior, not a bug, and is unchanged from before this correction
(the pre-existing mechanism had the identical gap). Real execution
requires an explicit operator infrastructure decision -- granting the
worker container the specific capabilities described in 22.5, or running
verification on a differently-shaped execution host -- that this PR
deliberately does not make. Domain model, eligibility, result model,
telemetry, and the Review Effectiveness Benchmark foundation are all
unaffected by this and remain fully functional regardless of sandbox
availability, exactly as designed from the start (a review run with an
unavailable sandbox produces `SANDBOX_ERROR`/no-attempt reports, folded
into the existing count-only telemetry, never a crash or a degraded
alternate path).

**22.7 -- Versioning re-audit for this correction.** `EXECUTABLE_VERIFICATION_VERSION`
stays at **1, unchanged**. Its own docstring says it bumps when "sandbox
isolation shape... changes materially enough that a prior report can no
longer be considered equivalent to what re-running now would produce" --
but nothing in this codebase ever consumes it for an equivalence check:
there is no cross-head result caching (by design, section 16) and no
report is ever persisted raw, so there is no comparison this correction
could silently invalidate. More importantly, the *public semantic
contract* of a report is unchanged -- `VerificationOutcome.PASSED` still
means exactly what it meant before ("the known, targeted test genuinely
passed under a real, isolated sandboxed run"); this correction only
strengthens *how* isolated that run is, never what the outcome asserts.
`REVIEW_PROMPT_VERSION`, `TELEMETRY_SCHEMA_VERSION`,
`QUALITY_COST_POLICY_VERSION`, `REVIEW_POLICY_VERSION`, and
`REVIEW_ENGINE_VERSION` are unaffected for the same reason plus the
original milestone's own reasoning (section 7 conditions, unchanged) --
none of the critic prompt shape, telemetry field shape, tiering
semantics, validation rules, or call shape changed in this correction
round. No version constant changes as a result of this security fix.

**22.8 -- A transient full-suite flake, investigated rather than
dismissed.** One full `tests/integration` run during this correction's
own gate-checking saw 4 of `test_executable_verification_corpus.py`'s
tests fail (`test_case_confirmed_failure_classification`,
`test_case_verifier_workspace_remains_readable`,
`test_case_disposable_home_and_tmp_writable`,
`test_case_docker_socket_unavailable`) while 597 others passed. Rather
than assume flakiness, this was investigated directly: all 4 tests
passed reliably when re-run in isolation; the full corpus file (38
tests) passed reliably standalone, combined with its three immediately
preceding files, and even under 300 artificially added background
processes on this host. `docker ps -a` at the time showed unrelated
third-party container activity on this shared development machine, and
this session had also been running a concurrent `docker build` at the
exact moment of the failing run. A subsequent clean, split run of the
entire `tests/integration` suite (two halves, run separately, with no
concurrent heavy background work from this session) passed completely:
319 + 282 = 601 passed, 0 failed. This is real, environment-level
contention on a shared development machine, not a defect in the
corrected sandbox -- documented honestly rather than silently re-run
until green.
