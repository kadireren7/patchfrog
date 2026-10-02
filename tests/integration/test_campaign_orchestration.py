"""M10: cross-repository campaigns against the real engine on the Acme fixture
workspace (four repositories, one upstream SDK change). Verification runs in
the real sandbox; GitHub is the in-memory fake; no provider is ever called."""

from __future__ import annotations

import ast
import shutil
import uuid
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.campaigns.domain import (
    CampaignState,
    EnrolledRepository,
    Freshness,
    OrgClass,
    RepositoryAccess,
    RepoState,
    UnknownReason,
    campaign_identity_key,
)
from patchfrog.campaigns.dossier import campaign_to_dict, render_campaign_markdown
from patchfrog.campaigns.evaluate import RepositoryInput
from patchfrog.campaigns.graph import build_org_graph
from patchfrog.campaigns.internal import InternalContract, InternalContractKind, contract_target
from patchfrog.campaigns.orchestrate import (
    CampaignRunResult,
    PullRequestPublisher,
    pr_publisher_from,
    run_campaign,
)
from patchfrog.campaigns.policy import PublicationMode, WatchMode, WorkspacePolicy
from patchfrog.campaigns.store import CampaignStore
from patchfrog.dependencies.discovery import discover_dependencies
from patchfrog.dependencies.domain import DependencyInventory, Ecosystem
from patchfrog.executable_verification.sandbox import is_sandbox_available
from patchfrog.migration_pr.fake_github import FakeMigrationGitHubPublisher
from patchfrog.migration_pr.publisher import MigrationPRPublisher
from patchfrog.upstream.events import build_contract_change, load_contract_file
from patchfrog.upstream.workspace import adapters_for_target
from tests.support.campaigns import (
    ACME,
    FRESH_AT,
    INTERNAL,
    NOW,
    SHA,
    STALE_AT,
    acme_entries,
    acme_event,
    acme_input,
)

MIGRATE = WorkspacePolicy(watch_mode=WatchMode.MIGRATE)
#: Hosts without a working bwrap/prlimit sandbox skip the tests whose point is a *VERIFIED* outcome (the
#: repository's established convention -- see docs/ci-health.md); freshness/state/idempotency tests still run.
needs_sandbox = pytest.mark.skipif(not is_sandbox_available(), reason="verification sandbox unavailable on this host")
WS = "workspace-1"


async def _run(entries: list[RepositoryInput] | None = None, **kwargs: object) -> CampaignRunResult:
    event, hints = acme_event()
    return await run_campaign(
        workspace_key=WS, event=event, entries=entries if entries is not None else acme_entries(), now=NOW,
        hints=hints, **kwargs,  # type: ignore[arg-type]
    )


_BASELINE: CampaignRunResult | None = None


async def _baseline() -> CampaignRunResult:
    """The default migrate-mode run, computed once: real sandbox verification is
    the slow part and these results are immutable data."""

    global _BASELINE
    if _BASELINE is None:
        _BASELINE = await _run(policy=MIGRATE)
    return _BASELINE


def _states(result: CampaignRunResult) -> dict[str, RepoState]:
    return {r.repository.removeprefix("acme/"): r.state for r in result.campaign.records}


@needs_sandbox
async def test_mixed_outcomes_are_never_reported_resolved() -> None:
    result = await _baseline()
    assert _states(result) == {
        "acme-web": RepoState.VERIFIED, "acme-worker": RepoState.HUMAN_REQUIRED,
        "acme-search": RepoState.NOT_AFFECTED, "acme-legacy": RepoState.STALE,
    }
    campaign = result.campaign
    assert campaign.state is CampaignState.ACTION_REQUIRED
    assert not campaign.org_blast_radius.safe
    assert campaign.org_blast_radius.summary() == {
        "total_enrolled": 4, "potentially_relevant": 4, "affected": 2, "not_affected": 1, "unknown": 1,
    }
    assert campaign.org_blast_radius.unknown_reasons == {"acme/acme-legacy": UnknownReason.STALE_DISCOVERY.value}
    assert any("cannot be declared safe" in risk for risk in campaign.unresolved_risks)


