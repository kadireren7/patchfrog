# Milestone R: Cross-Repo Intelligence Foundation -- Pre-Implementation Audit

Branch: `feat/cross-repo-intelligence`, off `main` @ `ed61f1168ea98d40fe3824321f68c536e105c3c9`
(Milestone Q, Cross-PR Intelligence Foundation, merged).

## 0. Product principle (restated)

Detect proven dependency/contract evidence **across repository
boundaries**: does the current PR change a contract/surface that
another **explicitly-linked** repository depends on? Never "which
repositories look related" -- no repository discovery engine, no
organization crawler, no speculative service graph. Cross-repo overlap
is evidence, never a standalone finding, exactly the same product
boundary Trajectory Intelligence (P) and Cross-PR Intelligence (Q)
already established for their own scopes.

## 1. Audit question: what explicit cross-repo relationship data already exists?

**None.** Grepped the whole `patchfrog/` tree:

- No `RepositoryRelation`-shaped table or model anywhere in
  `patchfrog/persistence/models/`.
- No package-manifest parsing anywhere (`grep` for
  `pyproject.toml`/`package.json`/`requirements.txt`/`manifest` across
  `patchfrog/indexing/` and `patchfrog/context/`: zero hits). PatchFrog
  has never parsed a dependency manifest of any kind.
- Git submodules (gitlinks) are explicitly **skipped** during
  indexing: `patchfrog/indexing/inventory.py` treats a gitlink as
  "non-regular file" and `continue`s past it -- no `.gitmodules`
  parsing, no submodule-to-repository mapping exists or has ever
  existed.
- No internal package name is ever linked to a `repository_id`
  anywhere.

**Conclusion**: sections 2-4's "explicit link only" / "no automatic
discovery" requirements are not just a design preference here -- they
are the *only* option. There is no existing safe signal to build on;
the smallest new explicit mechanism must be introduced from scratch.

## 2. Audit question: are repository dependencies persisted anywhere?

No (see section 1). `RepositoryModel` (`patchfrog/persistence/models/repository.py`)
has no self-referential or cross-repository relationship field of any
kind -- `id`, `github_repository_id`, `owner`, `name`, `full_name`,
`installation_id` (a **soft join**, not a real FK, to
`InstallationModel.github_installation_id` -- see below), `is_selected`,
timestamps. Nothing else.

## 3. Audit question: are package manifests parsed/indexed today?

No (confirmed via grep, section 1). Per spec section 11's own
instruction ("If mapping is not reliable: DEFER"), **`PACKAGE_DEPENDENCY`
relation/signal kind is deferred in v1** -- there is no reliable
package-name-to-repository mapping to build on, and introducing manifest
parsing is new infrastructure well beyond this milestone's "audit what
exists" scope.

## 4. Audit question: are git submodules represented?

No (confirmed via grep, section 1) -- gitlinks are structurally
skipped during indexing, on purpose, with no metadata ever recorded
about what a gitlink points to. **`SUBMODULE` relation kind is deferred
in v1** for the same reason as `PACKAGE_DEPENDENCY`: no existing,
reliable data to build on.

## 5. Audit question: can one repository safely identify another repository dependency without a crawler?

Not today -- there is no existing safe signal (sections 1-4). This
milestone must introduce the smallest new explicit mechanism: an
**operator-registered** relation, never auto-discovered, never inferred
from names/symbols/embeddings/README text. See section 9 for why the
registration path must be operator-only, not repository-config
-controlled.

## 6. Audit question: can current reviewed state of a peer repository be known?

Yes, in principle -- the exact same query shape Cross-PR Intelligence
(Q) already uses (`ReviewGenerationModel` ordered by
`sequence_number`, scoped to a `pull_request_id`) could be re-scoped to
a different `repository_id`'s pull requests. **But this milestone's
only implemented signal kind (section 10) does not need it at all** --
see section 8's design rationale. Kept as a documented, deliberately
*unused* capability for v1 (the `CrossRepoPeer` domain type carries the
fields for forward documentation, exactly mirroring how `TrajectoryHead`/
`CrossPRPeer` reserved fields for kinds that turned out unnecessary),
never wired into the one signal kind this milestone actually
constructs.

