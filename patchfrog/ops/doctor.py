"""Read-only operator diagnostic (``patchfrog doctor``) -- turns "is this
deployment correctly configured enough to run a first review" into one
PASS/WARN/FAIL report, actionable line by line, aimed at an external beta
operator who has never touched this codebase before (external beta
readiness milestone).

Never prints a secret value (only presence/length/shape), never calls an
LLM, never mutates any state. Deliberately tolerant of a completely
unconfigured environment: :class:`~patchfrog.config.settings.Settings`
itself raises a raw ``pydantic.ValidationError`` for its required fields
(``DATABASE_URL``, ``REDIS_URL``, ``GITHUB_APP_ID``,
``GITHUB_WEBHOOK_SECRET``, the private key) -- this module catches that
and reports each missing/invalid field as its own actionable check
result instead of letting the whole command die with an unfriendly
traceback. This is precisely the gap the onboarding-surface audit found:
a fresh operator's very first ``patchfrog ops health`` run, before
``.env`` is fully filled in, previously crashed with a raw multi-error
pydantic dump and never reached the database/Redis checks it was meant
to report.

:class:`DoctorReport` never fails on a *missing provider credential* or
an *unreachable GitHub API* -- both are ``WARN`` -- because PatchFrog's
own documented behavior (see ``docs/deployment.md``'s "Provider startup/
health behavior") is that indexing/analysis/webhook ingestion stay fully
functional without either; only DB/Redis/migration/App-identity problems
that make the service structurally unable to run are ``FAIL``.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import httpx
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from patchfrog.config.settings import Settings
from patchfrog.github.auth import build_app_jwt
from patchfrog.ops.health import check_database, check_redis
from patchfrog.persistence.database import create_engine
from patchfrog.review.runtime_config import SUPPORTED_PROVIDERS, resolve_review_runtime_config

#: Values copied verbatim from .env.example that a fresh operator is
#: likely to leave unchanged by mistake -- structurally present (Settings
#: accepts it) but functionally wrong (GitHub will reject every webhook
#: signature). Checked case-insensitively.
_PLACEHOLDER_WEBHOOK_SECRETS = frozenset({"change-me", "changeme", "your-webhook-secret"})

#: Conservative model-name family prefixes, after stripping a
#: `models/`-prefixed resource-path form (see
#: patchfrog.review.providers.gemini_provider's own docstring on why that
#: form is legitimate) -- never an exhaustive per-model list, which would
#: go stale the moment either vendor ships a new model name. Exists
#: solely to catch the exact live bug this project already hit once:
#: PATCHFROG_REVIEW_PROVIDER=gemini with PATCHFROG_REVIEW_MODEL left
#: unset (or copy-pasted from an Anthropic example), silently defaulting
#: to `claude-opus-5` and 404ing against Gemini's API on the first real
#: review.
_MODEL_FAMILY_PREFIX: dict[str, str] = {"anthropic": "claude-", "gemini": "gemini-"}
_MODEL_RESOURCE_PREFIX = "models/"


class DoctorStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    status: DoctorStatus
    detail: str = ""


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def overall(self) -> DoctorStatus:
        if any(c.status is DoctorStatus.FAIL for c in self.checks):
            return DoctorStatus.FAIL
        if any(c.status is DoctorStatus.WARN for c in self.checks):
            return DoctorStatus.WARN
        return DoctorStatus.PASS

    @property
    def exit_code(self) -> int:
        """0 = every required check passes (WARNs are still exit 0 --
        beta-usable, just not fully configured). 1 = at least one FAIL.
        A doctor-internal failure (an unexpected exception while running
        checks, never a configuration problem) is reported as exit 2 by
        the CLI wrapper, not from here -- see ``patchfrog.cli._run_doctor``."""

        return 1 if self.overall is DoctorStatus.FAIL else 0


def _settings_checks() -> tuple[list[DoctorCheck], Settings | None]:
    try:
        settings = Settings()
    except ValidationError as exc:
        checks = [
            DoctorCheck(
                name=f"settings:{err['loc'][0]}" if err["loc"] else "settings",
                status=DoctorStatus.FAIL,
                detail=str(err["msg"]),
            )
            for err in exc.errors()
        ]
        return checks, None
    return [DoctorCheck(name="settings", status=DoctorStatus.PASS, detail="all required variables present")], settings


def _webhook_secret_check(settings: Settings) -> DoctorCheck:
    if settings.github_webhook_secret.strip().lower() in _PLACEHOLDER_WEBHOOK_SECRETS:
        return DoctorCheck(
            name="github_webhook_secret",
            status=DoctorStatus.WARN,
            detail="looks like the .env.example placeholder -- every real GitHub webhook will fail signature verification until this is a real secret",
        )
    return DoctorCheck(
        name="github_webhook_secret", status=DoctorStatus.PASS, detail=f"present (length={len(settings.github_webhook_secret)})"
    )


def _private_key_check(settings: Settings) -> DoctorCheck:
    # Settings' own model_validator already guarantees "BEGIN" appears
    # and exactly one of GITHUB_PRIVATE_KEY/_PATH was set -- reaching
    # here at all means that already passed. This check reports *which*
    # source it came from and its shape, never the key material itself.
    source = "GITHUB_PRIVATE_KEY_PATH" if settings.github_private_key_path else "GITHUB_PRIVATE_KEY (inline)"
    looks_complete = "END" in settings.github_private_key
    if not looks_complete:
        return DoctorCheck(
            name="github_private_key",
            status=DoctorStatus.WARN,
            detail=f"source={source}; has a BEGIN marker but no END marker -- the PEM may be truncated",
        )
    return DoctorCheck(name="github_private_key", status=DoctorStatus.PASS, detail=f"source={source}, well-formed PEM shape")


def _provider_checks(settings: Settings) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []

    if settings.review_provider not in SUPPORTED_PROVIDERS:
        checks.append(
            DoctorCheck(
                name="review_provider",
                status=DoctorStatus.FAIL,
                detail=f"PATCHFROG_REVIEW_PROVIDER={settings.review_provider!r} is not one of {sorted(SUPPORTED_PROVIDERS)}",
            )
        )
        return checks

    runtime_config = resolve_review_runtime_config(settings)
    checks.append(
        DoctorCheck(
            name="review_provider",
            status=DoctorStatus.PASS,
            detail=f"provider={runtime_config.provider} model={runtime_config.model} critic_model={runtime_config.critic_model}",
        )
    )

    credential = settings.anthropic_api_key if runtime_config.provider == "anthropic" else settings.gemini_api_key
    credential_env_var = "ANTHROPIC_API_KEY" if runtime_config.provider == "anthropic" else "GEMINI_API_KEY"
    if not credential:
        checks.append(
            DoctorCheck(
                name="review_provider_credential",
                status=DoctorStatus.WARN,
                detail=(
                    f"{credential_env_var} is unset -- indexing/static analysis/webhook ingestion still work, "
                    f"but a real AI review will fail with a clear, non-retryable error the first time one is attempted"
                ),
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="review_provider_credential",
                status=DoctorStatus.PASS,
                detail=f"{credential_env_var} present (length={len(credential)})",
            )
        )

    checks.append(_model_family_check(provider=runtime_config.provider, model=runtime_config.model, field="PATCHFROG_REVIEW_MODEL"))
    if settings.review_critic_model is not None:
        checks.append(
            _model_family_check(
                provider=runtime_config.provider, model=runtime_config.critic_model, field="PATCHFROG_REVIEW_CRITIC_MODEL"
            )
        )
    return checks


def _model_family_check(*, provider: str, model: str, field: str) -> DoctorCheck:
    expected_prefix = _MODEL_FAMILY_PREFIX.get(provider)
    if expected_prefix is None:
        return DoctorCheck(name=f"model_family:{field}", status=DoctorStatus.PASS, detail="no family check defined for this provider")
    normalized = model.removeprefix(_MODEL_RESOURCE_PREFIX)
    if normalized.startswith(expected_prefix):
        return DoctorCheck(name=f"model_family:{field}", status=DoctorStatus.PASS, detail=f"{model!r} looks like a {provider} model name")
    # A model name for a *different known* family is a near-certain
    # misconfiguration (this exact shape happened live: provider=gemini,
    # PATCHFROG_REVIEW_MODEL left unset -> defaulted to claude-opus-5,
    # every review 404'd against Gemini's API). A model name that matches
    # neither known family is conservatively left as PASS -- a new,
    # unlisted model family is not this check's business to reject.
    other_families = [p for p, prefix in _MODEL_FAMILY_PREFIX.items() if p != provider and normalized.startswith(prefix)]
    if other_families:
        return DoctorCheck(
            name=f"model_family:{field}",
            status=DoctorStatus.WARN,
            detail=(
                f"{field}={model!r} looks like a {other_families[0]} model name, but PATCHFROG_REVIEW_PROVIDER={provider!r} -- "
                f"check {field} was actually set (an unset review model silently defaults to the Anthropic model name)"
            ),
        )
    return DoctorCheck(name=f"model_family:{field}", status=DoctorStatus.PASS, detail=f"{model!r} does not match a known mismatched family")


def _hard_caps_check(settings: Settings) -> DoctorCheck:
    return DoctorCheck(
        name="operator_hard_caps",
        status=DoctorStatus.PASS,
        detail=(
            f"max_candidates={settings.review_max_candidates} "
            f"max_total_input_tokens={settings.review_max_total_input_tokens} "
            f"max_output_tokens_per_candidate={settings.review_max_output_tokens_per_candidate} "
            f"max_concurrent_requests={settings.review_max_concurrent_requests} "
            f"max_retries={settings.review_max_retries}"
        ),
    )


def _publication_gate_check(settings: Settings) -> DoctorCheck:
    return DoctorCheck(
        name="publication_gates",
        status=DoctorStatus.PASS,
        detail=(
            f"GLOBAL_PUBLICATION_ENABLED={settings.global_publication_enabled} "
            f"GLOBAL_REVIEW_PROCESSING_ENABLED={settings.global_review_processing_enabled} "
            f"BETA_ALLOWLIST_MODE={settings.beta_allowlist_mode} "
            "-- per-installation (`patchfrog ops installations`) and per-repository "
            "(.patchfrog.yml `publish.enabled`) gates must ALSO be true; see `patchfrog ops preflight`"
        ),
    )


def _webhook_route_check() -> DoctorCheck:
    return DoctorCheck(
        name="webhook_route",
        status=DoctorStatus.PASS,
        detail=(
            "expected route: POST /webhooks/github -- GitHub App must subscribe to the `pull_request` event only, "
            "with permissions contents:read, metadata:read, pull_requests:write (see docs/quickstart.md)"
        ),
    )


async def _github_app_auth_check(settings: Settings) -> DoctorCheck:
    """Best-effort, optional, read-only: proves the App ID + private key
    pair actually authenticates, which presence/shape checks alone
    cannot (a syntactically valid but wrong/revoked key still passes
    those). Never FAILs on network trouble -- an operator running doctor
    from a machine with no outbound internet access (e.g. inside an
    isolated CI runner) should still get a usable report."""

    try:
        token = build_app_jwt(app_id=settings.github_app_id, private_key=settings.github_private_key)
    except Exception as exc:
        return DoctorCheck(name="github_app_auth", status=DoctorStatus.WARN, detail=f"could not sign a JWT with this key: {exc}")

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{settings.github_api_base_url.rstrip('/')}/app",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            )
    except httpx.HTTPError as exc:
        return DoctorCheck(name="github_app_auth", status=DoctorStatus.WARN, detail=f"GitHub API unreachable: {exc}")

    if response.status_code == 200:
        name = response.json().get("name", "?")
        return DoctorCheck(name="github_app_auth", status=DoctorStatus.PASS, detail=f"authenticated as GitHub App {name!r}")
    return DoctorCheck(
        name="github_app_auth",
        status=DoctorStatus.WARN,
        detail=f"GET /app returned {response.status_code} -- check GITHUB_APP_ID and the private key match the same App",
    )


def _git_sha_check() -> DoctorCheck:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return DoctorCheck(name="deployed_commit", status=DoctorStatus.WARN, detail=f"could not determine: {exc}")
    if result.returncode != 0 or not result.stdout.strip():
        return DoctorCheck(name="deployed_commit", status=DoctorStatus.WARN, detail="not running from a git checkout (e.g. a built image with no .git)")
    sha = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        return DoctorCheck(name="deployed_commit", status=DoctorStatus.WARN, detail="unexpected `git rev-parse HEAD` output")
    return DoctorCheck(name="deployed_commit", status=DoctorStatus.PASS, detail=sha)


async def run_doctor(
    *, settings: Settings | None = None, engine: AsyncEngine | None = None, check_github_auth: bool = True
) -> DoctorReport:
    """Run every diagnostic and return a complete report. Never raises
    for a configuration problem (every such case becomes a ``FAIL``/
    ``WARN`` check instead) -- only an unexpected internal error escapes,
    which the CLI wrapper reports as exit code 2 (spec: "internal doctor
    failure"), distinct from exit code 1 ("configuration problem").

    ``settings``/``engine`` are injectable purely for tests (a real test
    database engine, or a hand-built ``Settings`` that never touches
    ``.env``/the process environment) -- production always calls this
    with neither, letting it construct both itself exactly like every
    other ``patchfrog ops`` command does.
    """

    checks: list[DoctorCheck] = []
    owns_engine = engine is None

    if settings is None:
        settings_checks, settings = _settings_checks()
        checks.extend(settings_checks)
    else:
        checks.append(DoctorCheck(name="settings", status=DoctorStatus.PASS, detail="all required variables present"))
    checks.append(_git_sha_check())

    if settings is None:
        return DoctorReport(checks=tuple(checks))

    checks.append(_webhook_secret_check(settings))
    checks.append(_private_key_check(settings))
    checks.append(_webhook_route_check())
    checks.append(_publication_gate_check(settings))
    checks.append(_hard_caps_check(settings))
    checks.extend(_provider_checks(settings))

    resolved_engine = engine if engine is not None else create_engine(settings.database_url)
    try:
        db_check = await check_database(resolved_engine)
        checks.append(DoctorCheck(name="database", status=DoctorStatus.PASS if db_check.healthy else DoctorStatus.FAIL, detail=db_check.detail))
    finally:
        if owns_engine:
            await resolved_engine.dispose()

    redis_check = await check_redis(settings.redis_url)
    checks.append(DoctorCheck(name="redis", status=DoctorStatus.PASS if redis_check.healthy else DoctorStatus.FAIL, detail=redis_check.detail))

    if check_github_auth:
        checks.append(await _github_app_auth_check(settings))

    return DoctorReport(checks=tuple(checks))
