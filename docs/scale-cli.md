# `patchfrog scale`: local application discovery

`patchfrog scale` statically discovers the structure of a local Python web application: FastAPI
apps, routes and their dependencies, and SQLAlchemy/SQLModel models, relationships, engines,
sessions and session providers. It is the first user-facing part of the scalability engine
(Milestone 2.5).

**What it is not (yet):** it produces no findings, no performance scores and no capacity
estimates. Detection rules (N+1 queries, blocking I/O in async routes, unbounded queries) arrive in
a later phase. Everything it reports is structure that it found in your source code. What it
couldn't determine is listed explicitly, never guessed.

## Requirements and safety
- Python 3.12 and a PatchFrog checkout or installation (`pip install -e .`).
- **No services:** no PostgreSQL, Redis, Celery, GitHub credentials, AI provider keys or network
  access. `DATABASE_URL` and the other server settings are not read.
- **Your code is never executed.** Discovery parses source files; it never imports them, never
  runs `setup.py`, `conftest.py`, migrations or startup hooks, and never connects to a database.
  Git is used only to list files, with hooks and fsmonitor disabled. Symlinks are not followed,
  and every byte analysed is checked against the snapshot it is reported for.
- **Nothing from the repository configures PatchFrog.** In particular, a `.env` file in the
  analysed repository can't change where results are written.

## Quick start
```sh
python -m patchfrog scale analyze path/to/your/app
python -m patchfrog scale show <run_id>
```
`python -m patchfrog.cli scale ...` works too, but loads the whole review/compatibility CLI first
(about 3.5 s of startup instead of 0.15 s, measured below).

Example output (the `tests/fixtures/scalability/database/cases/05_sessions_sync` fixture, verbatim):
```
PatchFrog application discovery  run 60f2c9b824154d0891ed0e4173a639e1  [succeeded]
Revision   d8d2c76ca6b08881  (directory; 3 files)
Frameworks fastapi (imported only, version unknown), sqlalchemy (imported only, version unknown)
Web        1 app(s), 0 router(s), 5 route(s) (0 async, 5 sync, 0 unknown)
Database   0 model(s), 0 relationship(s), 1 engine(s), 1 session factory/session object(s), 3 session provider(s), 4 route(s) reach a session
Coverage   complete (2 of 3 Python files parsed; the rest have no routes or models)

Show again: python -m patchfrog scale show 60f2c9b824154d0891ed0e4173a639e1
Static structure only: no performance findings yet (detection rules arrive in a later phase).
```
In a Git checkout the revision line also shows the commit and uncommitted changes, for example
`(git <commit>, uncommitted changes: 1 modified, 0 added, 0 deleted; 3 files)`.
When something can't be resolved, the summary says `Coverage   NOT COMPLETE` and lists each
unresolved reference with its file, line and reason, for example
`app/main.py:12  include_target: r  (loop variable)`.

## Commands
### `scale analyze [PATH]`
Analyses `PATH` (default: the current directory) and stores the result as an immutable run.
- **Run reuse:** an identical request (same file contents and scope, same limits, same PatchFrog
  version) reuses the stored run instead of re-analysing; the summary says `reused`. Editing,
  adding or removing a file changes the snapshot, so the next run is new.
- **Uncommitted changes count:** the snapshot is what is on disk, including uncommitted edits.
  The commit and its uncommitted changes are shown as provenance only.

| Option | Meaning |
|---|---|
| `--json` | print the run and the full discovery model as JSON (below) |
| `--details` | list every route, model (with relationships and loading strategy), session provider, unresolved reference and limitation |
| `--strict` | exit 3 unless discovery coverage is complete (useful in CI) |
| `--store DIR` | run store directory (see *Store location*) |
| `--exclude PREFIX` | leave a repository-relative path prefix out of the snapshot (repeatable), e.g. `--exclude tests/fixtures` |
| `--tracked-only` | Git repositories: ignore untracked files |
| `--max-files N` | analyse at most N Python files (default 5,000; beyond it the run is PARTIAL) |
| `--max-seconds S` | wall-time budget (default 600; when exceeded the run is PARTIAL) |

### `scale show RUN_ID`
Shows a stored run. `--json` and `--details` work as for `analyze`. The run id must be the 32
hex characters that `analyze` printed. `show` refuses to present corrupt data: a tampered or
truncated artifact, a record of another kind, or an artifact of an unsupported (newer) schema
version is an error (exit 6). A run that is interrupted, failed, cancelled or still running has
no result, and `show` says so (exit 4).

### `scale schema`
Prints the JSON Schema of the discovery model (`docs/schemas/application-model.v2.schema.json`).

