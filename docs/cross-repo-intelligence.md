# Cross-Repo Intelligence Foundation

`patchfrog/cross_repo_intelligence/` answers a question none of the
prior Intelligence packages could: **"does another, explicitly-linked
repository depend on a contract the current PR just changed?"**

**PatchFrog never discovers cross-repository relationships from
similarity.** Cross-Repo Intelligence runs only across repositories
whose technical relationship has been explicitly established through a
trusted source. No relationship is ever inferred from repository
names, shared organization, shared language, matching directory names,
similar symbols, package-name coincidence, GitHub topics, embeddings,
or README text similarity.

**Cross-repo dependency evidence is not a finding.** It may only ever
deepen existing review mechanisms for a candidate that already exists:
require critic verification, contribute toward a higher effort tier.
It never says "this will break repo B" -- it says, at most, "use this
only as evidence that the current change may have external
compatibility impact; verify the current code independently."

## Explicit relationships only, registered by a trusted operator path

Before this milestone, PatchFrog had **no** cross-repository
relationship data of any kind: no relation table, no package-manifest
parsing anywhere, no submodule metadata (git submodules are explicitly
skipped during indexing). The smallest new mechanism was introduced
from scratch: `RepositoryRelationModel` (a directional, explicit
producer -> consumer dependency) and `RepositoryContractKeyModel` (an
operator-assigned, stable, cross-repository-meaningful name for one
exact symbol -- e.g. `"payments.capture:v1"`).

**Critical security finding**: `patchfrog/review/config_resolution.py::resolve_repository_review_config`
reads `.patchfrog.yml` at exactly the PR's own head commit. Unlike
`ReviewConfig`'s hard-capped fields (which can only ever *reduce*
cost), a repository-relation claim in `.patchfrog.yml` would *expand*
what PatchFrog reasons about across a trust boundary -- there is no
equivalent hard cap that makes an expanded-scope claim safe. **Neither
table is ever populated from `.patchfrog.yml`, base-branch config, or
any other PR-influenced source.** The only registration path is the
trusted-operator-only CLI:

```
python -m patchfrog.cli cross-repo contract add --repository org/a \
  --contract-kind function --stable-key payments.capture:v1 \
  --file-path patchfrog/billing/capture.py --qualified-name capture_payment

python -m patchfrog.cli cross-repo relation add --source org/a --target org/b \
  --contract-key payments.capture:v1
```

A PR under review has no path to expand PatchFrog's repository access
scope -- `build_cross_repo_intelligence_report`'s only inputs are
`repository_id` (already-authenticated webhook/CLI context) and
`contract_deltas` (Contract Intelligence's own already-computed,
current-repository-scoped output). There is no parameter through which
a PR's own config could ever inject a relation claim.

## Authorization: same installation, live-checked

`RepositoryModel.installation_id` (a soft join, not a real FK, to
`InstallationModel.github_installation_id`) is already exactly what
"same installation" authorization needs. A peer is authorized only
when it shares the current repository's own `installation_id` **and**
is currently `is_selected == True` -- checked fresh on every query,
never cached, so access revocation (an `installation_repositories`
"removed" webhook event) takes effect on the very next review with no
separate retire lifecycle needed. Cross-installation relations are
never supported in v1.

## Why v1's signal needs no live peer-repository state at all

Cross-PR Intelligence compares two PRs' own structural changes
directly, so it must read the peer's actual reviewed source state.
Cross-Repo Intelligence's only signal kind (`CROSS_REPO_CONTRACT_CHANGE`)
is structurally different: the proof that repository B depends on
repository A's contract is the **relation registration itself**, not a
live re-derivation from B's current source. PatchFrog has no
cross-repository indexing infrastructure at all, and this milestone
does not introduce one -- `CrossRepoPeer` carries no peer-review-state
fields (no latest reviewed head, no review run id).

## The bounded query

