# Acme campaign fixtures

A deterministic, fictional workspace used by the M10/M11 tests and by
`patchfrog campaigns demo`. The "upstream" is the same fictional `acme-ai` SDK the
M7-M9 demos use (`client.chat.create(prompt=...)` -> `client.responses.create(input=...)`);
it never describes a real vendor's API.

| Repository | Role |
|---|---|
| `acme-web` | direct SDK consumer, fully automatic migration, targeted test proves it -> `VERIFIED` |
| `acme-worker` | uses a removed argument (`stream=False`) -> `HUMAN_REQUIRED` |
| `acme-search` | uses `acme-ai` but only `embeddings.create`, which does not change -> `NOT_AFFECTED` |
| `acme-legacy` | already on 2.0, but its dependency discovery is *stale* -> never claimed unaffected |

`sdk_stub/` is a runnable stand-in for the 2.0 SDK given only to verification.