## Exit codes
| Code | Meaning |
|---:|---|
| 0 | run succeeded (with `--strict`: and coverage is complete) |
| 1 | unexpected internal error |
| 2 | usage or input error: path missing or not a directory, invalid run id, unusable store |
| 3 | run **partial**: a budget or limit stopped work, or files were skipped (too large, over the byte budget, or changed during analysis). With `--strict`, also a succeeded run whose coverage is incomplete |
| 4 | the run failed, was cancelled or interrupted, or is still running: no result |
| 5 | run not found |
| 6 | corrupt run or artifact, a run of another kind, or an unsupported schema version |
| 130 | interrupted with Ctrl-C (the run is recorded as failed: interrupted) |

**Success versus completeness.** A *succeeded* run analysed everything it was given. Its
*coverage* can still be incomplete because of code that static analysis can't resolve (dynamic
registrations, computed paths, unsupported patterns). That is a property of the code, so the
result is stored and reused. A *partial* run was cut short by a limit, so it is never reused and
always exits 3.

## JSON output
`--json` prints one object:

| Field | Content |
|---|---|
| `format` | `"patchfrog.scale.run"` |
| `format_version` | `1` |
| `run` | `run_id`, `state` (`succeeded`, `partial`, `failed`, `cancelled`, `interrupted`, `running`), `reason`, `reused`, timestamps, `engine_version`, `ruleset_version`, `failure_class`, `partial_reasons`, `budget_usage`, `store` |
| `revision` | `revision_id`; from `analyze` also the kind, commit, dirty state and change counts |
| `complete` | `true` only when the run succeeded **and** discovery coverage is complete. It is never true for a partial, failed or model-less run |
| `model` | the stored `ApplicationModel` (`discovery.json`), or `null` |

`model` follows the [JSON Schema](schemas/application-model.v2.schema.json) (draft 2020-12,
generated from the typed model; see `docs/architecture/DISCOVERY.md` §10). The model covers:
- frameworks, with declared versus locked versions (a version range is never reported as
  installed);
- routers, include edges and routes, with sync/async endpoints and effective `Depends`;
- dependency functions;
- the `database` section: models, columns, relationships and their `lazy` strategy with
  provenance, engines, session factories, session providers, and which routes reach a session;
- unresolved references, limitations and coverage.

## Store location
Runs are stored under the first of:
1. `--store DIR`;
2. the `PATCHFROG_STORE_DIR` environment variable;
3. `$XDG_CACHE_HOME/patchfrog/store`;
4. `~/.cache/patchfrog/store`.

These are read from the real environment only, never from a `.env` file. The store is created
with private permissions (`0700`) and must be on a local filesystem (it uses `flock`, which isn't
reliable on NFS). If you put it inside the analysed directory, it is excluded from the snapshot
automatically. Old runs can be pruned with the `LocalRunStore.prune` API; a CLI command for that
comes later.

## What is discovered, and what isn't
Supported and unsupported patterns are listed precisely in
[`docs/architecture/DISCOVERY.md`](architecture/DISCOVERY.md):
- §3 and §4: FastAPI;
- §2: frameworks and versions;
- §9: SQLAlchemy and SQLModel.

In short:
- **Discovered:** FastAPI apps and routers (also from app factories), routes with methods, full
  paths, endpoints and `Depends`; SQLAlchemy 2.x and legacy declarative models, SQLModel table
  models, columns, primary keys, relationships, lazy-loading configuration, inheritance, engines,
  session factories and session providers.
- **Reported, not guessed:** dynamic registrations, computed paths and prefixes, ambiguous
  imports, `declared_attr`, `column_property`, hybrid attributes, `type(...)` models, registry and
  imperative mapping, reflected tables, mounts.
- **Not analysed yet:** what endpoints *do* with a session (queries, loops), request-path call
  graphs, other web frameworks (Django, Flask), and runtime behaviour of any kind.

## Measurements (one developer host, 2026-11-23)
Measured with `scripts/bench_scale_cli.py --repo .` on Linux 7.1, Python 3.12.3, 12 CPUs, local
NVMe, with about 0.8 GB of free memory, so times are noisy. These are measurements, not
performance targets.

| Measurement | Result |
|---|---|
| Startup, `python -m patchfrog scale --help` | 0.15 s (median of 5) |
| Startup, `python -m patchfrog.cli scale --help` | 3.50 s (median of 5; the full CLI imports everything eagerly, ROADMAP S3) |
| Small app (3 files): first / repeated analysis | 0.22 s / 0.17 s; peak RSS 25 MiB; artifact 5.6 KiB |
| This repository (1,145 Python files): first / repeated | 3.32 s / 0.34 s (reused); peak RSS 156 MiB / 31 MiB; artifact 423 KiB |
| Same, `--exclude tests/fixtures` | 2.84 s / 0.33 s; peak RSS 152 MiB; artifact 306 KiB |
