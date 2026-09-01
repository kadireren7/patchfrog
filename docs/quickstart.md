# Quickstart: self-hosted, first review

The one canonical path from a fresh clone to a real PatchFrog review on
a real pull request. Every other doc (`docs/deployment.md`,
`docs/onboarding.md`, `docs/operations.md`) goes deeper on one part of
this flow -- this page exists so you never have to piece those together
yourself for a first run. See `docs/external-beta.md` first if you
haven't already, for what you're actually setting up.

## 1. Prerequisites

- Python 3.12+, PostgreSQL 16+, Redis, Docker (recommended, not
  required -- see `docs/deployment.md`).
- A GitHub account with permission to create a GitHub App on the
  account/org that owns the repositories you want reviewed.
- An Anthropic or Google Gemini API key (see step 8) -- only the
  provider you actually select needs a key.

## 2. Clone

```bash
git clone <this repository's URL>
cd patchfrog
```

## 3. Configure secrets safely

```bash
cp .env.example .env
```

Edit `.env` locally. **Secret safety:**

- Never commit `.env` (already gitignored).
- Never paste a real provider key, GitHub App private key, or webhook
  secret into a chat session, an issue, a PR, or a support request.
- Never run `docker compose config` and share its output -- it resolves
  every `env_file`-sourced secret into plain text.
- If you ever suspect a secret was exposed, rotate it (see
  `docs/beta-runbook.md`'s "Rotate provider key" / "Rotate webhook
  secret" sections) rather than hoping it wasn't seen.

You'll fill in the GitHub App fields (`GITHUB_APP_ID`,
`GITHUB_PRIVATE_KEY_PATH`, `GITHUB_WEBHOOK_SECRET`) after step 4-6
below produce real values for them.

## 4. Create the GitHub App

