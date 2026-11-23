# Local Revision Identity, Run Store and Job Runner (Milestones 2.1–2.2)

Status: **implemented (2026-11-22)** in `patchfrog/app/revision.py` and `patchfrog/app/store.py`
(2.1) and `patchfrog/app/jobs.py` (2.2, §3).
Decision background: ADR-002 (local artifacts on disk, no database). The public names are listed in
`PUBLIC_API.md` (all **provisional**). Neither milestone adds rules, a CLI command or an MCP tool.

## 1. RepositoryRevision

### What is identified
The **analysed working-tree snapshot**: the exact bytes of every in-scope file at capture time.
This is deliberately *not* a Git commit.

| | Git commit | Analysed snapshot (`revision_id`) |
|---|---|---|
| Covers | committed content only | what's on disk now: tracked + (default) untracked-not-ignored files |
| Uncommitted edits | invisible | included |
| Identity | commit SHA (includes history, author, message) | sha256 over sorted `(path, size, sha256)` + scope fingerprint |
| Same bytes, different commit | different SHA | **same id** (e.g. after committing an already-captured edit) |
| Same bytes, other clone or path | same SHA | same id (no absolute path is part of it) |

`commit_sha`, `dirty` and `changes` (modified/added/deleted versus `HEAD`) are **provenance**, not
identity. Plain directories (no Git) are supported: `kind = directory`, `commit_sha` and `dirty`
are `None`. Identical content gives the same `revision_id` whether it's captured from a Git
checkout or from a plain copy (tested).

### Scope (part of the identity, via `scope_fingerprint`)
- Git: `git ls-files --cached [--others --exclude-standard]`, so `.gitignore` is honoured.
  `include_untracked=False` restricts the scope to tracked files.
- Directory: `os.walk(followlinks=False)`; `.git` directories are pruned.
- Both: the shared vendor/build/virtualenv denylist is **pruned** (never descended into in
  directory mode). Secret-store-like names (`.env*`, keys, credentials) are **never opened** and
  are not part of the identity. `exclude_prefixes` and `exclude_paths` (absolute paths, used for a
  store placed inside the root) remove further subtrees.
- Budgets (`max_files`, default 200,000; `max_total_bytes`, default 2 GiB) are **not** part of the
  identity. They can only abort a capture, never change its result.

### Safety (repository content is untrusted)
- Nothing from the repository is executed or imported. Git is used only for listing (`rev-parse`,
  `ls-files`, `ls-tree`), with hooks, fsmonitor and the untracked cache disabled, and without
  global or system config (`run_git`). **`git status` and `git hash-object` are never used**: on
  this host, plain `git status` was shown to execute both a repository-configured
  `core.fsmonitor` command and a clean filter, while `capture_revision` on the same repository
  executed neither (`test_repository_configured_commands_are_never_executed`).
- Files are opened component by component relative to the root with `O_NOFOLLOW` (`openat`
  semantics). A symlink anywhere in a path, including one swapped in during capture, is skipped,
  so nothing outside the root is ever read. FIFOs, devices and submodule directories are skipped
  without blocking (`O_NONBLOCK` + `fstat`).
- Dirty detection compares git blob IDs computed **from raw bytes** in the same read pass with
  `git ls-tree -r HEAD`. No filter runs. Consequence: files under EOL or LFS or clean/smudge
  attributes can report `modified` although `git status` calls them clean. That is a
  conservative, documented difference.

### Consistency and errors
- Each file is hashed from a single descriptor, with `fstat` before and after the read. After
  hashing, the listing and every file's `(size, mtime_ns, ctime_ns, inode)` are re-checked, and
  `HEAD` is re-read.
- On any difference, the capture is retried once. If the tree is still changing, it raises
  `RevisionUnstableError`. **A wrong identity is never returned.** Detection granularity is the
  filesystem's timestamp resolution.
- Typed errors: `RevisionCaptureError` (base: bad root, git can't inspect the repository, an
  undecodable path in git output), `RevisionBudgetExceeded` (fails fast, before or during
  hashing), `RevisionUnstableError`.

### Cost
- One streaming read per in-scope file (1 MiB chunks); memory doesn't depend on file sizes.
- There is no persistent hash cache (yet): a repeat capture re-reads every in-scope file. That's
  a measured, deliberate simplicity choice (§5). A stat-validated cache (git-index-like, with
  racy-entry protection) is the obvious next optimization if real repositories need it.

## 2. LocalRunStore

