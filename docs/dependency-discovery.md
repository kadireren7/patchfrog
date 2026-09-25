# External Dependency Discovery + Contract Registry (M5)

`patchfrog/dependencies/` answers, for one repository: **which external
APIs and SDKs does this code depend on, at which version, where exactly
is each one used, and what does its contract look like right now?** It
is the seed for M6 (upstream change detection + consumer impact), which
is **not implemented yet**.

Everything here is deterministic, offline and read-only: no network, no
provider calls, no LLM.

```bash
python -m patchfrog.cli dependencies discover path/to/repo            # human report
python -m patchfrog.cli dependencies discover path/to/repo --json     # machine-readable
python -m patchfrog.cli dependencies discover path/to/repo --persist --full-name owner/repo
```

## What is detected

| Class | Evidence |
|---|---|
| OpenAI SDK | `openai` package (PyPI/npm/Go), `from openai import OpenAI`, `client.chat.completions.create(...)`, `api.openai.com`, `OPENAI_*` variable names |
| Stripe SDK | `stripe` / `@stripe/stripe-js`, `import Stripe from "stripe"`, `stripe.checkout.sessions.create(...)`, `api.stripe.com`, `STRIPE_*` names |
| GitHub API/SDK | PyGithub/githubkit/ghapi, `@octokit/*`, `api.github.com` REST calls, `GITHUB_*`/`GH_*` names |
| OpenAPI contracts | local `openapi.*`/`swagger.*`/`*.openapi.*` or any YAML/JSON declaring `openapi: 3.x`/`swagger: "2.0"`; path-literal references; calls to the spec's server hosts; OpenAPI-Generator client markers |
| Generic packages | every declared package in `requirements*.txt`, `pyproject.toml` (PEP 621 + Poetry), `package.json`, `go.mod`, with resolved versions from `poetry.lock`, `uv.lock`, `Pipfile.lock`, `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, and imports of the package |

Sources are scanned once: Python with the standard-library `ast`
(imports, names bound to them, constructor assignments, attribute call
chains, `os.environ[...]`/`getenv(...)` names, URL literals); JS/TS with
a string-aware lexical scanner (explicit `import`/`require` only).

**Environment variable names are evidence, values never are.** A name
alone only *corroborates* — a lone `GITHUB_TOKEN` never creates a GitHub
dependency. Values are never read: files that look like secret stores
(`.env*`, private keys, `*secret*`/`*credential*` files, `.npmrc`,
`.pypirc`, `.netrc`) are never opened, only counted; in a git checkout
only tracked files are considered; literal defaults such as
`getenv("X", "…")` are never captured; URLs keep the host (plus a
sanitized, token-redacted path for known API hosts), never userinfo or
query strings; URL-shaped dependency specs are reduced to `url:<host>`.

False-positive guards: comments/prose/strings are not imports; module
roots must match exactly (`myapp.openai_utils` is not `openai`); a
repository's own `github.py` is not PyGithub; hosts match exactly
(`api.github.com.evil.example` is not GitHub); `github.com` web links are
not the API.

## Domain model (`patchfrog/dependencies/domain.py`)

`ExternalDependency` (key, `DependencyProvider`, `ExternalDependencyKind`
sdk/http_api/openapi_contract/package, `Ecosystem`, package,
`DependencyVersion` declared/resolved + where, `DependencyUsageSite`s,
`DiscoveryEvidence`, `DetectionConfidence`, `DependencyContract` with
`ContractSource` and `ContractFingerprint`, metadata incl. repository
relationship). Confidence: **high** = declared *and* used in code;
**medium** = declared-only, used-only, or HTTP-only.

## Provider adapters (`adapters.py`, `openapi_adapter.py`)

`DependencyProviderAdapter`: `discover_usage`, `normalize_dependency`,
`resolve_contract_source`, `parse_contract`, `fingerprint_contract`,
`build_provider_metadata`. OpenAI, Stripe and GitHub are three
`ProviderSpec` entries of one declarative `KnownProviderAdapter`; adding
Anthropic, AWS, Twilio, Slack, … is one more spec (see
`test_a_new_provider_is_one_spec_without_core_changes`). OpenAPI has its
own adapter because its contract comes from a document. No adapter
monitors upstream changes yet (M6).

## Contracts and fingerprints

- **SDK (`sdk/static`)**: the *consumed surface* — package, declared and
  resolved version, and the sorted set of SDK entry points actually
  called (`chat.completions.create`, `checkout.sessions.create`, …).
- **HTTP (`http/observed`)**: the sorted set of called host+path
  templates.
- **OpenAPI (`openapi/local`)**: paths, methods, parameters, request
  bodies, responses, security requirements, security schemes and
  component shapes; `$ref`s kept as identities (`schema:Charge`);
  descriptions, summaries, examples and extensions dropped.
- **Package (`package/manifest`)**: ecosystem, name, declared, resolved.

Fingerprint = sha256 over canonical sorted-key JSON of the normalized
structure plus `CONTRACT_NORMALIZATION_VERSION`. YAML vs JSON, key/list
order, whitespace and prose never change it; a path, schema, parameter or
auth change does (`tests/unit/test_openapi_contract_fingerprint.py`).
This is fingerprinting only — breaking-change classification is M6.

## Registry (`registry.py`, migration `0034_external_dep_registry`)

| Table | Holds |
|---|---|
| `external_dependencies` | latest state per `(repository_id, dependency_key)`; `active`/`removed`; first/last observed + commit |
| `external_dependency_usage_sites` | current usage sites (file, line, symbol, evidence, token, confidence) |
| `external_contract_snapshots` | contract history: one row per distinct fingerprint, first/last observed |

All cascade from `repositories`. Identical re-discovery writes no new
rows; a changed version or contract adds a snapshot (history is never
overwritten); a vanished dependency is marked `removed` and reactivated
if it returns; usage sites are rewritten only when their fingerprint
changes. Writes per repository are serialized with a PostgreSQL
advisory lock. No raw source, spec text or secret value is stored.

## Dependency graph (`graph.py`)

Built with the existing `GraphNode`/`RepositoryEdge` primitives (new
`EXTERNAL_DEPENDENCY`/`EXTERNAL_CONTRACT` node kinds,
`USES_EXTERNAL_DEPENDENCY`/`DEPENDENCY_HAS_CONTRACT` edge kinds). Symbol
nodes use the code index's `(file_path, qualified_name)` identity —
Python symbols come from the same tree-sitter parser — so M6 can walk
from a contract change to consuming symbols and on into the existing
caller/callee graph.

## Known limitations

- JS/TS usage is lexical: dynamic `require`, re-exports and aliasing
  through other modules are not followed; symbol spans are brace-based.
- Generic packages map to imports by convention (`pyyaml` → `yaml` is not
  known); Go usage is manifest-only.
- Remote `$ref`s and multi-file specs are not resolved; only local specs
  are discovered (no fetching of upstream SaaS specs yet — M6).
- Registry population is on demand (CLI `--persist`); it is not yet
  wired into the webhook pipeline.