@needs_sandbox
async def test_per_repository_evidence_is_independent() -> None:
    result = await _baseline()
    web, worker = (result.campaign.record_for(f"acme/{n}") for n in ("acme-web", "acme-worker"))
    assert web is not None and worker is not None
    assert web.verification_outcome == "verified" and web.publication_readiness == "auto_open"
    assert web.verification_feasibility == "sandbox" and web.migration_strategy_available
    assert worker.verification_outcome == "human_required" and worker.publication_readiness == "plan_only"
    assert worker.human_actions and "stream" in worker.human_actions[0]
    assert web.patch_fingerprint and web.patch_fingerprint != worker.patch_fingerprint
    assert web.bundle_fingerprint != worker.bundle_fingerprint
    assert web.severity == "breaking" and web.direct_consumers >= 1 and web.transitive_consumers == 0


async def test_unaffected_repo_with_the_dependency_is_not_affected_but_relevant() -> None:
    result = await _baseline()
    search = result.campaign.record_for("acme/acme-search")
    assert search is not None
    assert search.org_class is OrgClass.NOT_AFFECTED and search.impact_status == "unaffected"
    assert search.patch_fingerprint is None and search.direct_consumers == 0


async def test_stale_repository_is_never_analyzed_even_when_a_checkout_exists() -> None:
    # The checkout would classify as affected if it were looked at; stale evidence must not be reused.
    stale_with_root = acme_input("acme-worker", discovered_at=STALE_AT)
    result = await _run([stale_with_root], policy=MIGRATE)
    record = result.campaign.records[0]
    assert record.state is RepoState.STALE and record.org_class is OrgClass.UNKNOWN
    assert record.direct_consumers == 0 and record.patch_fingerprint is None and record.verification_outcome is None
    assert record.requires_rediscovery


async def test_never_discovered_repository_is_unknown_not_unaffected() -> None:
    result = await _run([acme_input("acme-search", discovered_at=None, root=None)], policy=MIGRATE)
    record = result.campaign.records[0]
    assert record.state is RepoState.UNKNOWN and record.unknown_reason is UnknownReason.NO_DISCOVERY
    assert result.campaign.state is CampaignState.ACTION_REQUIRED


async def test_access_lost_is_recorded_and_never_claimed_unaffected() -> None:
    entry = acme_input("acme-search", access=RepositoryAccess.ACCESS_LOST)
    result = await _run([entry, acme_input("acme-web")], policy=MIGRATE)
    states = _states(result)
    assert states["acme-search"] is RepoState.ACCESS_LOST
    record = result.campaign.record_for("acme/acme-search")
    assert record is not None and record.freshness is Freshness.ACCESS_LOST
    assert record.org_class is OrgClass.UNKNOWN and "acme/acme-search" in result.campaign.org_blast_radius.unknown
    assert result.campaign.state is not CampaignState.RESOLVED


async def test_detect_only_policy_classifies_but_generates_nothing() -> None:
    result = await _run()  # default policy: detect_only
    states = _states(result)
    assert states["acme-web"] is RepoState.IMPACTED and states["acme-worker"] is RepoState.IMPACTED
    for evaluation in result.evaluations:
        assert evaluation.plan is None and evaluation.patch is None and evaluation.bundle is None
    assert result.campaign.state is CampaignState.ACTION_REQUIRED
    assert result.decisions == {}


async def test_registry_only_evidence_cannot_generate_a_patch() -> None:
    # Affected per the checkout-free path is impossible without rows; with no root and no evidence it is unknown.
    entry = RepositoryInput(
        repository=EnrolledRepository("acme/acme-web", last_discovery_at=FRESH_AT), root=None, commit_sha=SHA
    )
    result = await _run([entry], policy=MIGRATE)
    assert result.campaign.records[0].state is RepoState.UNKNOWN
    assert result.campaign.records[0].unknown_reason is UnknownReason.NO_DISCOVERY


