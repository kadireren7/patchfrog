# Upstream Change Detection + Consumer Impact / Blast Radius (M6)

`patchfrog/upstream/` answers: **when an external API/SDK we depend on
changes, exactly what broke, which of our own repositories use the
affected surface, and how far does the impact reach?** It is the second
stage of the product invariant **change -> impact -> evidence ->
verification -> decision**, built entirely on M5's dependency domain,
registry and code-graph primitives (`docs/dependency-discovery.md`) --
no parallel dependency/contract model.

Everything here is deterministic and offline: contract diffing, consumer
matching and blast-radius walks are pure functions over already-collected
evidence (a contract pair, an SDK surface pair, or the M5 registry).
There is no network fetch of specs/releases and no provider call --
`UPSTREAM_CHANGE_VERSION = 1`.

```bash
# Diff two contracts directly
python -m patchfrog.cli changes diff old.yaml new.yaml --hints hints.yaml

# Analyze the impact against one or more local checkouts
python -m patchfrog.cli changes analyze --old old.yaml --new new.yaml \
  --hints hints.yaml --repo demo=path/to/repo --persist --full-name org/demo

# Analyze against every repository the M5 registry already knows about
python -m patchfrog.cli changes analyze --package stripe --ecosystem pypi \
  --to-version 12.0.0 --registry --persist
```

## Change model (`domain.py`)

`ExternalChangeEvent` -- `ExternalChangeKind` (contract_diff /
version_update), `ExternalChangeSource` (contract pair, package version,
GitHub release, registry snapshot), a `DependencyTarget`, an ordered
tuple of `ContractDiffItem` (`DiffItemKind` x `DiffSubject` --
endpoint/method/parameter/request-body/response/auth/component/SDK
symbol/module), and a `ChangeClassification` (`ChangeRisk`: SAFE /
LOW_RISK / REVIEW_REQUIRED / BREAKING, each diff item's own
`CompatibilityClass`: NON_BREAKING / POTENTIALLY_BREAKING / BREAKING /
UNKNOWN, plus machine-readable reason codes). A stable fingerprint
(observed_at excluded) makes the same real-world change idempotent
however it is re-detected.

## Detecting a change (`openapi_diff.py`, `sdk_surface.py`, `package_version.py`, `events.py`)

- **OpenAPI diff**: deterministic, consumer-direction-aware diff over
  M5-normalized contracts (paths/methods, parameters incl.
  required/schema, request bodies, responses, security, security
  schemes, component property shapes). A schema used only in responses
  and one used only in requests classify differently -- a widened
  response is safe, a widened request is not.
- **SDK surface diff**: a small provider-agnostic document
  (`patchfrog_sdk_surface: 1`: symbols + params + modules) and its diff,
  for SDKs with no OpenAPI contract.
- **Version diff**: semver-ish comparison; a version bump alone is never
  proof of breakage on its own (major bump -> POTENTIALLY_BREAKING only).
- `events.py` builds an `ExternalChangeEvent` from any of: two contract
  files, a version pair (with registry/discovery context), an SDK
  surface pair, or release metadata (title/body scanned for known
  breaking-change phrasing, never inferred beyond that).

## Change hints (`hints.py`)

`patchfrog_change_hints: 1` -- explicit symbol/parameter/module/endpoint
renames, enum-value replacements, required-parameter value sources, and
operation-to-SDK-symbol bridges. This is the **only** way a rename or
replacement is ever treated as known; without a hint, PatchFrog reports
"removed + added," never guesses a rename. A hinted literal value is
checked against `HintError` if it looks credential-shaped (`sk-...`,
`ghp_...`, an AWS access key, etc.) -- a hint file can never smuggle a
secret into a plan or patch.

## Consumer mapping + blast radius (`consumers.py`, `code_graph.py`, `blast_radius.py`)

`consumers.py` matches each diff item against a repository's M5 usage
sites by the same evidence kind the diff item is about (exact SDK-call
chain, HTTP path+method, schema reference, import/module, auth,
package-level) -- **an unmatched usage site is reported as ignored, never
as affected.** Matched sites are walked into `code_graph.py`'s existing
graph primitives (parser registry + `RepositoryResolver` +
`infer_test_relationships`, DB-free) to produce `blast_radius.py`'s
DIRECT / TRANSITIVE / POTENTIAL edges, bounded depth, a confidence per
edge, and related tests/modules. The parser registry covers Python/C/C++
with a real caller graph; JS/TS gets lexical usage sites only (no caller
graph yet) -- blast radius says so explicitly rather than silently
under-reporting.

## Multi-repository impact (`workspace.py`)

`WorkspaceImpact` aggregates one change across every repository given
(`--repo`, repeatable) or every repository the M5 registry knows about
(`--registry`) into affected / unaffected / uncertain sets. Cross-Repo
Intelligence (R) is operator-registered PR-to-PR contract relations and
has no notion of external dependencies; the trusted evidence for "which
repositories consume dependency X" here is the M5 registry itself (every
row came from real discovery evidence), so `--registry` is
registry-backed, never name-inferred.

## Persistence (`store.py`, migration `0035_upstream_change_migration`)

`external_change_events`, `external_change_diff_items`,
`external_change_impacts` -- idempotent on the event/diff-item/impact
fingerprint (re-running the same analysis is a no-op write, not a
duplicate row). Full normalized contracts are not duplicated here; M5's
`external_contract_snapshots` already owns them, referenced by
fingerprint.

## Deliberately out of scope

Continuous polling/watchers for new upstream releases (M11, not this
milestone), network fetches of specs/releases (a caller supplies
contract/release data; this package never calls out), migration planning
and patch generation (M7, `docs/migration.md`), executable/runtime
verification of a migration (M8), and opening a PR (M9).
