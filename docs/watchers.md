# Upstream watchers (M11, engine side)

PatchFrog should not need a human to feed it every API change. A **watcher** turns "something upstream
may have changed" into at most one `ExternalChangeEvent` — the same event M6 builds from manual input —
so a detected change flows through exactly the pipeline a hand-fed one does:

```
source ─ fetch ─► normalized snapshot ─ compare with cursor ─► candidate change (M6 event, fingerprinted)
       ─► launch gate ─► M10 campaign ─► M7 migration ─► M8 verification ─► M9 decision ─► PR / report
```

Package `patchfrog/watchers/` (`WATCHER_ENGINE_VERSION = 1`) holds **detection, normalization and diff
intelligence**. *When* to poll, retries across workers, cursor storage and fan-out to workspaces belong to
the hosting service (PatchFrog Cloud, or your own scheduler). See `docs/product-boundary.md`.

## Sources (M11.2)

| Kind | Reads | Evidence |
|---|---|---|
| `pypi` | `https://pypi.org/pypi/<name>/json` | latest non-yanked, non-prerelease version |
| `npm` | `https://registry.npmjs.org/<name>` | `dist-tags.latest` (not deprecated) |
| `github_releases` | `GET /repos/<o>/<r>/releases` | latest published, non-draft, non-prerelease tag |
| `openapi_url` | an OpenAPI/Swagger document at an https URL | a normalized, fingerprinted contract |
| `changelog_feed` | Atom, RSS or JSON Feed | an entry whose title carries a version |
| `manual` | a snapshot the caller submits | any contract document |

Documented APIs only. **No HTML scraping.** A changelog sentence without a version is not a signal. The
XML parser rejects DTDs/entities. Remote `$ref`s in a fetched OpenAPI document are not resolved.

## Interface (M11.3)

`WatcherSource` (kind, locator, the `DependencyTarget` a change refers to) → an adapter's `fetch` →
`WatcherSnapshot` → `detect_change(source, snapshot, cursor)` → `WatchOutcome` (`BASELINE`, `UNCHANGED`,
`CHANGED`, `DUPLICATE_SUPPRESSED`, `SKIPPED`) carrying the new `WatcherCursor` and, for `CHANGED`, a
`DetectedUpstreamChange`. The cursor is a small JSON document (`to_json`/`from_json`) a host stores opaquely.

* **First observation is a baseline** and emits nothing: a watcher never replays history it was not watching.
* A version decrease (yank/rollback) never regresses the cursor. Unparseable versions are skipped, not guessed.
* The emitted event's fingerprint is the M6 fingerprint; the cursor remembers the last 50 it emitted, and a
  host should also enforce uniqueness on it — the same change seen twice is one change.
* A stored previous contract larger than 256 KB is dropped from the cursor and the loss is reported, so a
  later change cannot silently degrade into a version-only comparison.

## The launch gate (M11.5/M11.11)

A change opens a campaign only if it carries evidence of impact: by default `BREAKING`, i.e. **structural
proof** (an OpenAPI or SDK-surface diff). A bare version bump is `REVIEW_REQUIRED` at most and is recorded
without opening a campaign unless a workspace lowers the threshold (and even then a version number alone only
ever yields `UNKNOWN`, never a defect or a patch). Consequence for beta: package-registry and release watchers
produce *events*; campaigns come from sources that carry a contract (OpenAPI URL, manual SDK-surface
submission, or a provider adapter supplying structure).

## The watch registry (M11.1)

`required_watchers` derives what to watch from the dependency identities workspaces actually use — a
dependency with a PyPI/npm package gets a registry watcher; GitHub releases, OpenAPI URLs and feeds are
configured explicitly. Sources de-duplicate by `WatcherSource.key`, so fifty workspaces using one package
cause one fetch; the result fans out and each workspace evaluates it against its own evidence.

## Outbound safety

`validate_public_url` is the gate every fetch passes: https only, no credentials in the URL, a small port
allow-list, no `localhost`/`.internal`/`.local`, and every resolved address must be public (no loopback,
link-local, private, multicast); the real fetcher revalidates every redirect hop, caps response size, and
sends an auth header only to the exact host it was registered for. **Limitation:** a hostile DNS server could
answer differently between validation and connection; also restrict the watcher process's egress at the network
layer.

## Cost

Everything here is deterministic. A watcher never imports a model provider (structurally tested); a detected
change with no consumer produces `NOT_AFFECTED`, not a finding.
