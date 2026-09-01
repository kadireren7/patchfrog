# Contributing

## License

This repository is source-available under the [Elastic License
2.0](LICENSE), not open-source -- see `docs/licensing.md` for what that
means in practice before contributing. By submitting a change, you
agree it's licensed under the same terms as the rest of the repository.

## Before you start

- Read `docs/quickstart.md` to get a working local setup.
- Read `docs/product-boundary.md` for the self-hosted vs. PatchFrog
  Cloud architecture -- Cloud-specific work is out of scope for this
  repository today (see `docs/external-beta.md`).

## Development setup

```bash
pip install -e .
docker compose up -d postgres redis
alembic upgrade head
```

## Tests and gates

Every change is expected to pass, locally, before a PR:

```bash
ruff check .
mypy . --strict
pytest
```

- **No live LLM provider calls in the test suite.** Use
  `patchfrog.review.providers.fake.FakeLLMProvider` (a scripted,
  deterministic stand-in) -- see any existing test importing it for the
  pattern. A live-provider validation run is a separate, explicit,
  operator-approved activity (see `validation/production_e2e/`), never
  something `pytest` does on every run.
- **No secrets in fixtures.** Test credentials (webhook secret, App
  private key) are generated in-memory at test-session start (see
  `tests/conftest.py`) -- never commit a real or real-looking secret
  into a fixture file, even a throwaway-looking one.
- If you touch Alembic migrations, confirm a single head:
  `alembic heads` must print exactly one line.
- If you touch Docker image definitions, confirm both build:
  `docker compose build api worker`.

## Security disclosure

See `SECURITY.md` -- never open a public issue for a vulnerability or
anything containing a secret.