async def test_one_repository_failing_does_not_poison_the_others() -> None:
    from patchfrog.migration_verification.service import run_migration_verification

    async def verify(**kwargs: object) -> object:
        if "acme-web" in str(getattr(kwargs["plan"], "repository", "")):
            raise RuntimeError("sandbox exploded at https://user:hunter2@example.com/x ghp_" + "a" * 30)
        return await run_migration_verification(**kwargs)  # type: ignore[arg-type]

    result = await _run(policy=MIGRATE, verify=verify)
    web = result.campaign.record_for("acme/acme-web")
    assert web is not None and web.state is RepoState.FAILED
    assert web.error is not None and "hunter2" not in web.error and "ghp_" not in web.error
    states = _states(result)
    assert states["acme-worker"] is RepoState.HUMAN_REQUIRED and states["acme-search"] is RepoState.NOT_AFFECTED
    assert result.campaign.state is CampaignState.ACTION_REQUIRED


async def test_rerun_is_idempotent_and_does_not_bump_the_version() -> None:
    first = await _baseline()
    second = await _run(policy=MIGRATE, previous=first.campaign)
    assert second.campaign.campaign_id == first.campaign.campaign_id
    assert second.campaign.version == first.campaign.version == 1
    assert not second.changed
    assert [r.state for r in second.campaign.records] == [r.state for r in first.campaign.records]


def test_campaign_identity_is_workspace_change_and_engine_version() -> None:
    a = campaign_identity_key(workspace_key="w1", change_fingerprint="f")
    assert a == campaign_identity_key(workspace_key="w1", change_fingerprint="f")
    assert a != campaign_identity_key(workspace_key="w2", change_fingerprint="f")
    assert a != campaign_identity_key(workspace_key="w1", change_fingerprint="g")
    assert a != campaign_identity_key(workspace_key="w1", change_fingerprint="f", engine_version=2)


# -- publication ----------------------------------------------------------------------------


def _fake_github() -> FakeMigrationGitHubPublisher:
    fake = FakeMigrationGitHubPublisher()
    for repo in ("acme-web", "acme-worker", "acme-search", "acme-legacy"):
        fake.set_ref(owner="acme", repository=repo, ref="heads/main", sha=SHA)
    return fake


def _publisher(
    session_factory: async_sessionmaker[AsyncSession], fake: FakeMigrationGitHubPublisher
) -> PullRequestPublisher:
    return pr_publisher_from(MigrationPRPublisher(session_factory=session_factory, publisher=fake, installation_id=1))


