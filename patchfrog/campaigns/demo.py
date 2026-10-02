"""The deterministic M10/M11 end-to-end demo.

A fictional workspace of four repositories, one fictional upstream SDK
(``client.chat.create`` -> ``client.responses.create``), a manually submitted
SDK-surface snapshot standing in for a watcher source, an in-memory GitHub and
an in-memory database. No network, no live provider call, nothing outside a
temporary directory is written.

Stages: (1) the watcher baselines, then detects the change; (2) one campaign
is created, four repositories are evaluated, one evidence-backed PR is
opened and one human-required action is reported; (3) a re-run creates no
second campaign and no second PR; (4) after the PR lands, the human fix is
made and the stale repository is rediscovered, the campaign reconciles to
``RESOLVED``.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from patchfrog.campaigns.domain import EnrolledRepository
from patchfrog.campaigns.evaluate import RepositoryInput
from patchfrog.campaigns.ingest import WorkspaceChangeResult, process_change_for_workspace
from patchfrog.campaigns.orchestrate import pr_publisher_from
from patchfrog.campaigns.policy import PublicationMode, WatchMode, WorkspacePolicy
from patchfrog.dependencies.domain import Ecosystem
from patchfrog.migration_pr.fake_github import FakeMigrationGitHubPublisher
from patchfrog.migration_pr.publisher import MigrationPRPublisher
from patchfrog.persistence.models import Base
from patchfrog.persistence.models.campaign import CompatibilityCampaignModel
from patchfrog.persistence.models.migration_pr import MigrationPRModel
from patchfrog.upstream.domain import DependencyTarget
from patchfrog.upstream.events import load_contract_file
from patchfrog.upstream.hints import ChangeHints, load_hints
from patchfrog.watchers.adapters.manual import snapshot_from_contract
from patchfrog.watchers.domain import WatcherCursor, WatcherSource, WatcherSourceKind, WatchOutcome
from patchfrog.watchers.pipeline import ingest_snapshot

DEMO_ROOT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "campaigns" / "acme"
DEMO_WORKSPACE = "acme"
DEMO_SHA = "d3adb33f" * 5
#: Fixed so every run prints the same thing.
DEMO_NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
DEMO_REPOSITORIES = ("acme-web", "acme-worker", "acme-search", "acme-legacy")


class DemoUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DemoStage:
    title: str
    lines: tuple[str, ...]


@dataclass(slots=True)
class DemoReport:
    watcher: list[WatchOutcome] = field(default_factory=list)
    first: WorkspaceChangeResult | None = None
    rerun: WorkspaceChangeResult | None = None
    resolved: WorkspaceChangeResult | None = None
    campaigns_in_database: int = 0
    pull_requests_in_database: int = 0
    github_pr_creations: int = 0
    github_pr_drafts: list[bool] = field(default_factory=list)


def _entry(name: str, root: Path | None, *, discovered_at: datetime | None) -> RepositoryInput:
    return RepositoryInput(
        repository=EnrolledRepository(
            full_name=f"acme/{name}", last_discovery_at=discovered_at, last_discovery_commit_sha=DEMO_SHA
        ),
        root=root, commit_sha=DEMO_SHA, extra_workspace_paths=(DEMO_ROOT / "sdk_stub",),
    )


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    try:
        import aiosqlite  # noqa: F401
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise DemoUnavailable("the demo needs aiosqlite (pip install 'patchfrog[dev]')") from exc
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _load_change(*, hints: ChangeHints) -> tuple[WatcherSource, WatcherCursor, WatchOutcome, WatchOutcome]:
    old = load_contract_file(DEMO_ROOT / "contract" / "old.yaml")
    new = load_contract_file(DEMO_ROOT / "contract" / "new.yaml")
    source = WatcherSource(
        WatcherSourceKind.MANUAL, "acme-ai",
        DependencyTarget(ecosystem=Ecosystem.PYPI, package_name="acme-ai", modules=("acme_ai",), display_name="acme-ai"),
    )
    baseline = ingest_snapshot(source, snapshot_from_contract(source, old, now=DEMO_NOW), None, hints=hints)
    changed = ingest_snapshot(
        source, snapshot_from_contract(source, new, now=DEMO_NOW + timedelta(hours=1)), baseline.cursor, hints=hints
    )
    return source, changed.cursor, baseline, changed


async def run_full_demo() -> DemoReport:
    if not DEMO_ROOT.is_dir():
        raise DemoUnavailable(f"demo assets not found at {DEMO_ROOT} (expected a full repository checkout)")
    report = DemoReport()
    hints = load_hints((DEMO_ROOT / "contract" / "hints.yaml").read_text())
    source, cursor, baseline, changed = _load_change(hints=hints)
    report.watcher = [baseline, changed]
    assert changed.change is not None
    event = changed.change.event
    # polling the same upstream state again emits nothing
    report.watcher.append(
        ingest_snapshot(
            source, snapshot_from_contract(source, load_contract_file(DEMO_ROOT / "contract" / "new.yaml"),
                                           now=DEMO_NOW + timedelta(hours=2)), cursor, hints=hints,
        )
    )

    policy = WorkspacePolicy(watch_mode=WatchMode.MIGRATE_AND_OPEN_PR, publication=PublicationMode.AUTOMATIC)
    fake = FakeMigrationGitHubPublisher()
    for name in DEMO_REPOSITORIES:
        fake.set_ref(owner="acme", repository=name, ref="heads/main", sha=DEMO_SHA)
    repos = DEMO_ROOT / "repos"
    fresh, stale = DEMO_NOW - timedelta(hours=2), DEMO_NOW - timedelta(days=30)

    with tempfile.TemporaryDirectory(prefix="patchfrog-campaign-demo-") as tmp:
        work = Path(tmp)
        async with _database() as session_factory:
            publish = pr_publisher_from(
                MigrationPRPublisher(session_factory=session_factory, publisher=fake, installation_id=1)
            )

            async def run(entries: list[RepositoryInput], now: datetime) -> WorkspaceChangeResult:
                return await process_change_for_workspace(
                    session_factory=session_factory, workspace_key=DEMO_WORKSPACE, event=event, entries=entries,
                    now=now, policy=policy, hints=hints, publish=publish,
                )

            first_entries = [
                _entry("acme-web", repos / "acme-web", discovered_at=fresh),
                _entry("acme-worker", repos / "acme-worker", discovered_at=fresh),
                _entry("acme-search", repos / "acme-search", discovered_at=fresh),
                _entry("acme-legacy", None, discovered_at=stale),
            ]
            report.first = await run(first_entries, DEMO_NOW)
            report.rerun = await run(first_entries, DEMO_NOW)

            # -- the world moves on: A's PR is merged, B is fixed by a human, D is rediscovered.
            assert report.first.run is not None
            patches = {e.record.repository: e.patch for e in report.first.run.evaluations if e.patch is not None}
            web = work / "acme-web"
            shutil.copytree(repos / "acme-web", web)
            web_patch = patches["acme/acme-web"]
            for relative, content in web_patch.new_contents.items():
                (web / relative).write_text(content)
            worker = work / "acme-worker"
            shutil.copytree(repos / "acme-worker", worker)
            for relative, content in patches["acme/acme-worker"].new_contents.items():
                (worker / relative).write_text(content)
            summary = worker / "app" / "summary.py"
            summary.write_text(summary.read_text().replace(", stream=False", ""))
            later = DEMO_NOW + timedelta(days=1)
            report.resolved = await run(
                [
                    _entry("acme-web", web, discovered_at=later - timedelta(hours=1)),
                    _entry("acme-worker", worker, discovered_at=later - timedelta(hours=1)),
                    _entry("acme-search", repos / "acme-search", discovered_at=later - timedelta(hours=1)),
                    _entry("acme-legacy", repos / "acme-legacy", discovered_at=later - timedelta(minutes=5)),
                ],
                later,
            )
            async with session_factory() as session:
                report.campaigns_in_database = int(
                    (await session.execute(select(func.count()).select_from(CompatibilityCampaignModel))).scalar_one()
                )
                report.pull_requests_in_database = int(
                    (await session.execute(select(func.count()).select_from(MigrationPRModel))).scalar_one()
                )
    report.github_pr_creations = len(fake.create_pull_request_calls)
    report.github_pr_drafts = list(fake.create_pull_request_drafts)
    return report


__all__ = ["DEMO_ROOT", "DemoReport", "DemoUnavailable", "run_full_demo"]