One SQL statement: the current repository's own registered contract
keys matching the current PR's own changed `(file_path,
qualified_name)` surfaces (from Contract Intelligence's own
`ContractDelta`s, reused verbatim) are joined to active
`EXPLICIT_SHARED_CONTRACT` relations naming the current repository as
producer, joined to the peer repository -- authorized only under the
same installation and still selected. `ORDER BY`/`LIMIT
MAX_CROSS_REPO_PEERS` applied in the same query, before any row
reaches Python. Never a query over every relation a repository has
ever had.

## Directionality, transitivity, forks, renames

- **Direction matters**: a relation `A -> B` (A produces, B consumes)
  is queried only from A's own contract change looking for B as
  consumer -- never used in reverse.
- **No transitive inference**: `A -> B` and `B -> C` never combine
  into an inferred `A -> C`. There is no recursive query anywhere in
  this design -- one join hop, always.
- **Fork safety**: relations reference `RepositoryModel.id` (an
  immutable UUID), never `full_name`. A fork is a structurally
  distinct row with no relation of its own.
- **Rename safety**: same reasoning -- `full_name` is refreshed on
  every ingested webhook, but a relation's foreign keys never
  reference it.

## Supported and deferred kinds

**Only `EXPLICIT_SHARED_CONTRACT`** (relation kind) /
**`CROSS_REPO_CONTRACT_CHANGE`** (signal kind) are implemented in v1.
`PACKAGE_DEPENDENCY`/`SUBMODULE` relation kinds and
`CROSS_REPO_PACKAGE_DEPENDENCY_IMPACT` (signal kind) are deferred --
PatchFrog parses no package manifests and no submodule metadata
anywhere today, so there is no reliable mapping to build on. A single
real, explicit dependency is sufficient to produce a signal -- an
operator-registered relationship is never noise at N=1.

## No standalone publication, no user-facing copy beyond prompt evidence

No Change Story addendum, no summary block. The only user-facing
footprint is a bounded `<cross_repo_intelligence>` prompt section
attached to the exact candidate whose surface has a real overlap --
neutral wording only, never a conclusion, never naming an author.

## Orchestration integration (reuses Cross-PR/Trajectory Intelligence's exact mechanism)

A new optional `cross_repo_signal_present: bool` parameter on
`ReviewEffortPolicy.decide_provisional`: contributes
`ReviewEffortReason.CROSS_REPO_CONTRACT_IMPACT_PRESENT` to the existing
signal count, and unconditionally raises `critic_expectation` to
`CriticExpectation.MANDATORY` for that exact candidate.
`CrossRepoReviewHint.REQUIRE_CRITIC` is the only hint v1's single
signal kind ever selects. A candidate whose surface selects
`REQUIRE_CRITIC` from *any* of Trajectory/Cross-PR/Cross-Repo
Intelligence is reordered to the front of the candidate dispatch list.
`CriticExpectation` is a single decision state, not additive -- three
signals present at once never trigger three critic calls.

`QUALITY_COST_POLICY_VERSION` bumped 3 -> 4 -- a third real
tiering-policy semantics change, the same class of change Trajectory's
1 -> 2 and Cross-PR's 2 -> 3 bumps were.

## Relationship to K/N/O/P/Q/J/L/M

K remains the sole owner of within-repository contract-consumer
findings; R never duplicates or replaces a K finding, only adds
cross-repository evidence alongside an already-real candidate. N/O's
historical trust memory remains strictly repository-local -- a
cross-repo relation never implies shared historical trust. Cross-Repo
Intelligence never reuses Cross-PR Intelligence's own peer-discovery
logic (different trust boundary, different identity model). J/L/M's
own current-repository-only outputs are unchanged.

## Persistence

Two new tables (`repository_contract_keys`, `repository_relations`)
plus six nullable-default count columns on `review_runs` (migration
`0027_cross_repo_intelligence`). No relation event log, no duplicated
peer source, no duplicated diffs, no global graph. Counts only in
telemetry -- no repository full_name/id, no contract key string, no
organization name anywhere.

## Limitations

- Only `EXPLICIT_SHARED_CONTRACT`/`CROSS_REPO_CONTRACT_CHANGE` are
  implemented; package-dependency and submodule relation kinds are
  deferred until PatchFrog parses manifests/submodule metadata
  reliably.
- `PullRequestModel.state`-style peer freshness for *cross-repository*
  peers doesn't exist in v1 by design -- the relation registration
  itself is the evidence, not a live re-derivation from peer source.
- PatchFrog's persisted repository/relation state is only as current
  as the operator keeps it (relations are never auto-discovered or
  auto-updated) and as current as successfully ingested GitHub webhook
  state (for the underlying `is_selected`/`installation_id` checks).
- Cross-repo dependency evidence is orchestration evidence, not proof
  -- it can only ever deepen scrutiny of an already-real candidate; it
  never manufactures one, and it never tells the reviewer which
  repository is "right."
