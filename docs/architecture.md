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
 [M6] Upstream change detection              patchfrog/upstream                IMPLEMENTED
    │   new SDK versions / spec revisions diffed against the registry
    ▼
 [M6] Consumer impact / blast radius         patchfrog/upstream                IMPLEMENTED
    │   usage site → symbol → callers          (reuses the code graph;
    │                                          Python/C/C++ caller graph,
    │                                          JS/TS usage sites only)
    ▼
 [M7] Migration planning + generated patch   patchfrog/migration               IMPLEMENTED
    │   deterministic rewrite + safety gates,  (structural verification only;
    │   optional narrow model-assisted path    no executable verification yet)
    ▼
 [M8] Executable/runtime verification        executable_verification,          PARTLY EXISTS
    │   that a generated patch fixes the       fix_verification, review engine
    │   break -- not yet wired to M7 patches   (cost-aware review, M4)
    ▼
 [M9] Evidence-backed pull request           publishing, merge_readiness       PARTLY EXISTS
```

## Where existing capabilities fit

| Capability | Role now |
|---|---|
| Review engine (`patchfrog/review`) | **Secondary safety/verification layer.** Kept intact; M4 made it cheap (risk tiers, zero-call paths, single pass, per-tier budgets — `docs/cost-aware-review.md`) so it can run inside migration workflows. |
| Change/risk classifier (`patchfrog/change_risk`) | Pure diff classifier; sizes review effort today and will size migration verification. |
| Repository index + graph (`indexing`, `parsing`, `intelligence`) | Symbol identities and caller/callee edges the dependency graph plugs into. |
| Intelligence layers J–R | Deterministic evidence, used by normal PR review; M6 consumer-impact matching reuses the same code-graph primitives independently, not these layers directly. |
| Executable/fix verification (S, T) | Not yet wired to M7-generated patches; that integration is M8. |
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