@needs_sandbox
async def test_automatic_publication_opens_exactly_one_pr_and_never_for_human_required(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = _fake_github()
    policy = WorkspacePolicy(watch_mode=WatchMode.MIGRATE_AND_OPEN_PR, publication=PublicationMode.AUTOMATIC)
    first = await _run(policy=policy, publish=_publisher(session_factory, fake))
    assert len(fake.create_pull_request_calls) == 1
    assert fake.create_pull_request_calls[0].startswith("patchfrog/migrate/acme-ai/")
    assert fake.create_pull_request_drafts == [False]
    web = first.campaign.record_for("acme/acme-web")
    assert web is not None and web.state is RepoState.PR_OPENED and web.pr_number == 100 and web.pr_url
    worker = first.campaign.record_for("acme/acme-worker")
    assert worker is not None and worker.pr_number is None and worker.state is RepoState.HUMAN_REQUIRED
    assert set(first.publications) == {"acme/acme-web"}

    again = await _run(policy=policy, publish=_publisher(session_factory, fake), previous=first.campaign)
    assert len(fake.create_pull_request_calls) == 1  # no duplicate PR
    assert again.campaign.record_for("acme/acme-web").pr_number == 100  # type: ignore[union-attr]
    assert again.campaign.version == first.campaign.version


@needs_sandbox
async def test_draft_only_and_manual_approval_modes(session_factory: async_sessionmaker[AsyncSession]) -> None:
    draft_fake = _fake_github()
    draft = WorkspacePolicy(watch_mode=WatchMode.MIGRATE_AND_OPEN_PR, publication=PublicationMode.DRAFT_ONLY)
    await _run(policy=draft, publish=_publisher(session_factory, draft_fake))
    assert draft_fake.create_pull_request_drafts == [True]

    manual_fake = _fake_github()
    manual = WorkspacePolicy(watch_mode=WatchMode.MIGRATE_AND_OPEN_PR, publication=PublicationMode.MANUAL_APPROVAL)
    result = await _run(policy=manual, publish=_publisher(session_factory, manual_fake))
    assert manual_fake.create_pull_request_calls == [] and manual_fake.create_ref_calls == []
    assert result.decisions["acme/acme-web"].action.value == "await_approval"
    assert result.campaign.record_for("acme/acme-web").state is RepoState.VERIFIED  # type: ignore[union-attr]


async def test_migrate_mode_never_touches_github(session_factory: async_sessionmaker[AsyncSession]) -> None:
    fake = _fake_github()
    await _run(policy=MIGRATE, publish=_publisher(session_factory, fake))
    assert fake.create_ref_calls == [] and fake.create_pull_request_calls == []


@needs_sandbox
async def test_stale_base_is_rejected_and_never_published(session_factory: async_sessionmaker[AsyncSession]) -> None:
    fake = _fake_github()
    fake.set_ref(owner="acme", repository="acme-web", ref="heads/main", sha="b" * 40)  # base moved
    policy = WorkspacePolicy(watch_mode=WatchMode.MIGRATE_AND_OPEN_PR, publication=PublicationMode.AUTOMATIC)
    result = await _run(policy=policy, publish=_publisher(session_factory, fake))
    web = result.campaign.record_for("acme/acme-web")
    assert web is not None and web.pr_status == "stale_requires_regeneration" and web.pr_number is None
    assert web.state is RepoState.VERIFIED and fake.create_pull_request_calls == []
    assert any("regenerate" in r for r in web.reasons)


async def test_publication_error_is_isolated_to_its_repository(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def boom(**_: object) -> object:
        raise RuntimeError("github 502")

    policy = WorkspacePolicy(watch_mode=WatchMode.MIGRATE_AND_OPEN_PR, publication=PublicationMode.AUTOMATIC)
    result = await _run(policy=policy, publish=boom)
    web = result.campaign.record_for("acme/acme-web")
    assert web is not None and web.state is RepoState.FAILED and web.error
    assert _states(result)["acme-worker"] is RepoState.HUMAN_REQUIRED


# -- reconciliation to RESOLVED -----------------------------------------------------------------


def _copy_repo(tmp_path: Path, name: str) -> Path:
    destination = tmp_path / name
    shutil.copytree(ACME / "repos" / name, destination)
    return destination


async def test_campaign_reconciles_to_resolved_when_every_repo_is_actually_resolved(tmp_path: Path) -> None:
    first = await _baseline()
    assert first.campaign.state is CampaignState.ACTION_REQUIRED

    # 1) repo A's verified patch lands (a human merged the PR).
    web_root = _copy_repo(tmp_path, "acme-web")
    web_patch = next(e for e in first.evaluations if e.record.repository == "acme/acme-web").patch
    assert web_patch is not None
    for relative, content in web_patch.new_contents.items():
        (web_root / relative).write_text(content)
    step1 = await _run(
        [acme_input("acme-web", root=web_root), acme_input("acme-worker"), acme_input("acme-search"),
         acme_input("acme-legacy", discovered_at=STALE_AT, root=None)],
        policy=MIGRATE, previous=first.campaign,
    )
    assert _states(step1)["acme-web"] is RepoState.RESOLVED
    assert step1.campaign.state is CampaignState.PARTIALLY_RESOLVED
    assert step1.campaign.version == first.campaign.version + 1

    # 2) a human fixes repo B (applies the automatic edits, then removes the argument by hand).
    worker_root = _copy_repo(tmp_path, "acme-worker")
    worker_patch = next(e for e in first.evaluations if e.record.repository == "acme/acme-worker").patch
    assert worker_patch is not None
    for relative, content in worker_patch.new_contents.items():
        (worker_root / relative).write_text(content)
    summary = worker_root / "app" / "summary.py"
    summary.write_text(summary.read_text().replace(", stream=False", ""))

    # 3) repo D is rediscovered (fresh evidence, and it really is unaffected).
    final = await _run(
        [acme_input("acme-web", root=web_root), acme_input("acme-worker", root=worker_root),
         acme_input("acme-search"), acme_input("acme-legacy", discovered_at=FRESH_AT)],
        policy=MIGRATE, previous=step1.campaign,
    )
    assert _states(final) == {
        "acme-web": RepoState.RESOLVED, "acme-worker": RepoState.RESOLVED,
        "acme-search": RepoState.NOT_AFFECTED, "acme-legacy": RepoState.NOT_AFFECTED,
    }
    assert final.campaign.state is CampaignState.RESOLVED
    assert final.campaign.org_blast_radius.safe
    assert not final.campaign.unresolved_risks


# -- persistence + dossier --------------------------------------------------------------------


async def test_store_round_trip_and_idempotent_upsert(session_factory: async_sessionmaker[AsyncSession]) -> None:
    result = await _baseline()
    store = CampaignStore()
    async with session_factory() as session:
        _row, created = await store.upsert(session, result.campaign, recorded_at=NOW)
        await session.commit()
    assert created
    async with session_factory() as session:
        _, created_again = await store.upsert(session, result.campaign, recorded_at=NOW)
        await session.commit()
        loaded = await store.load(session, identity_key=result.campaign.campaign_id)
    assert not created_again and loaded is not None
    assert loaded.state is result.campaign.state and loaded.version == result.campaign.version
    assert [(r.repository, r.state) for r in loaded.records] == sorted(
        (r.repository, r.state) for r in result.campaign.records
    )
    assert loaded.org_blast_radius == result.campaign.org_blast_radius
    assert campaign_to_dict(loaded)["impact"] == campaign_to_dict(result.campaign)["impact"]
    async with session_factory() as session:
        assert len(await store.list_for_workspace(session, workspace_key=WS)) == 1


async def test_store_drops_repositories_no_longer_in_scope(session_factory: async_sessionmaker[AsyncSession]) -> None:
    full = await _baseline()
    smaller = await _run([acme_input("acme-web"), acme_input("acme-search")], policy=MIGRATE, previous=full.campaign)
    store = CampaignStore()
    async with session_factory() as session:
        await store.upsert(session, full.campaign)
        await store.upsert(session, smaller.campaign)
        await session.commit()
        loaded = await store.load(session, identity_key=full.campaign.campaign_id)
    assert loaded is not None and sorted(loaded.repositories_in_scope) == ["acme/acme-search", "acme/acme-web"]


async def test_same_change_in_two_workspaces_makes_two_campaigns(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    event, hints = acme_event()
    store = CampaignStore()
    for workspace in ("ws-a", "ws-b"):
        run = await run_campaign(workspace_key=workspace, event=event, entries=[acme_input("acme-search")], now=NOW,
                                 hints=hints, policy=MIGRATE)
        async with session_factory() as session:
            await store.upsert(session, run.campaign)
            await session.commit()
    async with session_factory() as session:
        assert len(await store.list_for_workspace(session, workspace_key="ws-a")) == 1
        assert len(await store.list_for_workspace(session, workspace_key="ws-b")) == 1


@needs_sandbox
async def test_dossier_is_machine_readable_and_honest() -> None:
    result = await _baseline()
    dossier = campaign_to_dict(result.campaign)
    assert dossier["campaign"]["state"] == "action_required"
    assert dossier["impact"]["organization_safe"] is False
    assert dossier["impact"]["unknown"] == ["acme/acme-legacy"]
    by_repo = {r["repository"]: r for r in dossier["repositories"]}
    assert by_repo["acme/acme-legacy"]["requires_rediscovery"] is True
    assert by_repo["acme/acme-web"]["next_action"] == "approve opening the verified migration PR"
    assert by_repo["acme/acme-search"]["next_action"] is None
    assert [a["repository"] for a in dossier["human_required_actions"]] == ["acme/acme-worker"]
    assert dossier["data_freshness"]["acme/acme-legacy"] == "stale"
    text = render_campaign_markdown(result.campaign)
    assert "NOT declared safe" in text and "acme/acme-legacy" in text and "Required actions" in text


# -- org graph ------------------------------------------------------------------------------


async def test_org_graph_shares_one_identity_across_repositories() -> None:
    event, _ = acme_event()
    entries: list[tuple[EnrolledRepository, Freshness, DependencyInventory | None]] = []
    for name in ("acme-web", "acme-worker", "acme-search", "acme-legacy"):
        inventory = discover_dependencies(ACME / "repos" / name, repository=f"acme/{name}",
                                          adapters=adapters_for_target(event.target))
        fresh = Freshness.STALE if name == "acme-legacy" else Freshness.FRESH
        enrolled = EnrolledRepository(f"acme/{name}", last_discovery_at=FRESH_AT)
        entries.append((enrolled, fresh, inventory))
    entries.append((EnrolledRepository("acme/empty"), Freshness.UNKNOWN, None))
    graph = build_org_graph(WS, entries)
    identity = graph.identity("acme-ai")
    assert identity is not None and identity.origin == "external"
    assert identity.repositories == ("acme/acme-legacy", "acme/acme-search", "acme/acme-web", "acme/acme-worker")
    empty = next(r for r in graph.repositories if r.full_name == "acme/empty")
    assert not empty.has_inventory and empty.freshness is Freshness.UNKNOWN
    assert any("acme/acme-legacy: evidence is stale" in n for n in graph.notes)
    assert graph.to_dict()["identities"][0]["identity"] == "acme-ai"


# -- internal contracts (M10.5) ---------------------------------------------------------------


def _internal_inputs() -> tuple[InternalContract, list[RepositoryInput]]:
    contract = InternalContract(
        "acme-shared", InternalContractKind.PACKAGE, "acme/platform-lib",
        contract_target(package_name="acme-shared", ecosystem=Ecosystem.PYPI, modules=["acme_shared"],
                        display_name="acme-shared"),
    )
    entries = [
        RepositoryInput(
            repository=EnrolledRepository(f"acme/{name}", last_discovery_at=FRESH_AT, last_discovery_commit_sha=SHA),
            root=INTERNAL / "repos" / name, commit_sha=SHA,
        )
        for name in ("platform-lib", "svc-orders", "svc-billing", "svc-reports", "acme-shared-fork")
    ]
    return contract, entries


async def test_internal_contract_change_maps_only_explicit_consumers() -> None:
    contract, entries = _internal_inputs()
    event = build_contract_change(
        load_contract_file(INTERNAL / "contract" / "old.yaml"), load_contract_file(INTERNAL / "contract" / "new.yaml"),
        target=contract.target,
    )
    result = await run_campaign(
        workspace_key=WS, event=event, entries=entries, now=NOW, policy=MIGRATE, internal_contract=contract,
    )
    states = {r.repository.removeprefix("acme/"): r.state for r in result.campaign.records}
    assert "platform-lib" not in states  # the producer is the origin, not a consumer
    assert result.excluded == {"acme/platform-lib": "producer of internal contract 'acme-shared'; not a consumer"}
    assert states["svc-orders"] is RepoState.HUMAN_REQUIRED and states["svc-billing"] is RepoState.HUMAN_REQUIRED
    assert states["svc-reports"] is RepoState.NOT_AFFECTED
    # a repository that merely sounds related, with no dependency evidence, is not matched by name
    fork = result.campaign.record_for("acme/acme-shared-fork")
    assert fork is not None and fork.impact_status == "unaffected" and fork.state is RepoState.NOT_AFFECTED
    assert fork.direct_consumers == 0
    assert result.campaign.origin == "internal" and result.campaign.producer_repository == "acme/platform-lib"
    assert result.campaign.state is CampaignState.ACTION_REQUIRED


async def test_internal_contract_graph_groups_under_the_explicit_identity() -> None:
    contract, entries = _internal_inputs()
    event = build_contract_change(
        load_contract_file(INTERNAL / "contract" / "old.yaml"), load_contract_file(INTERNAL / "contract" / "new.yaml"),
        target=contract.target,
    )
    graph_entries = []
    for entry in entries:
        inventory = discover_dependencies(entry.root, repository=entry.repository.full_name,  # type: ignore[arg-type]
                                          adapters=adapters_for_target(event.target))
        graph_entries.append((entry.repository, Freshness.FRESH, inventory))
    graph = build_org_graph(WS, graph_entries, internal_contracts=[contract])
    node = graph.identity("internal:acme-shared")
    assert node is not None and node.origin == "internal" and node.producer_repository == "acme/platform-lib"
    assert node.repositories == ("acme/svc-billing", "acme/svc-orders", "acme/svc-reports")
    assert "acme/acme-shared-fork" not in node.repositories


# -- structural invariants ----------------------------------------------------------------------

_CAMPAIGNS = Path(__file__).resolve().parents[2] / "patchfrog" / "campaigns"


def test_campaigns_package_never_imports_a_provider() -> None:
    for path in _CAMPAIGNS.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(
                    ("patchfrog.review", "patchfrog.routing", "anthropic", "openai", "google")
                ), f"{path.name} imports {node.module}"
            if isinstance(node, ast.Import):
                assert not any(a.name.split(".")[0] in ("anthropic", "openai", "google") for a in node.names)


def test_campaigns_package_has_no_fixture_specific_strings() -> None:
    for path in _CAMPAIGNS.glob("*.py"):
        text = path.read_text().lower()
        for word in ("acme", "chat.create", "responses.create", "stripe", "openai"):
            assert word not in text.replace("an openai", ""), f"{path.name} mentions {word!r}"


# -- registry pre-screen (cheap path: no checkout) ---------------------------------------------------


async def _registry_rows(
    session_factory: async_sessionmaker[AsyncSession], names: tuple[str, ...]
) -> dict[str, tuple[uuid.UUID, str]]:
    from patchfrog.dependencies.registry import DependencyRegistry
    from patchfrog.persistence.models.repository import RepositoryModel

    event, _ = acme_event()
    out: dict[str, tuple[uuid.UUID, str]] = {}
    async with session_factory() as session:
        for index, name in enumerate(names, start=1):
            repository = RepositoryModel(
                github_repository_id=index, owner="acme", name=name, full_name=f"acme/{name}", installation_id=1
            )
            session.add(repository)
            await session.flush()
            inventory = discover_dependencies(
                ACME / "repos" / name, repository=f"acme/{name}", commit_sha=SHA,
                adapters=adapters_for_target(event.target),
            )
            await DependencyRegistry().record_inventory(session, repository_id=repository.id, inventory=inventory)
            out[name] = (repository.id, f"acme/{name}")
        await session.commit()
    return out


async def test_registry_read_is_scoped_to_the_given_repositories(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from patchfrog.upstream.store import UpstreamChangeStore

    ids = await _registry_rows(session_factory, ("acme-web", "acme-search"))
    store = UpstreamChangeStore()
    async with session_factory() as session:
        only_web = await store.registry_dependencies_for_repositories(session, [ids["acme-web"][0]])
        nothing = await store.registry_dependencies_for_repositories(session, [])
        both = await store.registry_dependencies_for_repositories(session, [i for i, _ in ids.values()])
    assert {r.repository for r in only_web} == {"acme/acme-web"}
    assert nothing == []  # an empty scope is never "everything"
    assert {r.repository for r in both} == {"acme/acme-web", "acme/acme-search"}


async def test_fresh_registry_with_no_matching_dependency_is_not_affected_without_a_checkout(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    entry = RepositoryInput(
        repository=EnrolledRepository("acme/other", last_discovery_at=FRESH_AT), root=None, commit_sha=SHA,
        use_registry=True,
    )
    result = await _run([entry], policy=MIGRATE)
    record = result.campaign.records[0]
    assert record.state is RepoState.NOT_AFFECTED and record.org_class is OrgClass.NOT_AFFECTED
    assert record.impact_status == "unaffected"
    # ...whereas "the registry was never consulted" stays unknown
    unconsulted = RepositoryInput(repository=EnrolledRepository("acme/other", last_discovery_at=FRESH_AT), commit_sha=SHA)
    assert (await _run([unconsulted], policy=MIGRATE)).campaign.records[0].state is RepoState.UNKNOWN


async def test_registry_evidence_classifies_affected_but_cannot_generate_a_patch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from patchfrog.upstream.store import UpstreamChangeStore

    ids = await _registry_rows(session_factory, ("acme-web", "acme-search"))
    async with session_factory() as session:
        rows = await UpstreamChangeStore().registry_dependencies_for_repositories(session, [i for i, _ in ids.values()])
    by_repo: dict[str, list[object]] = {}
    for row in rows:
        by_repo.setdefault(row.repository, []).append(row)
    entries = [
        RepositoryInput(
            repository=EnrolledRepository(name, last_discovery_at=FRESH_AT), root=None, commit_sha=SHA,
            registry_rows=tuple(by_repo.get(name, [])), use_registry=True,  # type: ignore[arg-type]
        )
        for name in ("acme/acme-web", "acme/acme-search")
    ]
    result = await _run(entries, policy=MIGRATE)
    states = {r.repository: r for r in result.campaign.records}
    assert states["acme/acme-search"].state is RepoState.NOT_AFFECTED
    web = states["acme/acme-web"]
    assert web.state is RepoState.IMPACTED and web.org_class is OrgClass.AFFECTED
    assert web.patch_fingerprint is None and any("checkout" in r for r in web.reasons)


@needs_sandbox
async def test_a_human_approval_turns_await_into_a_real_publication_after_a_fresh_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = _fake_github()
    policy = WorkspacePolicy(watch_mode=WatchMode.MIGRATE_AND_OPEN_PR, publication=PublicationMode.MANUAL_APPROVAL)
    waiting = await _run(policy=policy, publish=_publisher(session_factory, fake))
    assert fake.create_pull_request_calls == [] and waiting.decisions["acme/acme-web"].action.value == "await_approval"

    approved = await _run(
        policy=policy, publish=_publisher(session_factory, fake), previous=waiting.campaign,
        approved_repositories=frozenset({"acme/acme-web"}),
    )
    assert len(fake.create_pull_request_calls) == 1 and fake.create_pull_request_drafts == [False]
    web = approved.campaign.record_for("acme/acme-web")
    assert web is not None and web.state is RepoState.PR_OPENED
    # approval is per repository: the human-required repo still has no PR
    assert approved.campaign.record_for("acme/acme-worker").pr_number is None  # type: ignore[union-attr]
    # approving a repository whose migration is not eligible changes nothing
    again = await _run(
        policy=policy, publish=_publisher(session_factory, fake), previous=approved.campaign,
        approved_repositories=frozenset({"acme/acme-web", "acme/acme-worker"}),
    )
    assert len(fake.create_pull_request_calls) == 1
    assert again.campaign.record_for("acme/acme-worker").pr_number is None  # type: ignore[union-attr]
