# PatchFrog Product Architecture

> PatchFrog keeps software compatible with the APIs and SDKs it depends
> on. It detects upstream changes, maps their impact across codebases,
> generates migrations, verifies them, and opens evidence-backed pull
> requests.

Permanent invariant: **change → impact → evidence → verification →
decision.** A signal that does not independently prove something never
becomes a standalone conclusion; deterministic evidence comes first and
provider (LLM) work only follows when it is justified.

## Pipeline

```
 repository
    │
    ▼
 [M5] External dependency discovery          patchfrog/dependencies          IMPLEMENTED
    │   manifests, lockfiles, imports, SDK call chains, endpoint hosts,
    │   env-var NAMES, local OpenAPI specs  (never secret values)
    ▼
 [M5] Contract registry + dependency graph   external_dependencies,           IMPLEMENTED
    │   usage sites, normalized contract      external_contract_snapshots,
    │   fingerprints with history             patchfrog/dependencies/graph
    ▼
 [M6] Upstream change detection              (future)                          NOT YET
    │   new SDK versions / spec revisions diffed against the registry
    ▼
 [M6] Consumer impact / blast radius         (future; reuses the code graph,   NOT YET
    │   usage site → symbol → callers          Contract Intelligence K,
    │                                          Cross-Repo Intelligence R)
    ▼
 [M7+] Migration generation                  (future)                          NOT YET
    ▼
 [M7+] Verification                          executable_verification,          PARTLY EXISTS
    │   targeted tests, fix verification,     fix_verification, review engine
    │   cost-aware review as a safety layer   (M4)
    ▼
 [M7+] Evidence-backed pull request          publishing, merge_readiness       PARTLY EXISTS
```

## Where existing capabilities fit

| Capability | Role now |
|---|---|
| Review engine (`patchfrog/review`) | **Secondary safety/verification layer.** Kept intact; M4 made it cheap (risk tiers, zero-call paths, single pass, per-tier budgets — `docs/cost-aware-review.md`) so it can run inside migration workflows. |
| Change/risk classifier (`patchfrog/change_risk`) | Pure diff classifier; sizes review effort today and will size migration verification. |
| Repository index + graph (`indexing`, `parsing`, `intelligence`) | Symbol identities and caller/callee edges the dependency graph plugs into. |
| Intelligence layers J–R | Deterministic evidence; K (contracts) and R (cross-repo) are the natural consumers-impact building blocks for M6. |
| Executable/fix verification (S, T) | Verification step for generated migrations. |
| Publishing, checks, merge readiness (V) | Decision and PR surface. |

Generic PR review remains available (`PATCHFROG_REVIEW_STRATEGY`,
`cost_aware` by default, `specialist_fanout` for the pre-M4 shape); it is
no longer the product's center.

## Boundaries

Everything above — including dependency discovery, the contract registry,
risk classification and cost policy — determines how PatchFrog
understands and verifies software, so it lives in this source-available
engine (ELv2). Hosted lifecycle/business operations (accounts, billing,
hosted credentials, scheduling of hosted discovery runs) belong to the
private Cloud control plane — see `docs/product-boundary.md`. M4/M5
required no Cloud change.