[github.com/settings/apps/new](https://github.com/settings/apps/new)
(or your org's equivalent). Minimal, exact configuration:

- **Webhook**: check "Active". You'll set the real URL in step 6 --
  any placeholder works for now (GitHub validates the URL is
  well-formed, not that it's currently reachable).
- **Webhook secret**: generate a real random value (e.g. `openssl rand
  -hex 32`) and save it -- this becomes `GITHUB_WEBHOOK_SECRET`.
- **Permissions** (repository): `Contents: Read-only`, `Metadata:
  Read-only`, `Pull requests: Read and write`. Nothing else.
- **Subscribe to events**: `Pull request` only.
- **Where can this GitHub App be installed?**: "Only on this account"
  is simplest for a first self-hosted instance.

After creating it:

- Note the **App ID** (top of the App's settings page) -> `GITHUB_APP_ID`.
- **Generate a private key** (same page) -> download the `.pem` file.
  Point `GITHUB_PRIVATE_KEY_PATH` at its path (preferred -- keeps the
  multiline PEM out of `.env` entirely), or inline it as
  `GITHUB_PRIVATE_KEY` with `\n` for newlines. Exactly one of the two,
  never both -- see `docs/deployment.md`'s "Required runtime secrets".

## 5. Install the App on one test repository

From the App's settings page, "Install App" -> select **one**
repository you control, ideally a throwaway/test one for this first
run. You can add more later.

## 6. Configure the webhook URL

GitHub must be able to reach `POST /webhooks/github` on your API
process over HTTPS. For local/first-run testing, a tunnel (e.g.
`cloudflared tunnel --url http://localhost:8000`) works; for anything
beyond a first test, point the App at a stable, permanently-reachable
ingress instead -- see `docs/deployment.md`'s "Local production-like
stack" and the module docstring of `apps/api/routes/github_webhooks.py`.

Update the App's **Webhook URL** (its settings page, or `PATCH
/app/hook/config` with an App-JWT) to the real reachable URL.
**Verification, not automation**: this project deliberately does not
script this step for you -- confirm it by re-reading the App's settings
page after saving, not by trusting a script's exit code.

## 7. Configure the provider/model (operator-controlled)

In `.env`:

```bash
PATCHFROG_REVIEW_PROVIDER=anthropic   # or: gemini
PATCHFROG_REVIEW_MODEL=claude-opus-5  # or: gemini-3.6-flash for gemini
ANTHROPIC_API_KEY=sk-ant-...          # or GEMINI_API_KEY=... for gemini
```

Never set these in `.patchfrog.yml` -- provider/model selection is an
operator/deployment decision, not a repository one (see
`docs/deployment.md`'s "Provider/model selection" section for why, and
for the full effective-default table if you leave some of these unset).

## 8. Start Postgres/Redis, then the API and worker

```bash
docker compose up -d postgres redis
alembic upgrade head
docker compose up -d api worker
```

(Or run `uvicorn`/`celery` directly against your own Postgres/Redis --
see `docs/deployment.md`.)

## 9. Health/readiness check

```bash
curl http://localhost:8000/health/ready
```

Then run the comprehensive diagnostic -- this is the one command that
actually tells you what's still missing, without ever printing a secret
value:

```bash
patchfrog ops doctor
```

Every `FAIL` line blocks a real review from ever succeeding; every
`WARN` is worth reading but won't block indexing/static analysis. Fix
`FAIL`s, then re-run `doctor` until it reports `PASS` (or an acceptable
`WARN` set -- e.g. no provider credential yet is fine if you're not
ready to spend on a review yet).

## 10. Add a minimal `.patchfrog.yml` (optional)

Not required -- no file means conservative defaults (no publication,
medium+ severity). To actually get a real GitHub comment once you're
ready (see "Publication is off by default" below), commit this to the
test repository:

```yaml
publish:
  enabled: true
  min_severity: medium
```

## 11. Confirm the repository is actually ready

```bash
patchfrog ops preflight --repository <owner>/<repo>
```

Reports one of `PUBLISH` / `DRY_RUN` / `BLOCKED` -- see "Publication is
off by default" below for what each of the three gates this checks
means, and fix whichever one is closed before expecting a real comment.
This checks gates only -- it does not re-check provider/model/credential
health (that's `patchfrog ops doctor`, step 9). Run both; a `PUBLISH`
outcome here still needs a healthy `doctor` report for a provider-backed
review to actually succeed.

## 12. Open a test pull request

Open (or push a commit to) a PR in the installed repository. This
triggers a real `pull_request` webhook -- watch the worker's logs
(`docker compose logs -f worker`, or your own process's stdout) for
`pull_request_ingested` -> `repository_indexed` -> `review_run_completed`.

## 13. Confirm the first review

- If `preflight` reported `PUBLISH`: a real PatchFrog PR review should
  appear within roughly a minute (longer for Gemini -- see
  `docs/deployment.md`'s timeout note).
- If it reported `DRY_RUN`: no GitHub comment appears, but the review
  still ran -- check `patchfrog ops failed` (nothing there means it
  succeeded) or `patchfrog telemetry review <run-id>` once you have a
  run id.
- If nothing happened at all: see `docs/operations.md`'s
  "Troubleshooting" table, or `docs/beta-runbook.md`.

## 14. Inspect telemetry and feedback

```bash
patchfrog telemetry review <review_run_id>
patchfrog feedback sync --repository <owner>/<repo> --pr <number>
```

## 15. Publication gates -- the one-screen explanation

**Three independent gates, every single one must be true, before any
real GitHub write happens:**

1. `GLOBAL_PUBLICATION_ENABLED` (env var, default `true`, restart to
   change) -- the deployment-wide kill switch.
2. `patchfrog ops installations --allow-publication --installation
   <id>` (DB-persisted, default `false`, no restart) -- per-installation
   opt-in.
3. The repository's own `.patchfrog.yml` `publish.enabled` (default
   `false`) -- per-repository opt-in.

None of the three can force publication on its own -- not even all
three combined can publish if a stale-head race or an unmappable
finding intervenes (see `docs/production-e2e.md`'s "Publishing safety").
`patchfrog ops preflight` (step 11) tells you the current state of all
three in one command, before you ever open a real PR to find out.

## 16. Recovery checklist

| Symptom | What to run |
|---|---|
| Not sure what's misconfigured | `patchfrog ops doctor` |
| Not sure if this repo will publish | `patchfrog ops preflight --repository owner/repo` |
| PR opened, nothing happened | `patchfrog ops failed [--since ISO]` |
| A review run stuck `RUNNING` | `patchfrog ops stale [--recover]` |
| Need to re-run a specific commit | `patchfrog ops retry <review_run_id>` |
| Need an overview across recent activity | `patchfrog telemetry beta-summary --since 7d` |

See `docs/beta-runbook.md` for the fuller day-to-day operator playbook,
including incident response (provider quota, webhook outage, worker
outage, key rotation).
