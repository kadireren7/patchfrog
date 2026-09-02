"""Integration coverage for ``patchfrog doctor``
(:mod:`patchfrog.ops.doctor`) -- external beta readiness.

Every test either exercises the real ``Settings()`` env-var-completeness
path (via ``monkeypatch.delenv``, session-wide required vars are already
set by ``tests/conftest.py``) or injects a hand-built ``Settings``/
``AsyncEngine`` directly (see ``run_doctor``'s own docstring on why both
are test-only injection points), so this suite never needs a real
database/Redis outage or a real GitHub App to prove FAIL/WARN behavior.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from patchfrog.config.settings import Settings
from patchfrog.ops.doctor import DoctorStatus, run_doctor
from patchfrog.ops.health import expected_migration_head

_REQUIRED_ENV_VARS = (
    "DATABASE_URL",
    "REDIS_URL",
    "GITHUB_APP_ID",
    "GITHUB_WEBHOOK_SECRET",
    "GITHUB_PRIVATE_KEY",
)


def _settings(*, test_private_key: str, **overrides: object) -> Settings:
    kwargs: dict[str, object] = {
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "REDIS_URL": "redis://localhost:6379/0",
        "GITHUB_APP_ID": "123456",
        "GITHUB_WEBHOOK_SECRET": "a-real-webhook-secret",
        "GITHUB_PRIVATE_KEY": test_private_key,
    }
    kwargs.update(overrides)
    return Settings(**kwargs)  # type: ignore[arg-type]


async def _migrated_engine(db_engine: AsyncEngine) -> AsyncEngine:
    expected = expected_migration_head()
    assert expected is not None
    async with db_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        await conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": expected})
    return db_engine


async def test_missing_required_settings_reports_fail_not_a_crash(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for var in _REQUIRED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("GITHUB_PRIVATE_KEY_PATH", raising=False)

    report = await run_doctor(check_github_auth=False)

    assert report.overall is DoctorStatus.FAIL
    names = {c.name for c in report.checks}
    assert "settings:DATABASE_URL" in names
    assert "settings:GITHUB_APP_ID" in names
    assert "settings:GITHUB_WEBHOOK_SECRET" in names
    # Never a raw traceback -- every failure is a structured, actionable check.
    assert all(c.detail for c in report.checks if c.status is DoctorStatus.FAIL)


async def test_all_good_reports_pass(db_engine: AsyncEngine, test_private_key: str) -> None:
    engine = await _migrated_engine(db_engine)
    settings = _settings(
        test_private_key=test_private_key,
        PATCHFROG_REVIEW_PROVIDER="gemini",
        PATCHFROG_REVIEW_MODEL="gemini-3.6-flash",
        GEMINI_API_KEY="fake-test-key-not-real",
    )

    report = await run_doctor(settings=settings, engine=engine, check_github_auth=False)

    assert report.overall is DoctorStatus.PASS, [c for c in report.checks if c.status is not DoctorStatus.PASS]


async def test_migration_mismatch_reports_fail(db_engine: AsyncEngine, test_private_key: str) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        await conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": "0000_stale"})
    settings = _settings(test_private_key=test_private_key)

    report = await run_doctor(settings=settings, engine=db_engine, check_github_auth=False)

    assert report.overall is DoctorStatus.FAIL
    db_check = next(c for c in report.checks if c.name == "database")
    assert db_check.status is DoctorStatus.FAIL
    assert "migration mismatch" in db_check.detail


async def test_redis_unavailable_reports_fail(db_engine: AsyncEngine, test_private_key: str) -> None:
    engine = await _migrated_engine(db_engine)
    settings = _settings(test_private_key=test_private_key, REDIS_URL="redis://127.0.0.1:1/0")

    report = await run_doctor(settings=settings, engine=engine, check_github_auth=False)

    assert report.overall is DoctorStatus.FAIL
    redis_check = next(c for c in report.checks if c.name == "redis")
    assert redis_check.status is DoctorStatus.FAIL


async def test_placeholder_webhook_secret_warns(db_engine: AsyncEngine, test_private_key: str) -> None:
    engine = await _migrated_engine(db_engine)
    settings = _settings(test_private_key=test_private_key, GITHUB_WEBHOOK_SECRET="change-me")

    report = await run_doctor(settings=settings, engine=engine, check_github_auth=False)

    secret_check = next(c for c in report.checks if c.name == "github_webhook_secret")
    assert secret_check.status is DoctorStatus.WARN
    assert report.exit_code == 0  # WARN alone never fails the exit code


async def test_missing_provider_credential_warns(db_engine: AsyncEngine, test_private_key: str) -> None:
    engine = await _migrated_engine(db_engine)
    settings = _settings(test_private_key=test_private_key, PATCHFROG_REVIEW_PROVIDER="anthropic")

    report = await run_doctor(settings=settings, engine=engine, check_github_auth=False)

    credential_check = next(c for c in report.checks if c.name == "review_provider_credential")
    assert credential_check.status is DoctorStatus.WARN
    assert "ANTHROPIC_API_KEY" in credential_check.detail


async def test_gemini_provider_with_unset_model_warns_family_mismatch(
    db_engine: AsyncEngine, test_private_key: str
) -> None:
    """The exact live bug this project hit once: PATCHFROG_REVIEW_PROVIDER=gemini
    with PATCHFROG_REVIEW_MODEL left unset silently resolves to the
    Anthropic default (claude-opus-5) and every real review 404s."""

    engine = await _migrated_engine(db_engine)
    settings = _settings(
        test_private_key=test_private_key, PATCHFROG_REVIEW_PROVIDER="gemini", GEMINI_API_KEY="fake-not-real"
    )

    report = await run_doctor(settings=settings, engine=engine, check_github_auth=False)

    mismatch_check = next(c for c in report.checks if c.name == "model_family:PATCHFROG_REVIEW_MODEL")
    assert mismatch_check.status is DoctorStatus.WARN
    assert "claude-opus-5" in mismatch_check.detail
    assert "PATCHFROG_REVIEW_MODEL" in mismatch_check.detail


async def test_matching_provider_and_model_family_passes(db_engine: AsyncEngine, test_private_key: str) -> None:
    engine = await _migrated_engine(db_engine)
    settings = _settings(
        test_private_key=test_private_key,
        PATCHFROG_REVIEW_PROVIDER="gemini",
        PATCHFROG_REVIEW_MODEL="gemini-3.6-flash",
        GEMINI_API_KEY="fake-not-real",
    )

    report = await run_doctor(settings=settings, engine=engine, check_github_auth=False)

    mismatch_check = next(c for c in report.checks if c.name == "model_family:PATCHFROG_REVIEW_MODEL")
    assert mismatch_check.status is DoctorStatus.PASS


async def test_models_prefixed_gemini_name_is_normalized_before_family_check(
    db_engine: AsyncEngine, test_private_key: str
) -> None:
    engine = await _migrated_engine(db_engine)
    settings = _settings(
        test_private_key=test_private_key,
        PATCHFROG_REVIEW_PROVIDER="gemini",
        PATCHFROG_REVIEW_MODEL="models/gemini-3.6-flash",
        GEMINI_API_KEY="fake-not-real",
    )

    report = await run_doctor(settings=settings, engine=engine, check_github_auth=False)

    mismatch_check = next(c for c in report.checks if c.name == "model_family:PATCHFROG_REVIEW_MODEL")
    assert mismatch_check.status is DoctorStatus.PASS


async def test_unsupported_provider_fails(db_engine: AsyncEngine, test_private_key: str) -> None:
    engine = await _migrated_engine(db_engine)
    settings = _settings(test_private_key=test_private_key, PATCHFROG_REVIEW_PROVIDER="openai")

    report = await run_doctor(settings=settings, engine=engine, check_github_auth=False)

    assert report.overall is DoctorStatus.FAIL
    provider_check = next(c for c in report.checks if c.name == "review_provider")
    assert provider_check.status is DoctorStatus.FAIL


async def test_no_secret_values_ever_appear_in_report_output(
    db_engine: AsyncEngine, test_private_key: str
) -> None:
    engine = await _migrated_engine(db_engine)
    webhook_secret = "super-secret-webhook-value-should-never-appear"
    provider_key = "sk-ant-should-never-appear-either"
    settings = _settings(
        test_private_key=test_private_key,
        GITHUB_WEBHOOK_SECRET=webhook_secret,
        PATCHFROG_REVIEW_PROVIDER="anthropic",
        ANTHROPIC_API_KEY=provider_key,
    )

    report = await run_doctor(settings=settings, engine=engine, check_github_auth=False)

    rendered = "\n".join(f"{c.name} {c.status} {c.detail}" for c in report.checks)
    assert webhook_secret not in rendered
    assert provider_key not in rendered
    assert test_private_key not in rendered
    assert "BEGIN" not in rendered  # never echoes the PEM body, only that it has a BEGIN marker structurally


async def test_exit_code_zero_for_pass_and_warn_only_one_for_fail(
    db_engine: AsyncEngine, test_private_key: str
) -> None:
    engine = await _migrated_engine(db_engine)
    warn_only_settings = _settings(test_private_key=test_private_key, PATCHFROG_REVIEW_PROVIDER="anthropic")
    warn_report = await run_doctor(settings=warn_only_settings, engine=engine, check_github_auth=False)
    assert any(c.status is DoctorStatus.WARN for c in warn_report.checks)
    assert warn_report.exit_code == 0

    fail_settings = _settings(test_private_key=test_private_key, REDIS_URL="redis://127.0.0.1:1/0")
    fail_report = await run_doctor(settings=fail_settings, engine=engine, check_github_auth=False)
    assert fail_report.exit_code == 1


async def test_doctor_never_calls_an_llm_or_mutates_state(db_engine: AsyncEngine, test_private_key: str) -> None:
    """No assertion possible on "never calls an LLM" beyond structural
    proof: run_doctor takes no LLMProvider, imports no provider client
    module, and this test's settings carry an obviously-fake credential
    that would fail loudly if any real network call to a provider were
    ever attempted."""

    engine = await _migrated_engine(db_engine)
    settings = _settings(
        test_private_key=test_private_key,
        PATCHFROG_REVIEW_PROVIDER="gemini",
        PATCHFROG_REVIEW_MODEL="gemini-3.6-flash",
        GEMINI_API_KEY="definitely-not-a-real-key",
    )

    before = await _row_counts(engine)
    report = await run_doctor(settings=settings, engine=engine, check_github_auth=False)
    after = await _row_counts(engine)

    assert report.overall is DoctorStatus.PASS
    assert before == after


async def _row_counts(engine: AsyncEngine) -> dict[str, int]:
    async with engine.connect() as conn:
        tables = (
            await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        ).scalars().all()
        return {
            str(t): (await conn.execute(text(f"SELECT COUNT(*) FROM {t}"))).scalar_one()
            for t in tables
        }
