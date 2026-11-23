"""``patchfrog scale ...``: local application discovery (Milestone 2.5).

A thin adapter over :func:`patchfrog.app.discovery.run_discovery` (JobRunner,
RepositoryRevision, LocalRunStore). It parses arguments, calls the use case
and renders the stored result; it holds no discovery logic.

    python -m patchfrog scale analyze [PATH] [--json] [--details] [--strict]
    python -m patchfrog scale show RUN_ID [--json] [--details]
    python -m patchfrog scale schema

(``python -m patchfrog.cli scale ...`` works too, but loads the whole
review/compatibility CLI first; ``python -m patchfrog`` dispatches ``scale``
without it.)

Nothing here needs a database, Redis, Celery, GitHub or an AI provider, and
nothing in the analysed repository is imported or executed. Discovery is
static, so PatchFrog reports structure only: no findings, scores or capacity
estimates (Phase 3 adds rules).

Exit codes (stable, documented in docs/scale-cli.md):

==  ===================================================================
0   run SUCCEEDED (with ``--strict``: and discovery coverage is complete)
1   unexpected internal error
2   usage or input error (bad path, invalid run id, unsafe store)
3   run PARTIAL (a budget or limit stopped work, or files were skipped);
    with ``--strict`` also a SUCCEEDED run whose coverage is incomplete
4   run FAILED, CANCELLED, INTERRUPTED or still RUNNING (no result)
5   run not found
6   stored run or artifact is corrupt, of another kind, or of an
    unsupported schema version
130 interrupted by the user (Ctrl-C); the run is recorded as FAILED
==  ===================================================================
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from patchfrog.app.discovery import (
    DISCOVERY_ARTIFACT,
    DISCOVERY_KIND,
    DiscoveryArtifactError,
    load_discovery,
    run_discovery,
)
from patchfrog.app.jobs import JOB_REPORT_ARTIFACT, JobBudget, JobError, JobReport, JobRunner
from patchfrog.app.revision import RepositoryRevision, RevisionScope
from patchfrog.app.store import (
    CorruptRecordError,
    IncompatibleSchemaError,
    LocalRunStore,
    RunNotFoundError,
    RunRecord,
    RunState,
    StoreError,
)
from patchfrog.scalability.project import ApplicationModel, DiscoveryLimits
from patchfrog.scalability.project.schema import application_model_json_schema_text

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_USAGE = 2
EXIT_PARTIAL = 3
EXIT_NO_RESULT = 4
EXIT_NOT_FOUND = 5
EXIT_CORRUPT = 6
EXIT_INTERRUPTED = 130

OUTPUT_FORMAT = "patchfrog.scale.run"
OUTPUT_FORMAT_VERSION = 1
STORE_ENV = "PATCHFROG_STORE_DIR"
_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_LIST_LIMIT = 10  # entries per section in the compact view


class ScaleError(Exception):
    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# --- arguments -------------------------------------------------------------------------------


def _add_store(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--store",
        type=Path,
        help=f"run store directory (default: ${STORE_ENV}, else $XDG_CACHE_HOME/patchfrog/store, "
        "else ~/.cache/patchfrog/store). Never read from the analysed repository.",
    )


def _add_output(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--json", action="store_true", help="print the run and the full discovery model as JSON")
    group.add_argument("--details", action="store_true", help="list every route, model, provider and unresolved reference")


def _configure(scale: argparse.ArgumentParser) -> None:
    sub = scale.add_subparsers(dest="scale_command", required=True, metavar="{analyze,show,schema}")

    analyze = sub.add_parser(
        "analyze",
        help="discover a local Python application's structure (static; nothing is executed)",
        description=(
            "Statically discover FastAPI apps, routes and dependencies, and SQLAlchemy/SQLModel models, "
            "sessions and session providers, in a local directory. The result is stored as an immutable, "
            "revision-bound run; an identical request (same file contents, limits and engine) reuses it. "
            "Reports structure only: no findings, scores or capacity estimates."
        ),
    )
    analyze.add_argument("path", nargs="?", default=".", type=Path, help="repository or directory (default: .)")
    _add_store(analyze)
    _add_output(analyze)
    analyze.add_argument("--strict", action="store_true", help="exit 3 unless discovery coverage is complete")
    analyze.add_argument("--max-seconds", type=float, default=600.0, help="wall-time budget (default: 600)")
    analyze.add_argument("--max-files", type=int, default=DiscoveryLimits().max_python_files, help="Python files analysed at most (default: %(default)s)")
    analyze.add_argument(
        "--exclude", action="append", default=[], metavar="PREFIX",
        help="exclude a repository-relative path prefix from the snapshot (repeatable)",
    )
    analyze.add_argument("--tracked-only", action="store_true", help="Git repositories: ignore untracked files")

    show = sub.add_parser("show", help="show a stored discovery run", description="Show a stored discovery run by its run id.")
    show.add_argument("run_id", help="the 32-character run id printed by 'scale analyze'")
    _add_store(show)
    _add_output(show)

    sub.add_parser(
        "schema", help="print the JSON Schema of the discovery model",
        description="Print the JSON Schema (draft 2020-12) of the ApplicationModel stored as discovery.json.",
    )


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    _configure(subparsers.add_parser("scale", help="local application discovery (scalability engine)"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="patchfrog scale", description="Local application discovery.")
    _configure(parser)
    return parser


# --- environment ------------------------------------------------------------------------------


def engine_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("patchfrog")
    except PackageNotFoundError:  # running from a source tree that isn't installed
        return "0+unknown"


def default_store_root() -> Path:
    """User-level locations only. ``CoreSettings`` is deliberately not used:
    it reads ``.env`` from the working directory, which is the analysed
    repository when run there, and repository content must not choose where
    PatchFrog writes."""

    configured = os.environ.get(STORE_ENV)
    if configured:
        return Path(configured).expanduser()
    cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(cache).expanduser() if cache and Path(cache).is_absolute() else Path.home() / ".cache"
    return base / "patchfrog" / "store"


def _open_store(path: Path | None) -> LocalRunStore:
    root = (path or default_store_root()).expanduser()
    try:
        return LocalRunStore(root)
    except IncompatibleSchemaError as exc:
        raise ScaleError(f"store {root}: {exc}", EXIT_CORRUPT) from exc
    except (StoreError, OSError) as exc:
        raise ScaleError(f"cannot use store {root}: {exc}", EXIT_USAGE) from exc


# --- payloads ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _View:
    record: RunRecord
    report: JobReport | None
    model: ApplicationModel | None
    reused: bool
    revision: RepositoryRevision | None
    store_root: Path

    @property
    def complete(self) -> bool:
        # Never true for a partial, failed or model-less run.
        return self.record.state is RunState.SUCCEEDED and self.model is not None and self.model.coverage.complete


def _payload(view: _View) -> dict[str, object]:
    record, report = view.record, view.report
    revision: dict[str, object] = {"revision_id": record.revision_id}
    if view.revision is not None:
        revision.update(
            kind=view.revision.kind.value,
            commit_sha=view.revision.commit_sha,
            dirty=view.revision.dirty,
            changes=None if view.revision.changes is None else vars(view.revision.changes).copy(),
            file_count=view.revision.file_count,
            total_bytes=view.revision.total_bytes,
        )
    return {
        "format": OUTPUT_FORMAT,
        "format_version": OUTPUT_FORMAT_VERSION,
        "run": {
            "run_id": record.run_id,
            "state": record.state.value,
            "reason": record.reason,
            "reused": view.reused,
            "created_at": record.created_at,
            "finished_at": record.finished_at,
            "engine_version": record.metadata.get("engine_version"),
            "ruleset_version": record.metadata.get("ruleset_version"),
            "failure_class": None if report is None or report.failure_class is None else report.failure_class.value,
            "partial_reasons": [] if report is None else list(report.partial_reasons),
            "budget_usage": None if report is None else vars(report.budget_usage).copy(),
            "store": str(view.store_root),
        },
        "revision": revision,
        "complete": view.complete,
        "model": None if view.model is None else view.model.to_dict(),
    }


def _read_report(store: LocalRunStore, record: RunRecord) -> JobReport | None:
    ref = record.artifacts.get(JOB_REPORT_ARTIFACT)
    if ref is None:
        return None
    try:
        return JobReport.from_dict(json.loads(store.read_bytes(ref, max_bytes=1024 * 1024)))
    except (StoreError, ValueError) as exc:
        raise ScaleError(f"run {record.run_id}: job report is corrupt ({type(exc).__name__})", EXIT_CORRUPT) from exc


# --- rendering ---------------------------------------------------------------------------------


def _short(value: str | None, size: int = 12) -> str:
    return "-" if value is None else value[:size]


def _frameworks(model: ApplicationModel) -> str:
    parts = []
    for f in model.frameworks:
        if f.resolved_version:
            parts.append(f"{f.package} {f.resolved_version} (locked)")
        elif f.version_status.value == "conflicting":
            parts.append(f"{f.package} (lockfiles disagree)")
        elif f.declarations:
            spec = next((d.spec for d in f.declarations if d.spec), None)
            parts.append(f"{f.package} {spec or ''} (declared, no lockfile)".replace("  ", " "))
        else:
            parts.append(f"{f.package} (imported only, version unknown)")
    return ", ".join(parts) if parts else "none detected"


def render_text(view: _View, *, details: bool) -> str:
    record, model = view.record, view.model
    lines = [
        f"PatchFrog application discovery  run {record.run_id}  [{record.state.value}{', reused' if view.reused else ''}]"
    ]
    revision = view.revision
    if revision is not None:
        provenance = "directory" if revision.commit_sha is None else f"git {_short(revision.commit_sha)}"
        if revision.dirty:
            changes = revision.changes
            counts = "" if changes is None else f": {changes.modified} modified, {changes.added} added, {changes.deleted} deleted"
            provenance += f", uncommitted changes{counts}"
        lines.append(f"Revision   {_short(record.revision_id, 16)}  ({provenance}; {revision.file_count} files)")
    else:
        lines.append(f"Revision   {_short(record.revision_id, 16)}")
    if model is None:
        lines.append(f"Result     none ({record.reason or record.state.value})")
        lines.append("Coverage   NOT COMPLETE: the run produced no discovery result")
        return "\n".join(lines)

    routes, db = model.routes, model.database
    attached = [r for r in routes if r.app_id is not None]
    kinds = Counter(r.endpoint.kind.value for r in routes)
    lines.append(f"Frameworks {_frameworks(model)}")
    lines.append(
        f"Web        {len(model.apps)} app(s), {len(model.routers) - len(model.apps)} router(s), {len(attached)} route(s)"
        f" ({kinds.get('async', 0)} async, {kinds.get('sync', 0)} sync, {kinds.get('unknown', 0)} unknown)"
        + (f", {len(routes) - len(attached)} on unattached routers" if len(routes) > len(attached) else "")
    )
    relationships = [r for m in db.models for r in m.relationships]
    lazy = Counter(f"{r.lazy}{'' if r.lazy_provenance.value == 'explicit' else ' (' + r.lazy_provenance.value + ')'}" for r in relationships)
    lazy_text = ", ".join(f"{k} x{v}" for k, v in sorted(lazy.items(), key=lambda kv: (-kv[1], kv[0])))
    lines.append(
        f"Database   {len(db.models)} model(s), {len(relationships)} relationship(s)"
        + (f" [lazy: {lazy_text}]" if lazy_text else "")
        + f", {sum(1 for o in db.objects if 'engine' in o.kind.value)} engine(s),"
        f" {sum(1 for o in db.objects if 'session' in o.kind.value)} session factory/session object(s),"
        f" {len(db.session_providers)} session provider(s), {len({s.route_id for s in db.route_sessions})} route(s) reach a session"
    )
    coverage = model.coverage
    unresolved = len(model.unresolved) + len(db.unresolved)
    if view.complete:
        lines.append(f"Coverage   complete ({coverage.parsed_files} of {coverage.python_files} Python files parsed; the rest have no routes or models)")
    else:
        why = []
        if record.state is RunState.PARTIAL:
            why.append("run PARTIAL")
        if unresolved:
            why.append(f"{unresolved} unresolved reference(s)")
        if model.limitations:
            why.append(f"{len(model.limitations)} limitation(s)")
        lines.append(f"Coverage   NOT COMPLETE: {', '.join(why) or 'see limitations'}")

    limit = None if details else _LIST_LIMIT
    if details:
        lines.append("")
        lines.append("Routes")
        for r in routes:
            app = r.app_id or "(unattached)"
            lines.append(f"  {','.join(r.methods):<8} {r.path:<40} {r.endpoint.kind.value:<6} {r.endpoint.symbol or '?'}  [{app}]")
        lines.append("Models")
        for m in db.models:
            table = f"{m.table_name or '?'} ({m.table_name_provenance.value})"
            lines.append(f"  {m.symbol:<40} table {table}  pk {','.join(m.primary_key) or '?'} ({m.primary_key_provenance.value}){'' if m.complete else '  INCOMPLETE'}")
            for rel in m.relationships:
                lines.append(
                    f"      {rel.name} -> {rel.target_model or rel.target_expression} ({rel.resolution.value}),"
                    f" lazy={rel.lazy} ({rel.lazy_provenance.value})"
                )
        lines.append("Session providers")
        for p in db.session_providers:
            lines.append(f"  {p.symbol}  {p.shape.value}, {p.session_kind.value} ({p.provenance.value}) from {p.source or '?'}")
    entries = [(u.location.path, u.location.line, u.kind.value, u.expression, u.reason) for u in model.unresolved]
    entries += [(u.location.path, u.location.line, u.kind.value, u.expression, u.reason) for u in db.unresolved]
    if entries:
        lines.append("")
        lines.append(f"Unresolved ({len(entries)}; nothing below was guessed)")
        for path, line, kind, expression, reason in sorted(entries)[:limit]:
            lines.append(f"  {path}:{line}  {kind}: {expression}  ({reason})")
        if limit is not None and len(entries) > limit:
            lines.append(f"  ... {len(entries) - limit} more (use --details or --json)")
    if model.limitations:
        lines.append("")
        lines.append("Limitations")
        for x in model.limitations[:limit]:
            where = f"{x.location.path}:{x.location.line}  " if x.location else ""
            lines.append(f"  {where}{x.code.value}: {x.detail}")
        if limit is not None and len(model.limitations) > limit:
            lines.append(f"  ... {len(model.limitations) - limit} more (use --details or --json)")
    if view.report is not None and view.report.partial_reasons and record.state is RunState.PARTIAL:
        lines.append("")
        lines.append("Partial because")
        for reason in view.report.partial_reasons[:limit]:
            lines.append(f"  {reason}")
    lines.append("")
    lines.append(f"Show again: python -m patchfrog scale show {record.run_id}")
    lines.append("Static structure only: no performance findings yet (detection rules arrive in a later phase).")
    return "\n".join(lines)


def _emit(view: _View, args: argparse.Namespace) -> None:
    if args.json:
        sys.stdout.write(json.dumps(_payload(view), indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_text(view, details=args.details) + "\n")


def _exit_code(view: _View, *, strict: bool) -> int:
    state = view.record.state
    if state is RunState.SUCCEEDED:
        return EXIT_PARTIAL if strict and not view.complete else EXIT_OK
    if state is RunState.PARTIAL:
        return EXIT_PARTIAL
    return EXIT_NO_RESULT


# --- commands ------------------------------------------------------------------------------------


def _analyze(args: argparse.Namespace) -> int:
    path: Path = args.path
    if not path.exists():
        raise ScaleError(f"path does not exist: {path}", EXIT_USAGE)
    if not path.is_dir():
        raise ScaleError(f"not a directory: {path}", EXIT_USAGE)
    if args.max_files < 1 or not args.max_seconds > 0:
        raise ScaleError("--max-files and --max-seconds must be positive", EXIT_USAGE)
    try:
        scope = RevisionScope(include_untracked=not args.tracked_only, exclude_prefixes=tuple(args.exclude))
    except ValueError as exc:
        raise ScaleError(str(exc), EXIT_USAGE) from exc
    store = _open_store(args.store)
    limits = DiscoveryLimits(max_python_files=args.max_files)
    try:
        result = run_discovery(
            store, path, engine_version=engine_version(), limits=limits, scope=scope,
            budget=JobBudget(max_seconds=args.max_seconds), runner=JobRunner(store),
        )
    except JobError as exc:
        code = EXIT_USAGE if exc.failure_class.value == "input_error" else EXIT_PARTIAL
        hint = " (retry later: another identical run holds the lock)" if exc.retryable else ""
        raise ScaleError(f"{exc.failure_class.value}: {exc}{hint}", code) from exc
    except DiscoveryArtifactError as exc:
        raise ScaleError(str(exc), EXIT_CORRUPT) from exc
    view = _View(result.job.record, result.job.report, result.model, result.job.reused, result.job.revision, store.root)
    _emit(view, args)
    return _exit_code(view, strict=args.strict)


def _show(args: argparse.Namespace) -> int:
    run_id: str = args.run_id.strip().lower()
    if not _RUN_ID.match(run_id):
        raise ScaleError(f"invalid run id {args.run_id!r}: expected 32 lowercase hex characters", EXIT_USAGE)
    store = _open_store(args.store)
    try:
        record = store.get_run(run_id)
    except RunNotFoundError as exc:
        raise ScaleError(f"run {run_id} not found in {store.root}", EXIT_NOT_FOUND) from exc
    except (CorruptRecordError, IncompatibleSchemaError) as exc:
        raise ScaleError(f"run {run_id}: {exc}", EXIT_CORRUPT) from exc
    except StoreError as exc:
        raise ScaleError(f"run {run_id}: {exc}", EXIT_CORRUPT) from exc
    if record.kind != DISCOVERY_KIND:
        raise ScaleError(f"run {run_id} is a {record.kind!r} run, not a discovery run", EXIT_CORRUPT)
    report = _read_report(store, record)
    try:
        model = load_discovery(store, record)
    except DiscoveryArtifactError as exc:
        raise ScaleError(f"run {run_id}: {exc}", EXIT_CORRUPT) from exc
    if model is None and record.state in (RunState.SUCCEEDED, RunState.PARTIAL):
        raise ScaleError(f"run {run_id} is {record.state.value} but has no {DISCOVERY_ARTIFACT}", EXIT_CORRUPT)
    view = _View(record, report, model, False, None, store.root)
    _emit(view, args)
    return _exit_code(view, strict=False)


def dispatch(args: argparse.Namespace) -> int:
    try:
        if args.scale_command == "analyze":
            return _analyze(args)
        if args.scale_command == "show":
            return _show(args)
        if args.scale_command == "schema":
            sys.stdout.write(application_model_json_schema_text())
            return EXIT_OK
    except ScaleError as exc:
        sys.stderr.write(f"patchfrog scale: error: {exc}\n")
        return exc.exit_code
    except KeyboardInterrupt:
        sys.stderr.write("patchfrog scale: interrupted (the run is recorded as failed: interrupted)\n")
        return EXIT_INTERRUPTED
    return EXIT_USAGE


def main(argv: Sequence[str] | None = None) -> int:
    return dispatch(build_parser().parse_args(argv))


__all__ = ["build_parser", "dispatch", "main", "register"]
