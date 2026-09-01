<p align="center">
  <img src="docs/assets/brand/patchfrog-logo.png" alt="PatchFrog" width="320" />
</p>

<p align="center">
  PatchFrog is a <strong>source-available</strong> AI code review engine for GitHub — static analysis, an AI reviewer, and a deterministic context engine, published as real GitHub PR reviews. Self-host it with your own AI provider credentials, or use PatchFrog Cloud (planned / under development).
</p>

<p align="center">
  <a href="https://github.com/kadireren7/patchfrog/actions/workflows/ci.yml"><img src="https://github.com/kadireren7/patchfrog/actions/workflows/ci.yml/badge.svg" alt="CI status" /></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-blue" alt="Python 3.12+" />
  <img src="https://img.shields.io/badge/license-Elastic--2.0-informational" alt="License: Elastic License 2.0 (source available)" />
</p>

## What it does

PatchFrog installs as a GitHub App and reviews pull requests. It combines:

- **Static Analysis Engine** — ruff, semgrep, cppcheck, and clang-tidy adapters over Python, C, and C++, with toolchain-aware result identity.
- **Context Engine** — deterministic, non-LLM selection of the surrounding code a finding needs (symbol graph, cross-file `extern` resolution), so the reviewer sees exactly the relevant context and nothing more.
- **AI Reviewer** — two cooperating specialist agents (Correctness, Security — see [`docs/agent-orchestration.md`](docs/agent-orchestration.md)) reviewing the same shared, deterministic evidence, plus an independent critic; validates and explains candidate findings (identification, root cause, impact, fix) with calibrated confidence, never raw scores, with cross-agent duplicate/contradiction handling before anything reaches a PR.
- **Incremental Review + Review Memory** — re-reviews only what changed; unrelated files, unchanged symbols, and already-carried findings are never re-sent to the provider. Renames, moves, and ambiguous matches are handled explicitly, never guessed.
- **Publishing** — findings are posted as a real GitHub PR review: one summary comment plus inline comments on the diff, safe-by-default (dry-run unless explicitly enabled), idempotent against retries.
- **Feedback Loop** — polls reactions, replies, `/patchfrog` commands, and thread state to measure usefulness and correctness signals over time, without ever webhook-driving off endpoints the App isn't granted.
- **Quality Evaluation Harness** — a committed benchmark corpus with a golden baseline (precision/recall, clean-case pass rate, incremental-safety, critic on/off, context-ablation) gates every change before merge.

A published review looks like:

> ## 🐸 PatchFrog review
>
> 🐸 **HIGH · security** — Password logged in plaintext
>
> the raw password is interpolated directly into a log line...

## Requirements

- Python 3.12+
- PostgreSQL, Redis
- A GitHub App installation (webhook + REST access to the target repository)

## Quickstart

New to PatchFrog? [`docs/quickstart.md`](docs/quickstart.md) is the one
canonical path from a fresh clone to a real GitHub App review on a real
pull request -- GitHub App creation, webhook setup, provider
configuration, and the `patchfrog ops doctor`/`patchfrog ops preflight`
diagnostics that tell you what's still missing before you open a PR to
find out. See [`docs/external-beta.md`](docs/external-beta.md) first for
exactly what you're setting up (self-hosted only -- PatchFrog Cloud is
planned, not available today) and its current limitations.

## Local development

```bash
docker compose up -d postgres redis   # or point at your own instances
pip install -e .
alembic upgrade head
```

Run the CLI directly against a local checkout:

```bash
python -m patchfrog.cli index /path/to/repo
python -m patchfrog.cli analyze /path/to/repo
python -m patchfrog.cli review /path/to/repo --base main
```

| Command          | Purpose                                                              |
| ----------------- | --------------------------------------------------------------------- |
| `index`           | Index a local repository checkout                                     |
| `analyze`         | Run static analysis (requires `index` first)                          |
| `context`         | Build a deterministic context bundle for a finding or file/line       |
| `review`          | Run the AI reviewer against the diff since `--base`                   |
| `review-history`  | Inspect incremental review and finding-memory history                 |
| `publish`         | Plan (default) or publish a completed review run as a GitHub review   |
| `feedback`        | Sync/inspect/export reaction and command feedback signals             |
| `ops`             | Health/doctor checks, per-repo preflight, recovery, usage, installation management |
| `eval`            | Run the quality evaluation harness against the benchmark corpus       |

Full command help: `python -m patchfrog.cli --help`.

## Testing

```bash
ruff check .
mypy . --strict
pytest
```

## Architecture and brand

See [`docs/brand.md`](docs/brand.md) for identity/tone guidelines and asset usage, [`docs/product-boundary.md`](docs/product-boundary.md) for the self-hosted vs. PatchFrog Cloud architecture, and the `docs/` directory for phase-by-phase design notes.

## License

[Elastic License 2.0](LICENSE) (source available). See [`docs/licensing.md`](docs/licensing.md) for what changed from this repository's earlier Apache-2.0 releases (not retroactive) and what ELv2 means in practice, and [`TRADEMARK.md`](TRADEMARK.md) for name/logo/bot-identity usage.
