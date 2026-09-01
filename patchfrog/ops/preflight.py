"""Read-only, per-repository pre-flight check (``patchfrog ops
preflight --repository owner/repo``) -- external beta readiness.

Answers, in one shot and without requiring a real webhook delivery to
have arrived first: "if a pull request opened against this repository
right now, would PatchFrog review it, and would it actually publish?"
Before this existed, answering that required manually cross-referencing
``patchfrog ops installations`` (installation-level state) against a
repository's own ``.patchfrog.yml`` (read by hand) against
``GLOBAL_PUBLICATION_ENABLED`` (an environment variable, invisible from
any CLI output) -- exactly the kind of scattered-across-three-places
ambiguity that produced real confusion once already during this
project's own dogfood (see ``validation/production_e2e/latest-summary.md``
section 8).

Reuses :func:`patchfrog.ops.eligibility.check_eligibility` directly --
the *exact* function the real webhook-triggered pipeline calls -- rather
than re-deriving its own copy of eligibility logic, so preflight can
never silently drift from what a real PR would actually experience.
Never calls an LLM. The one live network access this module performs
(resolving ``.patchfrog.yml`` from the repository's current default
branch) is read-only and best-effort: an unreachable GitHub API degrades
that single check to ``WARN``, never crashes the whole report.

**Scope, precisely**: this module answers whether the *repository/
eligibility/publication gates* permit review and publication -- it
deliberately does not re-check provider/model/credential health
(``patchfrog ops doctor``'s job -- see that module's own
``review_provider``/``review_provider_credential``/``model_family:*``
checks) or GitHub App auth (also doctor's ``github_app_auth`` check).
Never duplicated here: a `PUBLISH` outcome means "every gate this
module checks is open," not "a provider-backed review is guaranteed to
succeed" -- doctor must *also* report no `FAIL` (and ideally no
provider-related `WARN`) for that. Run both; neither subsumes the
other.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.config.settings import Settings
from patchfrog.github.auth import InstallationTokenProvider
from patchfrog.github.client import GitHubClient
from patchfrog.github.errors import GitHubError
from patchfrog.ops.doctor import DoctorStatus
from patchfrog.ops.eligibility import IneligibilityReason, check_eligibility
from patchfrog.ops.queries import get_repository_and_installation_by_full_name
from patchfrog.persistence.models.installation import InstallationStatus
from patchfrog.publishing.config_resolution import resolve_repository_publication_config


class PreflightOutcome(StrEnum):
    #: Review generation would run AND, if it finds anything, PatchFrog
    #: would actually write a real GitHub comment.
    PUBLISH = "publish"
    #: Review generation would run, but nothing would ever reach GitHub
    #: -- at least one publish gate is closed (or unresolvable).
    DRY_RUN = "dry_run"
    #: Review generation itself would never even start.
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    name: str
    status: DoctorStatus
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PreflightReport:
    repository_full_name: str
    checks: tuple[PreflightCheck, ...]
    outcome: PreflightOutcome


async def run_preflight(
    session: AsyncSession,
    *,
    settings: Settings,
    repository_full_name: str,
    github_client: GitHubClient | None = None,
) -> PreflightReport:
    checks: list[PreflightCheck] = []

    found = await get_repository_and_installation_by_full_name(session, full_name=repository_full_name)
    if found is None:
        checks.append(
            PreflightCheck(
                name="repository",
                status=DoctorStatus.FAIL,
                detail=(
                    "no repository known to PatchFrog under this name -- has the GitHub App been "
                    "installed and this repository selected? (see docs/quickstart.md)"
                ),
            )
        )
        return PreflightReport(repository_full_name=repository_full_name, checks=tuple(checks), outcome=PreflightOutcome.BLOCKED)

    repository, installation = found
    checks.append(
        PreflightCheck(
            name="repository",
            status=DoctorStatus.PASS,
            detail=f"known, installation_id={repository.installation_id}, is_selected={repository.is_selected}",
        )
    )

    decision = await check_eligibility(
        session, settings=settings, repository=repository, github_installation_id=repository.installation_id
    )
    if not decision.eligible:
        checks.append(
            PreflightCheck(
                name="eligibility",
                status=DoctorStatus.FAIL,
                detail=_ineligibility_detail(decision.reason, decision.detail),
            )
        )
        return PreflightReport(repository_full_name=repository_full_name, checks=tuple(checks), outcome=PreflightOutcome.BLOCKED)
    checks.append(PreflightCheck(name="eligibility", status=DoctorStatus.PASS, detail="review generation would run"))

    # From here, review generation is confirmed eligible -- everything
    # else only affects whether a finding could ever actually publish.
    global_ok = settings.global_publication_enabled
    checks.append(
        PreflightCheck(
            name="publish_gate:global",
            status=DoctorStatus.PASS if global_ok else DoctorStatus.WARN,
            detail=f"GLOBAL_PUBLICATION_ENABLED={global_ok}",
        )
    )

    installation_ok = installation is not None and installation.publication_allowed
    checks.append(
        PreflightCheck(
            name="publish_gate:installation",
            status=DoctorStatus.PASS if installation_ok else DoctorStatus.WARN,
            detail=(
                f"publication_allowed={installation.publication_allowed}"
                if installation is not None
                else "no installation row found"
            ),
        )
    )

    repo_gate_ok, repo_gate_check = await _repository_publish_gate_check(
        settings=settings,
        repository_full_name=repository_full_name,
        installation_id=repository.installation_id,
        github_client=github_client,
    )
    checks.append(repo_gate_check)

    all_gates_confirmed_open = global_ok and installation_ok and repo_gate_ok is True
    outcome = PreflightOutcome.PUBLISH if all_gates_confirmed_open else PreflightOutcome.DRY_RUN
    return PreflightReport(repository_full_name=repository_full_name, checks=tuple(checks), outcome=outcome)


def _ineligibility_detail(reason: IneligibilityReason | None, detail: str) -> str:
    messages = {
        IneligibilityReason.INSTALLATION_NOT_FOUND: "no installation row -- has the GitHub App `installation` webhook event ever been received?",
        IneligibilityReason.INSTALLATION_NOT_ACTIVE: f"installation status is not {InstallationStatus.ACTIVE.value} -- check `patchfrog ops installations`",
        IneligibilityReason.BETA_NOT_ACTIVE: "installation beta_state is not active -- run `patchfrog ops installations --activate <id>` (BETA_ALLOWLIST_MODE is on)",
        IneligibilityReason.REPOSITORY_NOT_SELECTED: "repository is not selected under its installation -- was it deselected, or removed from the App's repository access?",
        IneligibilityReason.REPOSITORY_INSTALLATION_MISMATCH: "repository's recorded installation_id disagrees with its own row -- data inconsistency, fails closed",
        IneligibilityReason.GLOBAL_PROCESSING_DISABLED: "GLOBAL_REVIEW_PROCESSING_ENABLED=false -- the whole deployment's review generation is off",
        IneligibilityReason.QUOTA_EXCEEDED: f"per-installation daily review quota exceeded ({detail})",
        IneligibilityReason.RESOURCE_LIMIT_EXCEEDED: "resource limit exceeded (only ever evaluated per-PR, not by preflight)",
    }
    base = messages.get(reason, "not eligible") if reason is not None else "not eligible"
    return f"{base}" + (f" -- {detail}" if detail and reason not in (IneligibilityReason.QUOTA_EXCEEDED,) else "")


async def _repository_publish_gate_check(
    *,
    settings: Settings,
    repository_full_name: str,
    installation_id: int,
    github_client: GitHubClient | None,
) -> tuple[bool | None, PreflightCheck]:
    """Best-effort: resolves the repository's *actual* current
    ``.patchfrog.yml`` ``publish.enabled`` from its live default branch.
    Returns ``(True/False/None, check)`` -- ``None`` means "could not be
    determined", which the caller must never treat as either gate state."""

    try:
        owner, name = repository_full_name.split("/", 1)
    except ValueError:
        return None, PreflightCheck(
            name="publish_gate:repository", status=DoctorStatus.WARN, detail=f"malformed repository name {repository_full_name!r}"
        )

    try:
        async with httpx.AsyncClient(timeout=settings.github_api_timeout_seconds) as http_client:
            token_provider = InstallationTokenProvider(
                http_client=http_client,
                app_id=settings.github_app_id,
                private_key=settings.github_private_key,
                api_base_url=settings.github_api_base_url,
            )
            client = github_client or GitHubClient(
                http_client=http_client,
                token_provider=token_provider,
                api_base_url=settings.github_api_base_url,
                timeout_seconds=settings.github_api_timeout_seconds,
            )
            head_sha = await client.get_default_branch_head_sha(installation_id=installation_id, owner=owner, repository=name)
            token = await token_provider.get_token(installation_id)
            config = await resolve_repository_publication_config(
                local=False,
                commit_sha=head_sha,
                repository_full_name=repository_full_name,
                clone_url=f"https://github.com/{repository_full_name}.git",
                token=token,
            )
    except (GitHubError, httpx.HTTPError, OSError) as exc:
        return None, PreflightCheck(
            name="publish_gate:repository",
            status=DoctorStatus.WARN,
            detail=f"could not resolve .patchfrog.yml from the live default branch: {exc}",
        )

    return config.enabled, PreflightCheck(
        name="publish_gate:repository",
        status=DoctorStatus.PASS if config.enabled else DoctorStatus.WARN,
        detail=f"publish.enabled={config.enabled} (min_severity={config.min_severity.value}, resolved at {head_sha[:12]})",
    )
