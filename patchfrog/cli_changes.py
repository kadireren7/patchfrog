"""``patchfrog changes ...`` (M6) and ``patchfrog migrations ...`` (M7).

Offline and deterministic: no network, no provider call, no LLM. Only
``--persist`` / ``--registry`` touch the configured database. Secret
values are never read (discovery never opens secret-store files; hints
refuse credential-shaped values) and never printed.

Wired from :mod:`patchfrog.cli`; kept in its own module so the M6/M7
surface does not further grow the main CLI file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.config.settings import get_settings
from patchfrog.dependencies.discovery import discover_dependencies
from patchfrog.dependencies.domain import DependencyInventory, Ecosystem
from patchfrog.dependencies.registry import DependencyRegistry
from patchfrog.persistence.database import create_engine, create_session_factory
from patchfrog.persistence.models.repository import RepositoryModel
from patchfrog.repository.git import GitError, run_git
from patchfrog.upstream.domain import DependencyTarget, ExternalChangeEvent, ExternalChangeSource
from patchfrog.upstream.events import (
    ContractLoadError,
    LoadedContract,
    build_contract_change,
    build_version_change,
    contract_from_registry_snapshot,
    load_contract_file,
    release_from_metadata,
)
from patchfrog.upstream.hints import EMPTY_HINTS, ChangeHints, HintError, load_hints
from patchfrog.upstream.report import (
    event_to_dict,
    render_event_text,
    render_workspace_text,
    workspace_to_dict,
)
from patchfrog.upstream.sdk_surface import SurfaceError
from patchfrog.upstream.store import UpstreamChangeStore
from patchfrog.upstream.workspace import (
    RepositoryImpact,
    WorkspaceImpact,
    adapters_for_target,
    analyze_inventory,
    current_version,
    registry_impact,
)

UpsertRepository = Callable[..., Awaitable[uuid.UUID]]


class CommandError(Exception):
    """A user-facing input problem (printed without a traceback)."""


@dataclass(frozen=True)
class RepoArg:
    name: str
    path: Path


def _repo_arg(value: str) -> RepoArg:
    name, sep, path = value.partition("=")
    if sep and name and not Path(value).exists():
        return RepoArg(name=name, path=Path(path))
    return RepoArg(name=Path(value).resolve().name, path=Path(value))


def _commit_sha(root: Path) -> str | None:
    if not (root / ".git").exists():
        return None
    try:
        return run_git(["-C", str(root), "rev-parse", "HEAD"]).strip()
    except GitError:
        return None


# -- shared inputs -------------------------------------------------------------------


def add_change_inputs(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("upstream change (one of: contract pair, version change, registry snapshot)")
    group.add_argument("--old", type=Path, help="Old contract: OpenAPI 3.x/Swagger 2.0 document or SDK surface")
    group.add_argument("--new", type=Path, help="New contract (same format as --old)")
    group.add_argument("--hints", type=Path, help="Change hints (patchfrog_change_hints: 1) -- renames/replacements")
    group.add_argument("--package", help="Package name for a version change (e.g. stripe)")
    group.add_argument("--ecosystem", choices=[e.value for e in Ecosystem if e is not Ecosystem.NONE],
                       help="Package ecosystem for a version change")
    group.add_argument("--from-version", help="Current version (default: the version the repository uses)")
    group.add_argument("--to-version", help="New version")
    group.add_argument("--release", type=Path, help="Release metadata JSON (GitHub release object or "
                                                    "{version, tag, published_at, url, notes})")
    group.add_argument("--provider", help="Provider identity of the dependency (e.g. github)")
    group.add_argument("--dependency", help="Exact dependency key to target (e.g. openapi:api/openapi.yaml)")
    group.add_argument("--from-registry", action="store_true",
                       help="Take the old contract from the latest M5 registry snapshot of --dependency in "
                            "--full-name (requires DATABASE_URL)")
    group.add_argument("--full-name", help="Repository identity for --persist/--from-registry (owner/repo)")


def _explicit_target(args: argparse.Namespace) -> DependencyTarget | None:
    if not (args.provider or args.dependency or args.package):
        return None
    return DependencyTarget(
        provider_key=args.provider,
        ecosystem=Ecosystem(args.ecosystem) if args.ecosystem else None,
        package_name=args.package,
        dependency_key=args.dependency,
    )


def _load_hints(args: argparse.Namespace) -> ChangeHints:
    if args.hints is None:
        return EMPTY_HINTS
    try:
        return load_hints(args.hints.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CommandError(f"cannot read hints: {exc.__class__.__name__}") from exc


def _release(args: argparse.Namespace) -> Any:
    if args.release is None:
        return None
    try:
        return release_from_metadata(json.loads(args.release.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise CommandError(f"cannot read release metadata: {exc.__class__.__name__}") from exc


async def _registry_old_contract(args: argparse.Namespace) -> LoadedContract:
    if not args.dependency or not args.full_name:
        raise CommandError("--from-registry needs --dependency and --full-name")
    settings = get_settings()
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            repository = (
                await session.execute(select(RepositoryModel).where(RepositoryModel.full_name == args.full_name))
            ).scalar_one_or_none()
            if repository is None:
                raise CommandError(f"repository {args.full_name!r} is not in the registry")
            found = await UpstreamChangeStore().latest_contract_snapshot(
                session, repository_id=repository.id, dependency_key=args.dependency
            )
    finally:
        await engine.dispose()
    if found is None:
        raise CommandError(f"no contract snapshot for {args.dependency!r} in {args.full_name!r}")
    dependency, snapshot = found
    if snapshot.normalized_contract is None:
        raise CommandError("the registry snapshot is fingerprint-only (contract too large to store)")
    return contract_from_registry_snapshot(
        snapshot.normalized_contract, fingerprint=snapshot.fingerprint,
        ref=f"registry:{args.full_name}:{snapshot.source_ref}", version=dependency.declared_version,
    )


def build_event(args: argparse.Namespace, *, first_repo: Path | None) -> tuple[ExternalChangeEvent, ChangeHints]:
    hints = _load_hints(args)
    release = _release(args)
    target = _explicit_target(args)
    if args.from_registry:
        if args.new is None:
            raise CommandError("--from-registry needs --new")
        old = asyncio.run(_registry_old_contract(args))
        new = load_contract_file(args.new)
        return build_contract_change(old, new, hints=hints, target=target, release=release,
                                     source=ExternalChangeSource.REGISTRY_SNAPSHOT), hints
    if args.old is not None or args.new is not None:
        if args.old is None or args.new is None:
            raise CommandError("--old and --new must be given together")
        source = ExternalChangeSource.GITHUB_RELEASE if release is not None else None
        return build_contract_change(load_contract_file(args.old), load_contract_file(args.new), hints=hints,
                                     target=target, release=release, source=source), hints
    to_version = args.to_version or (release.version if release is not None else None)
    if to_version is None:
        raise CommandError("give --old/--new, or --package with --to-version/--release, or --from-registry")
    if target is None or not (target.package_name or target.dependency_key or target.provider_key):
        raise CommandError("a version change needs --package (and ideally --ecosystem)")
    from_version = args.from_version
    if from_version is None and first_repo is not None:
        from_version = current_version(
            discover_dependencies(first_repo, adapters=adapters_for_target(target)), target
        )
    source = ExternalChangeSource.GITHUB_RELEASE if release is not None else ExternalChangeSource.PACKAGE_VERSION
    return build_version_change(target, from_version, to_version, hints=hints, release=release,
                                source=source), hints


# -- changes diff ---------------------------------------------------------------------


def run_changes_diff(args: argparse.Namespace) -> int:
    hints = _load_hints(args)
    event = build_contract_change(load_contract_file(args.old_contract), load_contract_file(args.new_contract),
                                  hints=hints)
    if args.output:
        Path(args.output).write_text(json.dumps(event_to_dict(event), indent=2, sort_keys=True) + "\n")
    if args.json:
        print(json.dumps(event_to_dict(event), indent=2, sort_keys=True))
    else:
        print(render_event_text(event, show_non_breaking=args.all), end="")
    return 0


# -- changes analyze ---------------------------------------------------------------------


@dataclass
class LocalAnalysis:
    event: ExternalChangeEvent
    hints: ChangeHints
    workspace: WorkspaceImpact
    inventories: dict[str, DependencyInventory]
    roots: dict[str, Path]


def analyze_local(args: argparse.Namespace, repos: list[RepoArg]) -> LocalAnalysis:
    for repo in repos:
        if not repo.path.is_dir():
            raise CommandError(f"not a directory: {repo.path}")
    event, hints = build_event(args, first_repo=repos[0].path if repos else None)
    impacts: list[RepositoryImpact] = []
    inventories: dict[str, DependencyInventory] = {}
    for repo in repos:
        name = args.full_name if (args.full_name and len(repos) == 1) else repo.name
        inventory = discover_dependencies(
            repo.path, repository=name, commit_sha=_commit_sha(repo.path), adapters=adapters_for_target(event.target)
        )
        inventories[name] = inventory
        impacts.append(analyze_inventory(inventory, event, hints=hints, root=repo.path))
    workspace = WorkspaceImpact(repositories=tuple(sorted(impacts, key=lambda r: r.repository)))
    return LocalAnalysis(event, hints, workspace, inventories, {r.name: r.path for r in repos})


async def _persist_local(analysis: LocalAnalysis, upsert_repository: UpsertRepository) -> dict[str, Any]:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    session_factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    store = UpstreamChangeStore()
    registry = DependencyRegistry()
    try:
        async with session_factory() as session:
            event_row, created = await store.record_event(session, analysis.event)
            await session.commit()
        impacts: dict[str, list[bool]] = {}
        for impact in analysis.workspace.repositories:
            repository_id = await upsert_repository(session_factory, full_name=impact.repository)
            async with session_factory() as session:
                await registry.record_inventory(
                    session, repository_id=repository_id, inventory=analysis.inventories[impact.repository]
                )
                rows = await registry.list_dependencies(session, repository_id=repository_id)
                dependency_ids = {r.dependency_key: r.id for r in rows}
                written = await store.record_impact(
                    session, event_id=event_row.id, repository_id=repository_id, impact=impact,
                    dependency_ids=dependency_ids,
                )
                await session.commit()
            impacts[impact.repository] = [c for _, c in written]
        return {
            "event_id": str(event_row.id),
            "event_created": created,
            "impacts_created": {name: sum(flags) for name, flags in impacts.items()},
        }
    finally:
        await engine.dispose()


async def _registry_analysis(event: ExternalChangeEvent, hints: ChangeHints, *, persist: bool) -> tuple[
    WorkspaceImpact, dict[str, Any] | None
]:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    store = UpstreamChangeStore()
    try:
        async with session_factory() as session:
            rows, repositories, dependency_ids, repository_ids = await store.registry_dependencies(session)
            workspace = registry_impact(rows, event, hints=hints, repositories=repositories)
            persisted: dict[str, Any] | None = None
            if persist:
                event_row, created = await store.record_event(session, event)
                created_impacts = 0
                for impact in workspace.repositories:
                    ids = {key: dep_id for (repo, key), dep_id in dependency_ids.items() if repo == impact.repository}
                    written = await store.record_impact(
                        session, event_id=event_row.id, repository_id=repository_ids[impact.repository],
                        impact=impact, dependency_ids=ids,
                    )
                    created_impacts += sum(1 for _, c in written if c)
                await session.commit()
                persisted = {"event_id": str(event_row.id), "event_created": created,
                             "impacts_created": created_impacts}
        return workspace, persisted
    finally:
        await engine.dispose()


def run_changes_analyze(args: argparse.Namespace, upsert_repository: UpsertRepository) -> int:
    if args.registry:
        if args.repo:
            raise CommandError("--registry analyzes registry evidence; do not also pass --repo")
        event, hints = build_event(args, first_repo=None)
        if event.kind.value == "version_update" and event.old.version is None:
            raise CommandError("--registry with a version change needs --from-version")
        workspace, persisted = asyncio.run(_registry_analysis(event, hints, persist=args.persist))
    else:
        if not args.repo:
            raise CommandError("give at least one --repo PATH (or NAME=PATH), or use --registry")
        analysis = analyze_local(args, [_repo_arg(r) for r in args.repo])
        event, workspace = analysis.event, analysis.workspace
        persisted = asyncio.run(_persist_local(analysis, upsert_repository)) if args.persist else None
    payload: dict[str, Any] = {"event": event_to_dict(event), "impact": workspace_to_dict(workspace)}
    if persisted is not None:
        payload["persisted"] = persisted
    if args.output:
        Path(args.output).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_event_text(event), end="")
        print(render_workspace_text(workspace), end="")
        if persisted is not None:
            print(f"persisted: {persisted}")
    return 0


# -- wiring ---------------------------------------------------------------------------------


def register(subparsers: Any) -> None:
    changes = subparsers.add_parser(
        "changes",
        help="M6: upstream API/SDK change detection, consumer impact and blast radius (offline, deterministic)",
    )
    changes_sub = changes.add_subparsers(dest="changes_command", required=True)

    diff = changes_sub.add_parser("diff", help="Compare two contracts (OpenAPI or SDK surface)")
    diff.add_argument("old_contract", type=Path)
    diff.add_argument("new_contract", type=Path)
    diff.add_argument("--hints", type=Path, help="Change hints (renames/replacements)")
    diff.add_argument("--json", action="store_true", help="Print the machine-readable change event")
    diff.add_argument("--all", action="store_true", help="Also list non-breaking changes")
    diff.add_argument("--output", default=None, help="Also write the JSON event to this path")

    analyze = changes_sub.add_parser(
        "analyze", help="Analyze an upstream change against local repositories (or the registry)"
    )
    add_change_inputs(analyze)
    analyze.add_argument("--repo", action="append", default=[],
                         help="Local repository checkout (repeatable; NAME=PATH to name it)")
    analyze.add_argument("--registry", action="store_true",
                         help="Use M5 registry evidence of every known repository instead of local checkouts")
    analyze.add_argument("--persist", action="store_true",
                         help="Record the event and impacts (and, for --repo, the dependency inventory)")
    analyze.add_argument("--json", action="store_true")
    analyze.add_argument("--output", default=None)


def dispatch(args: argparse.Namespace, *, upsert_repository: UpsertRepository) -> int:
    try:
        if args.command == "changes":
            if args.changes_command == "diff":
                return run_changes_diff(args)
            if args.changes_command == "analyze":
                return run_changes_analyze(args, upsert_repository)
    except (CommandError, ContractLoadError, SurfaceError, HintError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 1


__all__ = ["CommandError", "add_change_inputs", "analyze_local", "build_event", "dispatch", "register"]

