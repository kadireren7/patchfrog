"""``patchfrog campaigns ...`` -- M10 organization compatibility campaigns, offline.

``analyze`` evaluates one upstream change against several local checkouts the
way a hosted workspace would (freshness, access, blast radius, per-repo
migration and verification) without GitHub or a database. ``demo`` runs the
bundled deterministic end-to-end scenario. Nothing here calls a model provider
or writes to GitHub.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from patchfrog.campaigns.demo import DemoReport, DemoUnavailable, run_full_demo
from patchfrog.campaigns.domain import CompatibilityCampaign, EnrolledRepository, RepositoryAccess
from patchfrog.campaigns.dossier import campaign_to_dict, render_campaign_markdown
from patchfrog.campaigns.evaluate import RepositoryInput
from patchfrog.campaigns.orchestrate import run_campaign
from patchfrog.campaigns.policy import WatchMode, WorkspacePolicy
from patchfrog.cli_changes import CommandError, _repo_arg, add_change_inputs, build_event
from patchfrog.upstream.events import ContractLoadError
from patchfrog.upstream.hints import HintError
from patchfrog.upstream.sdk_surface import SurfaceError

_POLICIES = {"detect_only": WatchMode.DETECT_ONLY, "migrate": WatchMode.MIGRATE}


def _entries(args: argparse.Namespace, now: datetime) -> list[RepositoryInput]:
    if not args.repo:
        raise CommandError("give at least one --repo PATH (or NAME=PATH)")
    repos = [_repo_arg(r) for r in args.repo]
    names = {r.name for r in repos}
    for flag in ("stale", "unknown", "access_lost"):
        missing = set(getattr(args, flag)) - names
        if missing:
            raise CommandError(f"--{flag.replace('_', '-')} names a repository not given as --repo: {sorted(missing)}")
    entries: list[RepositoryInput] = []
    for repo in repos:
        if not repo.path.is_dir():
            raise CommandError(f"not a directory: {repo.path}")
        discovered: datetime | None = now - timedelta(minutes=5)
        access = RepositoryAccess.AVAILABLE
        if repo.name in args.stale:
            discovered = now - timedelta(days=args.freshness_days + 1)
        if repo.name in args.unknown:
            discovered = None
        if repo.name in args.access_lost:
            access = RepositoryAccess.ACCESS_LOST
        entries.append(
            RepositoryInput(
                repository=EnrolledRepository(full_name=repo.name, access=access, last_discovery_at=discovered),
                root=repo.path,
            )
        )
    return entries


def _print_campaign(campaign: CompatibilityCampaign, args: argparse.Namespace) -> None:
    payload = campaign_to_dict(campaign)
    if args.output:
        Path(args.output).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_campaign_markdown(campaign), end="")


def run_campaigns_analyze(args: argparse.Namespace) -> int:
    from patchfrog.campaigns.freshness import FreshnessPolicy

    now = datetime.fromisoformat(args.now).astimezone(UTC) if args.now else datetime.now(UTC)
    entries = _entries(args, now)
    event, hints = build_event(args, first_repo=entries[0].root)
    run = asyncio.run(
        run_campaign(
            workspace_key=args.workspace, event=event, entries=entries, now=now,
            policy=WorkspacePolicy(watch_mode=_POLICIES[args.policy]),
            freshness_policy=FreshnessPolicy(window=timedelta(days=args.freshness_days)), hints=hints,
        )
    )
    _print_campaign(run.campaign, args)
    return 0


def _demo_payload(report: DemoReport) -> dict[str, Any]:
    def summarize(result: Any) -> dict[str, Any]:
        assert result is not None and result.run is not None
        return {
            "campaign_created": result.campaign_created,
            "dossier": campaign_to_dict(result.run.campaign),
        }

    return {
        "watcher": [o.kind.value for o in report.watcher],
        "first_run": summarize(report.first),
        "rerun": summarize(report.rerun),
        "after_fixes": summarize(report.resolved),
        "campaigns_in_database": report.campaigns_in_database,
        "pull_requests_in_database": report.pull_requests_in_database,
        "github_pr_creations": report.github_pr_creations,
        "github_pr_drafts": report.github_pr_drafts,
    }


def _states(result: Any) -> str:
    campaign = result.run.campaign
    return ", ".join(f"{r.repository.split('/')[-1]}={r.state.value}" for r in campaign.records)


def render_demo_text(report: DemoReport) -> str:
    assert report.first and report.first.run and report.rerun and report.rerun.run
    assert report.resolved and report.resolved.run
    first, rerun, resolved = report.first.run.campaign, report.rerun.run.campaign, report.resolved.run.campaign
    lines = [
        "PatchFrog compatibility campaign demo (fictional 'acme-ai' SDK; offline, no live provider call)",
        "",
        "1. Watcher",
        "   " + " -> ".join(o.kind.value for o in report.watcher)
        + "   (first poll is a baseline; the change is detected once; polling again emits nothing)",
        "",
        "2. First run: one campaign, four repositories",
        f"   campaign state: {first.state.value}   organization safe: {first.org_blast_radius.safe}",
        f"   {_states(report.first)}",
        f"   PRs opened: {report.github_pr_creations}   human-required actions: "
        f"{sum(1 for r in first.records if r.state.value == 'human_required')}",
        "",
        "3. Re-run with identical inputs",
        f"   campaign created again: {report.rerun.campaign_created}   version {first.version} -> {rerun.version}",
        f"   campaigns in database: {report.campaigns_in_database}   PRs in database: "
        f"{report.pull_requests_in_database}   GitHub PR creations: {report.github_pr_creations}",
        "",
        "4. After the PR lands, the human fix is made and the stale repository is rediscovered",
        f"   campaign state: {resolved.state.value} (v{resolved.version})   organization safe: "
        f"{resolved.org_blast_radius.safe}",
        f"   {_states(report.resolved)}",
        "",
    ]
    return "\n".join(lines) + "\n"


def run_campaigns_demo(args: argparse.Namespace) -> int:
    try:
        report = asyncio.run(run_full_demo())
    except DemoUnavailable as exc:
        raise CommandError(str(exc)) from exc
    if args.json:
        print(json.dumps(_demo_payload(report), indent=2, sort_keys=True))
        return 0
    print(render_demo_text(report), end="")
    if report.first and report.first.run:
        print("First-run dossier:\n")
        print(render_campaign_markdown(report.first.run.campaign), end="")
    return 0


def register(subparsers: Any) -> None:
    campaigns = subparsers.add_parser(
        "campaigns",
        help="M10: organization-wide compatibility campaigns for one upstream change (offline, deterministic)",
    )
    sub = campaigns.add_subparsers(dest="campaigns_command", required=True)

    analyze = sub.add_parser(
        "analyze", help="Evaluate one upstream change across several local checkouts as one campaign"
    )
    add_change_inputs(analyze)
    analyze.add_argument("--repo", action="append", default=[], help="Local checkout (repeatable; NAME=PATH to name it)")
    analyze.add_argument("--policy", choices=sorted(_POLICIES), default="detect_only",
                         help="detect_only classifies; migrate also plans, generates and verifies (never publishes)")
    analyze.add_argument("--stale", action="append", default=[], metavar="NAME",
                         help="Treat NAME's dependency discovery as older than the freshness window")
    analyze.add_argument("--unknown", action="append", default=[], metavar="NAME",
                         help="Treat NAME as never discovered")
    analyze.add_argument("--access-lost", action="append", default=[], metavar="NAME",
                         help="Treat NAME as no longer readable under the installation")
    analyze.add_argument("--freshness-days", type=int, default=7, help="Freshness window (default 7)")
    analyze.add_argument("--workspace", default="local", help="Workspace identity (default 'local')")
    analyze.add_argument("--now", default=None, help="ISO timestamp to evaluate at (default: now)")
    analyze.add_argument("--json", action="store_true", help="Print the machine-readable dossier")
    analyze.add_argument("--output", default=None, help="Also write the JSON dossier to this path")

    demo = sub.add_parser(
        "demo", help="The bundled four-repository watcher -> campaign -> PR -> resolution demo (offline)"
    )
    demo.add_argument("--json", action="store_true")


def dispatch(args: argparse.Namespace) -> int:
    try:
        if args.campaigns_command == "analyze":
            return run_campaigns_analyze(args)
        if args.campaigns_command == "demo":
            return run_campaigns_demo(args)
    except (CommandError, ContractLoadError, SurfaceError, HintError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 1


__all__ = ["dispatch", "register", "render_demo_text", "run_campaigns_demo"]
