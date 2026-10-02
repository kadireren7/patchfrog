"""M10/M11 end to end: watcher -> change event -> campaign -> per-repository
M6->M9 -> (fake) PR -> reconciliation, with real persistence, real sandbox
verification and the in-memory GitHub. No live provider call anywhere."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.campaigns.demo import DemoReport, run_full_demo
from patchfrog.campaigns.domain import CampaignState, EnrolledRepository, RepoState
from patchfrog.campaigns.evaluate import RepositoryInput
from patchfrog.campaigns.ingest import process_change_for_workspace
from patchfrog.campaigns.policy import PublicationMode, WatchMode, WorkspacePolicy
from patchfrog.cli import main
from patchfrog.executable_verification.sandbox import is_sandbox_available
from patchfrog.persistence.models.campaign import CompatibilityCampaignModel
from patchfrog.persistence.models.migration_pr import MigrationPRModel
from patchfrog.persistence.models.migration_verification import MigrationVerificationRunModel
from patchfrog.persistence.models.repository import RepositoryModel
from patchfrog.persistence.models.upstream import (
    ExternalChangeEventModel,
    MigrationPatchModel,
    MigrationPlanModel,
)
from patchfrog.upstream.domain import ChangeRisk, DependencyTarget
from patchfrog.upstream.events import build_version_change
from patchfrog.watchers.domain import WatchOutcomeKind
from tests.support.campaigns import ACME, FRESH_AT, NOW, SHA, acme_event, acme_input

needs_sandbox = pytest.mark.skipif(not is_sandbox_available(), reason="verification sandbox unavailable on this host")

_DEMO: DemoReport | None = None


async def _demo() -> DemoReport:
    global _DEMO
    if _DEMO is None:
        _DEMO = await run_full_demo()
    return _DEMO


@needs_sandbox
async def test_full_demo_matches_the_acceptance_scenario() -> None:
    report = await _demo()
    assert [o.kind for o in report.watcher] == [
        WatchOutcomeKind.BASELINE, WatchOutcomeKind.CHANGED, WatchOutcomeKind.UNCHANGED,
    ]
    assert report.first is not None and report.first.run is not None and report.first.launched
    first = report.first.run.campaign
    # one campaign, four repository evaluations
    assert report.first.campaign_created and len(first.records) == 4
    states = {r.repository.split("/")[1]: r.state for r in first.records}
    assert states == {
        "acme-web": RepoState.PR_OPENED, "acme-worker": RepoState.HUMAN_REQUIRED,
        "acme-search": RepoState.NOT_AFFECTED, "acme-legacy": RepoState.STALE,
    }
    # not falsely resolved; stale repo never claimed unaffected
    assert first.state is CampaignState.ACTION_REQUIRED and not first.org_blast_radius.safe
    # exactly one evidence-backed PR (verified evidence), one human-required action, one stale repo
    assert report.github_pr_creations == 1 and report.github_pr_drafts == [False]
    web = first.record_for("acme/acme-web")
    assert web is not None and web.verification_outcome == "verified" and web.pr_number is not None
    assert sum(1 for r in first.records if r.state is RepoState.HUMAN_REQUIRED) == 1
    assert [r.repository for r in first.records if r.requires_rediscovery] == ["acme/acme-legacy"]
    # re-run: no duplicate campaign, no duplicate PR, no version churn
    assert report.rerun is not None and report.rerun.run is not None
    assert not report.rerun.campaign_created and report.rerun.run.campaign.version == first.version
    assert report.campaigns_in_database == 1 and report.pull_requests_in_database == 1
    # second scenario: fix + rediscovery -> RESOLVED
    assert report.resolved is not None and report.resolved.run is not None
    resolved = report.resolved.run.campaign
    assert resolved.state is CampaignState.RESOLVED and resolved.org_blast_radius.safe
    assert {r.repository.split("/")[1]: r.state for r in resolved.records} == {
        "acme-web": RepoState.RESOLVED, "acme-worker": RepoState.RESOLVED,
        "acme-search": RepoState.NOT_AFFECTED, "acme-legacy": RepoState.NOT_AFFECTED,
    }


@needs_sandbox
def test_campaigns_demo_cli_text_and_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["campaigns", "demo"]) == 0
    text = capsys.readouterr().out
    assert "action_required" in text and "resolved" in text and "organization safe: False" in text
    assert "campaign created again: False" in text and "GitHub PR creations: 1" in text
    assert main(["campaigns", "demo", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["first_run"]["dossier"]["campaign"]["state"] == "action_required"
    assert payload["after_fixes"]["dossier"]["campaign"]["state"] == "resolved"
    assert payload["github_pr_creations"] == 1 and payload["campaigns_in_database"] == 1


def test_campaigns_analyze_cli_reports_stale_and_access_lost_honestly(
    capsys: pytest.CaptureFixture[str], tmp_path: object
) -> None:
    repos = ACME / "repos"
    args = [
        "campaigns", "analyze", "--old", str(ACME / "contract" / "old.yaml"), "--new", str(ACME / "contract" / "new.yaml"),
        "--hints", str(ACME / "contract" / "hints.yaml"),
        "--repo", f"web={repos / 'acme-web'}", "--repo", f"search={repos / 'acme-search'}",
        "--repo", f"legacy={repos / 'acme-legacy'}", "--repo", f"gone={repos / 'acme-worker'}",
        "--stale", "legacy", "--access-lost", "gone", "--json", "--now", "2026-10-02T12:00:00+00:00",
    ]
    assert main(args) == 0
    dossier = json.loads(capsys.readouterr().out)
    by_repo = {r["repository"]: r for r in dossier["repositories"]}
    assert by_repo["legacy"]["state"] == "stale" and by_repo["legacy"]["requires_rediscovery"]
    assert by_repo["gone"]["state"] == "access_lost" and by_repo["gone"]["impact"]["direct"] == 0
    assert by_repo["search"]["state"] == "not_affected"
    assert by_repo["web"]["state"] == "impacted"  # default policy is detect_only: nothing is generated
    assert dossier["impact"]["organization_safe"] is False
    assert dossier["campaign"]["state"] == "action_required"


def test_campaigns_analyze_cli_rejects_bad_input(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["campaigns", "analyze", "--old", str(ACME / "contract" / "old.yaml"),
                 "--new", str(ACME / "contract" / "new.yaml")]) == 2
    assert "give at least one --repo" in capsys.readouterr().err
    assert main(["campaigns", "analyze", "--old", str(ACME / "contract" / "old.yaml"),
                 "--new", str(ACME / "contract" / "new.yaml"), "--repo", f"a={ACME / 'repos' / 'acme-web'}",
                 "--stale", "typo"]) == 2
    assert "not given as --repo" in capsys.readouterr().err


# -- gate + policy at the ingest boundary ----------------------------------------------------------


async def test_a_version_only_change_is_recorded_but_opens_no_campaign(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    event = build_version_change(DependencyTarget(package_name="acme-ai"), "1.4.0", "2.0.0")
    assert event.classification.risk is ChangeRisk.REVIEW_REQUIRED
    result = await process_change_for_workspace(
        session_factory=session_factory, workspace_key="ws", event=event, entries=[acme_input("acme-web")], now=NOW,
        policy=WorkspacePolicy(watch_mode=WatchMode.MIGRATE),
    )
    assert not result.launched and "below the campaign threshold" in result.reason and result.run is None
    async with session_factory() as session:
        assert (await session.execute(select(func.count()).select_from(CompatibilityCampaignModel))).scalar_one() == 0
    lowered = await process_change_for_workspace(
        session_factory=session_factory, workspace_key="ws", event=event, entries=[acme_input("acme-search")], now=NOW,
        policy=WorkspacePolicy(watch_mode=WatchMode.DETECT_ONLY), min_risk=ChangeRisk.REVIEW_REQUIRED,
    )
    assert lowered.launched and lowered.run is not None


async def test_watch_mode_off_launches_nothing(session_factory: async_sessionmaker[AsyncSession]) -> None:
    event, hints = acme_event()
    result = await process_change_for_workspace(
        session_factory=session_factory, workspace_key="ws", event=event, entries=[acme_input("acme-web")], now=NOW,
        policy=WorkspacePolicy(watch_mode=WatchMode.OFF), hints=hints,
    )
    assert not result.launched and "off" in result.reason


async def test_detect_only_ingest_persists_a_campaign_and_the_event_but_no_migration(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    event, hints = acme_event()
    result = await process_change_for_workspace(
        session_factory=session_factory, workspace_key="ws", event=event,
        entries=[acme_input("acme-web"), acme_input("acme-search")], now=NOW, hints=hints,
        policy=WorkspacePolicy(),
    )
    assert result.launched and result.campaign_created and result.run is not None
    assert result.run.campaign.state is CampaignState.ACTION_REQUIRED
    async with session_factory() as session:
        assert (await session.execute(select(func.count()).select_from(CompatibilityCampaignModel))).scalar_one() == 1
        assert (await session.execute(select(func.count()).select_from(ExternalChangeEventModel))).scalar_one() == 1
        assert (await session.execute(select(func.count()).select_from(MigrationPlanModel))).scalar_one() == 0


@needs_sandbox
async def test_ingest_writes_the_audit_trail_atomically_with_the_campaign(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repository = RepositoryModel(
            github_repository_id=1, owner="acme", name="acme-web", full_name="acme/acme-web", installation_id=1
        )
        session.add(repository)
        await session.commit()
        repository_id = repository.id
    event, hints = acme_event()
    base = acme_input("acme-web")
    entry = RepositoryInput(
        repository=EnrolledRepository("acme/acme-web", last_discovery_at=FRESH_AT, last_discovery_commit_sha=SHA),
        root=base.root, commit_sha=SHA, extra_workspace_paths=base.extra_workspace_paths,
        engine_repository_id=repository_id,
    )
    result = await process_change_for_workspace(
        session_factory=session_factory, workspace_key="ws", event=event, entries=[entry], now=NOW, hints=hints,
        policy=WorkspacePolicy(watch_mode=WatchMode.MIGRATE, publication=PublicationMode.MANUAL_APPROVAL),
    )
    assert result.run is not None and result.run.campaign.records[0].state is RepoState.VERIFIED
    async with session_factory() as session:
        for model in (CompatibilityCampaignModel, ExternalChangeEventModel, MigrationPlanModel, MigrationPatchModel,
                      MigrationVerificationRunModel):
            assert (await session.execute(select(func.count()).select_from(model))).scalar_one() == 1, model
        assert (await session.execute(select(func.count()).select_from(MigrationPRModel))).scalar_one() == 0
    # an identical re-run reconciles in place: still one of everything
    await process_change_for_workspace(
        session_factory=session_factory, workspace_key="ws", event=event, entries=[entry], now=NOW, hints=hints,
        policy=WorkspacePolicy(watch_mode=WatchMode.MIGRATE, publication=PublicationMode.MANUAL_APPROVAL),
    )
    async with session_factory() as session:
        for model in (CompatibilityCampaignModel, MigrationPlanModel, MigrationPatchModel, MigrationVerificationRunModel):
            assert (await session.execute(select(func.count()).select_from(model))).scalar_one() == 1, model