## 7. Audit question: can a peer repository's latest reviewed head be identified?

Same answer as section 6 -- technically yes via the same query
mechanism, but not needed and not used by v1's implemented signal.

## 8. Why v1's signal needs no live peer-repository state at all

Cross-PR Intelligence (Q) compares two PRs' *own structural changes*
directly (`ReviewCandidateModel` rows in each PR's own review run) --
it fundamentally needs to read the peer's current, actually-reviewed
source-derived state, because the comparison *is* "did the peer
directly touch this exact symbol."

Cross-Repo Intelligence's only safely-provable v1 signal
(`EXPLICIT_SHARED_CONTRACT`, section 10) is structurally different: the
proof that repository B depends on repository A's contract `X` is the
**relation registration itself** (an operator explicitly recorded "B
consumes A's `payments.capture:v1`"), not a live re-derivation from B's
current source. PatchFrog has no cross-repository indexing
infrastructure at all -- it has never cloned/parsed/indexed a
repository it wasn't asked to review directly, and this milestone does
not introduce that (section 22: "no cross-repo graph," "no cross-repo
call graph"). Reading B's live source to re-verify the dependency would
require exactly the new indexing infrastructure this milestone's own
scope explicitly excludes.

**Conclusion**: v1's peer-eligibility check is entirely: (a) an active
`RepositoryRelation` row exists with the right direction and contract
key, (b) both repositories are authorized under the same installation
(section 11), (c) direction is correct. No peer commit/generation
state is read, checked, or required. This is a narrower, safer state
model than section 20 anticipated needing -- documented explicitly per
that section's own escape hatch ("If repo current-head freshness
cannot be proven: document and support a narrower state model").

## 9. Audit question: can K ContractDelta outputs be persisted/reconstructed cross-repo?

No -- `patchfrog.contract_intelligence.domain.ContractDelta` is
computed in-memory only, once per review run, and never persisted
(grepped `patchfrog/persistence/` for `ContractDelta`: zero hits) --
the exact same limitation N/O/P/Q's own audits already established for
every other Intelligence package's in-memory-only report type.

`ContractDelta.qualified_name`/`file_path` is Contract Intelligence's
own natural identity, but it is **repository-local and means nothing
across a repository boundary** -- repo B has no reason to know or care
about repo A's internal file paths/qualified names. A cross-repo
contract identity must be a *human-assigned, stable string* (e.g.
`"payments.capture:v1"`), never derived from a symbol's own path/name
and never derived by an LLM (spec section 10's own explicit
requirement).

**Fix (the smallest addition needed)**: introduce
`RepositoryContractKeyModel` -- an operator-registered mapping from
`(repository_id, contract_kind, stable_key)` to the exact
`(file_path, qualified_name)` in that repository the key refers to.
This is registered once per contract by whoever administers the
relation (the same trusted path as `RepositoryRelationModel`, section
11) -- never derived automatically, never from an LLM, never from
freeform prose.

## 10. What exact cross-repo signal is provable today?

**`CROSS_REPO_CONTRACT_CHANGE`** (spec section 9's `EXPLICIT_SHARED_CONTRACT`
relation kind, section 28's recommended sole v1 overlap kind) -- and
only this one:

1. The current PR's own Contract Intelligence report (`ContractIntelligenceReport.deltas`,
   reused verbatim, never re-derived) contains a real `ContractDelta`
   at `(file_path, qualified_name)`.
2. A `RepositoryContractKeyModel` row exists for the **current**
   repository at that exact `(file_path, qualified_name)` -- i.e. an
   operator has explicitly registered this exact symbol as `stable_key`
   under some `contract_kind`.
3. An **active** `RepositoryRelationModel` row exists with
   `source_repository_id == current_repository_id`,
   `relation_kind == EXPLICIT_SHARED_CONTRACT`, and
   `external_contract_key == stable_key` (from step 2).
4. The relation's `target_repository_id` (the peer/consumer) is
   authorized: same installation as the current repository (section
   11), and currently selected (`RepositoryModel.is_selected == True`
   for both sides, checked live at query time -- never cached, so
   revocation takes effect on the very next review, section 44).

All four conditions are checked in bounded SQL, mirroring Q's own
corrected, single-query discipline (see
`validation/cross_pr_intelligence/latest-summary.md` section 19) --
see section 17 below for the exact query shape.

## 11. Repository authorization model (same-installation, live-checked)

`RepositoryModel.installation_id` is a **soft join** (a raw
`github_installation_id` int value, not a real foreign key --
predates `installations`, see that model's own docstring) to
`InstallationModel.github_installation_id`. This is already exactly
what "same installation" authorization needs: two repositories are
mutually authorized for cross-repo purposes only when
`repo_a.installation_id == repo_b.installation_id` **and** both have
`is_selected == True` (flipped live by `installation_repositories`
webhook add/remove events -- see that model's own docstring). No new
authorization table is needed; this is a live filter on already
-persisted, already-webhook-maintained state, checked fresh on every
query (never cached), which is exactly what section 44 ("peer access
revocation must take effect immediately") requires for free.

Cross-installation relations are **never** supported in v1 (section 6
of the spec: "same-installation default"). If a future need arises for
cross-installation relations, that would need a new, explicit,
separately-audited trust mechanism -- out of scope here.

## 12. Critical security finding: `.patchfrog.yml` is read from the PR's own untrusted head

`patchfrog/review/config_resolution.py::resolve_repository_review_config`
reads `.patchfrog.yml` **at exactly `commit_sha`** -- i.e. the current
PR's own head commit for a remote review, or the local working tree
for a CLI review. This is by design for `ReviewConfig` (a repository
legitimately controls its own review budget, capped by
`apply_operator_hard_caps` so it can only ever *reduce* cost, never
increase it beyond operator limits).

**This makes `.patchfrog.yml`-based cross-repo relation registration
structurally unsafe** (spec sections 25/26's own hypothetical is a real,
confirmed architectural fact, not a hypothetical): a malicious PR could
modify its own `.patchfrog.yml` at its own head to claim
`cross_repo: relations: [{repository: secret-org/private-repo, ...}]`.
Unlike `ReviewConfig`'s hard-capped fields (which can only ever *reduce*
cost), a repository-relation claim would *expand* what PatchFrog reasons
about across a trust boundary -- there is no equivalent "hard cap" that
makes an expanded-scope claim safe the way `apply_operator_hard_caps`
makes an expanded-budget claim safe.

**Decision**: `RepositoryRelationModel`/`RepositoryContractKeyModel` rows
are **never** read from `.patchfrog.yml`, base-branch config, or any
PR-influenced source of any kind. The only registration path is a new,
trusted-operator-only CLI command
(`python -m patchfrog.cli cross-repo relation add/list/remove`,
`python -m patchfrog.cli cross-repo contract add/list/remove`) writing
directly to the database via the same trusted pattern
`ops installations` already uses (`patchfrog/cli.py`'s
`_run_ops_installations`/`_ops_installations_async` -- a Settings-backed
DB connection, no PR content ever touched). This satisfies section 26's
mandatory rule exactly: a PR under review can never expand PatchFrog's
repository access scope, because the PR is never consulted for this
decision at all.

## 13. Directionality, transitivity, forks, renames (design commitments, verified against the data model)

- **Direction is stored explicitly**: `RepositoryRelationModel.source_repository_id`
  (producer) / `target_repository_id` (consumer). A relation `A -> B`
  is queried only from `A`'s own contract change looking for `B` as
  consumer; it is never used in reverse.
- **No transitive inference**: the query only ever does one join hop
  (current repo's own contract change -> relation row -> peer). `A -> B`
  and `B -> C` never combine into an inferred `A -> C` -- there is no
  code path that would even attempt it (no recursive/transitive query
  exists anywhere in this design).
- **Fork safety**: relations reference `repository_id` (`RepositoryModel.id`,
  the same UUID `Base` primary key used everywhere else in this
  codebase), never `full_name`/`owner`/`name` string matching. A fork
  is a structurally distinct `RepositoryModel` row (different
  `github_repository_id`, hence a different `id`) with no relation row
  of its own -- nothing to inherit.
- **Rename safety**: same reasoning -- `RepositoryModel.id` is
  immutable once created; `full_name` is refreshed on every ingested
  webhook (`RepositoryRepository.upsert`) but the relation's own foreign
  keys never reference it.

## 14. Bounds

`MAX_CROSS_REPO_PEERS = 4`, `MAX_CONTRACTS_PER_PEER = 20` (renamed
`MAX_CROSS_REPO_CONTRACTS_PER_QUERY` in the domain module for clarity --
bounds the relation-row `IN` query, not a per-peer loop, since v1's
signal needs no per-peer secondary query at all), `MAX_CROSS_REPO_SIGNALS = 8`.
All applied in SQL (`LIMIT`), never a Python-side loop over an
unbounded relation set -- mirrors Q's own corrected, single-bounded
-query discipline exactly (`validation/cross_pr_intelligence/latest-summary.md`
section 19).

## 15. Package layout (mirrors Q exactly)

`patchfrog/cross_repo_intelligence/{__init__,domain,queries,matching,service,evidence,telemetry}.py`.
No `story.py`/`summary.py` -- no standalone user-facing section in v1
(spec section 34).

## 16. Persistence

Two new tables (the smallest addition needed, per section 9's finding
that no reusable stable contract identity exists today):

- `repository_relations`: `id`, `source_repository_id` (FK),
  `target_repository_id` (FK), `relation_kind`
  (`RepositoryRelationKind` -- only `EXPLICIT_SHARED_CONTRACT`
  implemented; `PACKAGE_DEPENDENCY`/`SUBMODULE`/`API_CONSUMER`/
  `SCHEMA_CONSUMER`/`MANUAL_EXPLICIT` kept on the enum for forward
  documentation only, per sections 3-4's findings), `external_contract_key`,
  `provenance` (`RepositoryRelationProvenance` -- only `OPERATOR_CLI`
  populated in v1; `PACKAGE_MANIFEST_MAPPING`/`SUBMODULE`/
  `API_CONTRACT_REGISTRATION` reserved), `active`, `created_at`. Unique
  on `(source_repository_id, target_repository_id, relation_kind, external_contract_key)`.
- `repository_contract_keys`: `id`, `repository_id` (FK), `contract_kind`
  (reuses `patchfrog.contract_intelligence.domain.ContractKind`),
  `stable_key`, `file_path`, `qualified_name`, `created_at`. Unique on
  `(repository_id, stable_key)` (one key names exactly one symbol per
  repository) and indexed on `(repository_id, file_path, qualified_name)`
  (the reverse lookup this milestone's own query needs: "does the
  current PR's changed symbol have a registered stable key").

No relation *event log*, no duplicated peer source, no duplicated
diffs, no global graph -- exactly per spec section 49's own
constraints.

## 17. Query shape (mirrors Q's corrected, bounded discipline)

One bounded query: join `RepositoryContractKeyModel` (current repo,
matching the changed `(file_path, qualified_name)`) to
`RepositoryRelationModel` (`source_repository_id == current_repository_id`,
`external_contract_key == stable_key`, `active == True`) to
`RepositoryModel` (the target/peer, `installation_id` matching the
current repository's own, `is_selected == True`) -- `LIMIT MAX_CROSS_REPO_PEERS`
applied in the same statement. Never a query over every relation a
repository has ever had.

## 18. Orchestration integration (reuses Q's/P's exact mechanism)

`ReviewEffortPolicy.decide_provisional` gains `cross_repo_signal_present: bool = False`
-- the same structural-signal + unconditional-mandatory-critic pattern
as `trajectory_signal_present`/`cross_pr_signal_present`. New
`ReviewEffortReason.CROSS_REPO_CONTRACT_IMPACT_PRESENT`. Candidate
dispatch reordering extended to consider all three reports' hints.
`QUALITY_COST_POLICY_VERSION` bumped 3 -> 4 (a third real tiering-signal
addition, same justification class as P's 1->2 and Q's 2->3 bumps).

## 19. Relationship to K/N/O/P/Q/J/L/M

K remains the sole owner of within-repository contract-consumer
findings; R never duplicates or replaces a K finding, only adds
cross-repository *evidence* alongside an already-real candidate. N/O's
historical trust memory remains strictly repository-local -- a
cross-repo relation never implies shared historical trust (spec
section 37). P's trajectory and Q's cross-PR evidence are both
current-PR/current-repository scoped; R is the only package reasoning
about a different `repository_id` entirely, and never reuses Q's own
peer-discovery query (different trust boundary, different identity
model -- spec section 35). J/L/M's own current-repository-only outputs
are unchanged; R never infers peer-repository intent or test evidence
(spec section 39).

## 20. CLI (the trusted registration path)

`python -m patchfrog.cli cross-repo contract add/list/remove` and
`cross-repo relation add/list/remove`, mirroring the exact
`_run_ops_installations`/`_ops_installations_async` pattern already
established for `ops installations` (a `Settings`-backed DB connection
built fresh per invocation, never touching PR content). `relation add`
rejects `source == target` (a repository can never be its own peer via
this path) and resolves repository names via a new
`RepositoryRepository.get_by_full_name`. Manually verified end-to-end
against a real Postgres database during implementation: contract
add/list/remove, relation add/list/remove, the self-relation rejection,
and the unknown-repository error path all behave correctly.

## 21. Telemetry: exclusion-reason counters deliberately omitted

The spec's own telemetry section suggested
`cross_repo_peer_excluded_auth_count`/`cross_repo_peer_excluded_stale_count`.
These are deliberately **not** included: counting *why* a peer was
excluded would require a second, separate query scanning every
relation a repository has (to know what was filtered out), which
directly conflicts with this milestone's own bounded-single-query
discipline (section 17). The five telemetry fields that are included
(`cross_repo_peer_count`, `cross_repo_signal_count`,
`cross_repo_explicit_contract_relation_count`,
`cross_repo_require_critic_count`, `cross_repo_deepen_context_count`)
are all derivable from the one bounded query's own result set.

## 22. Two real bugs found during implementation

1. **Enum-column length overflow**: `ReviewEffortReason.CROSS_REPO_CONTRACT_IMPACT_PRESENT`'s
   first-drafted value (`"cross_repo_contract_impact_present"`, 34
   characters) exceeded the existing `enum_column(ReviewEffortReason,
   length=32)` constraint on `ReviewCandidateModel.effort_reasons`
   (`patchfrog/persistence/models/review.py`) -- surfaced immediately
   as a hard `ValueError` on module import during a real CLI smoke
   test (not by a unit test, since no existing test imports the whole
   persistence-models package under length validation). Fixed by
   shortening the value to `"cross_repo_contract_impact"` (26
   characters); no call site needed updating since every reference is
   symbolic (`ReviewEffortReason.CROSS_REPO_CONTRACT_IMPACT_PRESENT`),
   never the raw string.
2. **`RepositoryRelationRepository.deactivate` reported success on an
   already-inactive relation**: calling `cross-repo relation remove`
   twice in a row both printed "deactivated relation..." and exited 0,
   even though the second call changed nothing. Found via manual CLI
   smoke testing (call it twice, observe both report success). Fixed
   by checking `model.active` before flipping it, returning `False`
   (a real "nothing to do" result) when the relation was already
   inactive -- exercised by
   `test_case_relation_removed_signal_disappears`'s own two-step shape
   indirectly, but the CLI-level idempotency itself was verified
   manually against real Postgres rather than by an automated test (no
   established pattern exists in this codebase for testing the
   `Settings`-backed CLI async helpers directly -- see section 20).

## 23. Explicitly not started

OpenAI provider, Model Router, Agent Handoff, Merge Readiness,
Cloud/dashboard work, and Milestone S (Cross-Repo's own next
follow-on, whatever it turns out to be). Implementation, gates, and
verification are now complete.