### Layout and identities
`objects/<aa>/<sha256>` (immutable, read-only files), `runs/<run_id>/{run.json, lock}`,
`index/<idempotency_key>`, `tmp/`, `locks/<aa>` (request-lock stripes, 2.2), `store.json`, `lock`. Everything is created `0o700`.
- **Artifact identity:** `ArtifactRef(sha256, size)`, a content address. Identical bytes are
  stored once (dedup; a duplicate put doesn't rewrite).
- **Execution identity:** `run_id` (uuid4), unique per execution.
- **Request identity:** `idempotency_key`, deterministic and supplied by the caller (later: the
  JobRunner, over revision + configuration). `find_succeeded(key)` returns the **first**
  SUCCEEDED run for the key, and only if all of its artifacts are still present. PARTIAL, FAILED,
  CANCELLED and INTERRUPTED runs are never reused.

### Lifecycle
`begin_run → RUNNING → finish(SUCCEEDED | PARTIAL | FAILED | CANCELLED)`. `INTERRUPTED` is set
only by `recover()`. A finished record is never changed by the owner again. Leaving the `RunHandle`
context without finishing records `FAILED` with the reason.

### Atomicity, durability, corruption
- Every file is written to `tmp/` (exclusive create, `O_NOFOLLOW`, `flock`-held), then `fsync`ed,
  then `os.replace`d, and then the parent directory is `fsync`ed. **Readers see either the
  complete file or nothing.** Records carry a sha256 of their canonical JSON. Artifacts are
  verified against their address on every read, and `verify()` re-hashes everything (streaming).
  Newer schemas raise `IncompatibleSchemaError`.
- **Tested:** process crashes (real `SIGKILL` during a run and during a 8+ MiB streamed write: no
  partial object is published, the run is recovered as `INTERRUPTED`, abandoned temp files are
  removed); concurrent writers and readers across processes (4 writers + 2 readers, 5 repeated
  runs: no torn or corrupt read, dedup to one object); truncated and tampered records; corrupted
  objects.
- **Not tested (platform assumption):** power loss or OS crash. Durability then depends on the
  filesystem honouring `fsync` on files and directories (true for ext4/xfs/btrfs with default
  options on local disks). **No stronger durability claim is made.**

### Concurrency
- Any number of processes may share a store. Puts of the same content race harmlessly (identical
  bytes, atomic replace).
- **Liveness uses `flock`**: the run owner holds an exclusive lock on `runs/<id>/lock` for its
  lifetime, and the kernel releases it on process death, even on SIGKILL. `recover()` marks a
  RUNNING run INTERRUPTED only when it can take that lock. There are no PID checks, so PID reuse
  is not a problem. `recover()` and `prune()` serialize on the store `lock`.
- Platform: POSIX local filesystems. **`flock` is not reliable on NFS and similar network
  filesystems; don't put a store there.** Windows is not supported (no `fcntl`).

### Retention (bounded disk)
`prune(keep_last=, older_than_seconds=, max_total_bytes=)` deletes finished runs by policy, then
garbage-collects unreferenced objects. It **never** deletes a RUNNING run, one of the `keep_last`
newest finished runs, or an unreferenced object younger than `gc_grace_seconds` (default 24 h: a
live run may have stored it without recording it yet). When `max_total_bytes` can't be met
without deleting protected data, it reports `over_budget=True` instead. `max_artifact_bytes`
(default 256 MiB) bounds every single artifact.

### Path and symlink safety
The run ID, artifact address, idempotency key, revision ID, run kind and artifact names are
validated by strict patterns, so no caller-supplied string becomes a path component unchecked.
The store root must not be a symlink, must be owned by the user and must not be group- or
world-writable. The same is checked for every internal directory, including object buckets (a
planted bucket symlink is refused; tested). Files are opened with `O_NOFOLLOW`.

### Location
The store root is an explicit constructor argument. The CLI (Milestone 2.5) defaults to, in order:
`--store`, the `PATCHFROG_STORE_DIR` environment variable, `$XDG_CACHE_HOME/patchfrog/store`, and
`~/.cache/patchfrog/store` (`patchfrog/cli_scale.py`, `default_store_root`). It is **not**
`CoreSettings`, contrary to the earlier plan: `CoreSettings` reads `.env` from the working
directory, which is the analysed repository when the CLI runs there, and repository content must
not choose where PatchFrog writes (tested with a repository `.env`; ADR-002 implementation note). If a caller places a store
inside the analysed root, it must pass it to `capture_revision(exclude_paths=...)`. That's tested:
identity never depends on the store's own files.

## 3. JobRunner (Milestone 2.2)

`JobRunner(store).run(spec, job, token=...)` executes one request synchronously in the calling
thread and returns a `JobResult` (`record`, `revision`, `report`, `reused`). The steps are:
capture the revision (the store is passed as `exclude_paths`) → derive the key → take the request
lock → reuse a SUCCEEDED run **without calling the job**, or begin a run, call `job(ctx)` and
finish it. Construction calls `store.recover()`, so a dead owner's run becomes INTERRUPTED first.

### Request identity
`idempotency_key = sha256(canonical JSON of {job_protocol_version, kind, revision_id,
config_fingerprint, engine_version, ruleset_version})`. The scope is already inside `revision_id`.
`config` must be JSON data (string keys, finite floats, ≤ 64 KiB canonical), and key order never
matters. **Budgets are not part of the key**: they only stop work, and a stopped run is PARTIAL,
which is never reused. A limit that shapes a *complete* result (for example a findings cap) must go
in `config`. `JOB_PROTOCOL_VERSION` is in every key, so changing what keys mean never reuses old runs.

### Outcomes
| Situation | State | `failure_class` | Job artifacts kept |
|---|---|---|---|
| job returns, no partial reasons | SUCCEEDED | – | yes |
| job called `ctx.mark_partial(reason)` | PARTIAL | – | yes (labelled partial) |
| wall-time, artifact-count or artifact-bytes budget hit | PARTIAL | `budget_exhausted` | those written before |
| token cancelled (checked at every checkpoint and once more after the job returns) | CANCELLED | – | **no** |
| `JobError(class, msg)` | FAILED | that class | no |
| any other `Exception` | FAILED | `internal_error`; the reason holds the **type only** | no |
| `KeyboardInterrupt`/`SystemExit` | FAILED (recorded, then re-raised) | `interrupted` | no |
| process killed | INTERRUPTED (by the next `recover()`) | – | no |
| revision capture fails or exceeds its budget; an identical request is still running past `max_seconds` | no run; raises `JobError` (`input_error` / `budget_exhausted`, the last one `retryable`) | | |

Every terminal state the runner records carries a `job-report` artifact (a reserved name): state,
failure class, reason, partial reasons (≤ 100, ≤ 200 chars each), `budget_usage` (elapsed seconds,
checkpoints, artifacts, artifact bytes), config fingerprint, and engine and ruleset versions. A
reused run returns its stored report. A SUCCEEDED record without a readable report (not written by
a runner) is recomputed rather than trusted. Discarded artifacts are just unreferenced objects;
`prune()` collects them after the grace period.

### Cancellation and budgets
Both are cooperative. `ctx.checkpoint()` raises an internal `BaseException` subclass, so a job's
own `except Exception` can't swallow a cancellation or a budget stop (checked by a mutation test).
The `CancellationToken` wraps a `threading.Event` and can be cancelled from any thread. The
wall-time deadline starts when `run()` is called, so revision capture and waiting for the request
lock count against it. Capture itself is bounded by its file and byte budgets; it isn't
interrupted. `JobBudget` defaults: `max_seconds=600`, `max_artifacts=64`,
`max_artifact_bytes=512 MiB` (total per run; each artifact is also bounded by the store's
`max_artifact_bytes`).

### Concurrency
`LocalRunStore.request_lock(key, timeout_seconds=...)` maps a key onto one of 256 lock files
(`locks/<first two hex digits>`), held with `flock` (exclusive, polled every 50 ms until the
timeout). The lock files are never deleted, because unlinking a lock that someone may hold would let
two owners in. Striping keeps their number fixed; unrelated keys that share a stripe only wait for
each other. Inside the lock, the runner re-checks `find_succeeded` before running, so identical
requests from 4 threads or 3 processes call the job exactly once (tested). `flock` has the NFS
caveat from §2.

### Reading repository files (Milestone 2.3)
`JobContext.read_file(path, max_bytes=...)` is the only way a job reads repository content. It calls
`read_revision_file`, which opens the path as the capture did (component by component,
`O_NOFOLLOW`) and raises `RevisionEntryChanged` unless the size and sha256 equal the revision
entry. A job therefore never analyses bytes it can't attribute to `ctx.revision`.

### Not built (later milestones, when needed)
A queue and a `queued` state (Phase 4, bounded MCP queue), thread or process pools, an asyncio
wrapper, retries, server-mode dispatch (Celery wraps the same job function, ADR-010), and engine
version discovery (the caller passes `engine_version`).

## 4. Deliberately deferred
`PostgresRunStore`, a hash cache for
repeat captures, a default store location and `patchfrog cache prune` (CLI), streaming artifact
reads beyond `verify_artifact`, Windows support.

## 5. Measured results (host: Linux 7.1, Python 3.12.3, 12 CPUs, local NVMe, ~1 GB free RAM; `scripts/bench_local_store.py`, 2026-11-22)

| Measurement | Result |
|---|---|
| Directory capture, 20,000 files / 28.7 MB, first / repeat | 1.02 s / 1.05 s (identity stable) |
| Git capture of the same tree, clean / one modified file | 1.31 s / 1.18 s (`modified=1`); clean Git id == directory id |
| This repository (1,450 in-scope files, 9.8 MB, dirty) | 0.37 s first / 0.15 s warm cache |
| 1,000 small (2 KiB) puts / the same 1,000 again (dedup) | 3.17 s (≈ 3.2 ms each, dominated by file + directory `fsync`) / 0.02 s |
| 256 MiB streamed put / streaming verify | 361 MiB/s / 1,340 MiB/s |
| 500 runs begin+finish (2 fsynced atomic writes + index link each) | 4.83 s (≈ 9.7 ms per run) |
| `recover()` scan of 500 runs / full `verify()` / prune to the last 10 | 0.02 s / 0.21 s / 0.16 s |
| Peak RSS of the whole benchmark process | 70 MiB |
| 64 MiB streamed put (unit test, `tracemalloc`) | peak traced allocation < 8 MiB (asserted) |

These are single-host measurements, not performance claims. The fsync cost per small write is the
price of the durability ordering above. It's irrelevant for a few artifacts per run, and
batching would be the fix if a later milestone writes thousands per run.
