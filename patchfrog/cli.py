"""Developer CLI for repository indexing, static analysis, and context.

    python -m patchfrog.cli index --repository /path/to/repo [--full-name owner/repo]
    python -m patchfrog.cli analyze --repository /path/to/repo [--full-name owner/repo]
    python -m patchfrog.cli context --repository /path/to/repo --finding-id <id>
    python -m patchfrog.cli context --repository /path/to/repo --file src/foo.py --line 42

Deliberately minimal — a couple of subcommands, argparse only. This
exists purely as a controlled way to trigger indexing/analysis/context
generation during development and self-validation; it is not a
general-purpose CLI framework.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
import uuid
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.analysis.domain import AnalysisRunSummary
from patchfrog.analysis.service import StaleIndexError, StaticAnalysisService
from patchfrog.config.logging import configure_logging
from patchfrog.config.settings import get_settings
from patchfrog.context.domain import ContextBundle, ContextTargetType
from patchfrog.context.service import ContextService, StaleContextIndexError
from patchfrog.indexing.models import IndexingSummary
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.database import create_engine, create_session_factory
from patchfrog.persistence.models.analysis import FindingModel
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.repository.git import GitError

logger = structlog.get_logger(__name__)


def _synthetic_github_repository_id(full_name: str) -> int:
    """A stable, non-negative id for a repository with no real GitHub App installation.

    CLI-indexed repositories aren't necessarily backed by a GitHub App
    installation (e.g. indexing a scratch checkout), but ``repositories``
    still requires a unique ``github_repository_id`` — derived
    deterministically from the repository's full name so repeat CLI runs
    against the same repository reuse the same row (and therefore benefit
    from incremental indexing).
    """

    digest = hashlib.sha256(full_name.encode()).digest()[:8]
    return int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF


def _default_full_name(repository_path: Path) -> str:
    return repository_path.resolve().name


async def _upsert_cli_repository(
    session_factory: async_sessionmaker[AsyncSession], *, full_name: str
) -> uuid.UUID:
    owner, _, name = full_name.partition("/")
    async with session_factory() as session:
        repository_row = await RepositoryRepository().upsert(
            session,
            github_repository_id=_synthetic_github_repository_id(full_name),
            owner=owner or full_name,
            name=name or full_name,
            full_name=full_name,
            installation_id=0,
        )
        await session.commit()
        return repository_row.id


async def _index_local(*, repository_path: Path, full_name: str) -> IndexingSummary:
    settings = get_settings()
    configure_logging(settings.log_level)

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        repository_id = await _upsert_cli_repository(session_factory, full_name=full_name)

        service = RepositoryIndexingService(session_factory=session_factory)
        return await service.index_local_repository(
            repository_id=repository_id,
            root_path=repository_path,
            repository_full_name=full_name,
        )
    finally:
        await engine.dispose()


async def _analyze_local(*, repository_path: Path, full_name: str) -> AnalysisRunSummary:
    settings = get_settings()
    configure_logging(settings.log_level)

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        repository_id = await _upsert_cli_repository(session_factory, full_name=full_name)

        service = StaticAnalysisService(session_factory=session_factory)
        return await service.analyze_local_repository(
            repository_id=repository_id,
            root_path=repository_path,
            repository_full_name=full_name,
        )
    finally:
        await engine.dispose()


async def _context_local(
    *, repository_path: Path, full_name: str, finding_id: uuid.UUID | None, file_path: str | None, line: int | None
) -> ContextBundle:
    settings = get_settings()
    configure_logging(settings.log_level)

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        repository_id = await _upsert_cli_repository(session_factory, full_name=full_name)
        service = ContextService(session_factory=session_factory)

        if finding_id is not None:
            async with session_factory() as session:
                finding = await session.get(FindingModel, finding_id)
            if finding is None:
                raise ValueError(f"no finding with id {finding_id}")
            return await service.build_context_local(
                repository_id=repository_id,
                root_path=repository_path,
                repository_full_name=full_name,
                target_type=ContextTargetType.FINDING,
                file_path=finding.file_path,
                line=finding.start_line,
                symbol_id=finding.symbol_id,
                finding_id=finding.id,
                analysis_run_id=finding.analysis_run_id,
            )

        assert file_path is not None and line is not None  # enforced by argparse mutual-exclusion below
        return await service.build_context_local(
            repository_id=repository_id,
            root_path=repository_path,
            repository_full_name=full_name,
            target_type=ContextTargetType.LINE,
            file_path=file_path,
            line=line,
        )
    finally:
        await engine.dispose()


def _run_index(args: argparse.Namespace) -> int:
    repository_path: Path = args.repository
    if not repository_path.is_dir():
        print(f"error: not a directory: {repository_path}", file=sys.stderr)
        return 1
    if not (repository_path / ".git").exists():
        print(f"error: not a git repository (no .git found): {repository_path}", file=sys.stderr)
        return 1

    full_name = args.full_name or _default_full_name(repository_path)
    try:
        summary = asyncio.run(_index_local(repository_path=repository_path, full_name=full_name))
    except GitError as exc:
        # Expected, user-actionable failures (dirty worktree, no commits
        # yet, git not usable, ...) — a clean one-line message, not a
        # Python traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"indexed {full_name}: files_total={summary.files_total} "
        f"files_parsed={summary.files_parsed} files_reused={summary.files_reused} "
        f"files_failed={summary.files_failed} symbols_extracted={summary.symbols_extracted} "
        f"edges_created={summary.edges_created} duration_ms={summary.duration_ms:.1f} "
        f"incremental={summary.incremental}"
    )
    return 0


def _run_analyze(args: argparse.Namespace) -> int:
    repository_path: Path = args.repository
    if not repository_path.is_dir():
        print(f"error: not a directory: {repository_path}", file=sys.stderr)
        return 1
    if not (repository_path / ".git").exists():
        print(f"error: not a git repository (no .git found): {repository_path}", file=sys.stderr)
        return 1

    full_name = args.full_name or _default_full_name(repository_path)
    try:
        summary = asyncio.run(_analyze_local(repository_path=repository_path, full_name=full_name))
    except GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except StaleIndexError as exc:
        print(f"error: {exc} — run 'index' for this commit first", file=sys.stderr)
        return 1

    print(
        f"analyzed {full_name}: status={summary.status.value} "
        f"analyzers_succeeded={summary.analyzers_succeeded} analyzers_failed={summary.analyzers_failed} "
        f"analyzers_skipped={summary.analyzers_skipped} raw_findings={summary.raw_findings_count} "
        f"findings={summary.findings_count} duration_ms={summary.duration_ms:.1f} "
        f"reused_existing_run={summary.reused_existing_run}"
    )
    return 0


def _run_context(args: argparse.Namespace) -> int:
    repository_path: Path = args.repository
    if not repository_path.is_dir():
        print(f"error: not a directory: {repository_path}", file=sys.stderr)
        return 1
    if not (repository_path / ".git").exists():
        print(f"error: not a git repository (no .git found): {repository_path}", file=sys.stderr)
        return 1
    if args.finding_id is None and (args.file is None or args.line is None):
        print("error: either --finding-id, or both --file and --line, are required", file=sys.stderr)
        return 1

    full_name = args.full_name or _default_full_name(repository_path)
    finding_id = uuid.UUID(args.finding_id) if args.finding_id else None
    try:
        bundle = asyncio.run(
            _context_local(
                repository_path=repository_path,
                full_name=full_name,
                finding_id=finding_id,
                file_path=args.file,
                line=args.line,
            )
        )
    except GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except StaleContextIndexError as exc:
        print(f"error: {exc} — run 'index' for this commit first", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"context for {full_name}: target={bundle.target.target_type.value}:{bundle.target.file_path} "
        f"items={len(bundle.items)}/{bundle.metrics.candidate_count} tokens={bundle.total_tokens_estimate} "
        f"lines={bundle.total_lines} generation_ms={bundle.metrics.generation_ms:.1f} "
        f"reused={bundle.reused_existing_bundle}"
    )
    for rank, item in enumerate(bundle.items):
        print(
            f"  [{rank}] score={item.score:.3f} kind={item.kind.value} relationship={item.relationship.value} "
            f"file={item.file_path} lines={item.start_line}-{item.end_line} "
            f"tokens={item.estimated_tokens} truncated={item.truncated} reason={item.reason!r}"
        )
        if args.show_content:
            for content_line in item.content.splitlines():
                print(f"      | {content_line}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="patchfrog.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser("index", help="Index a local repository checkout")
    index_parser.add_argument(
        "--repository", required=True, type=Path, help="Path to a local git checkout"
    )
    index_parser.add_argument(
        "--full-name",
        default=None,
        help="Repository identity, e.g. 'owner/repo' (defaults to the directory name)",
    )

    analyze_parser = subparsers.add_parser(
        "analyze", help="Run static analysis on a local repository checkout (requires 'index' first)"
    )
    analyze_parser.add_argument(
        "--repository", required=True, type=Path, help="Path to a local git checkout"
    )
    analyze_parser.add_argument(
        "--full-name",
        default=None,
        help="Repository identity, e.g. 'owner/repo' (defaults to the directory name)",
    )

    context_parser = subparsers.add_parser(
        "context", help="Build a deterministic context bundle for a finding or file/line (requires 'index' first)"
    )
    context_parser.add_argument(
        "--repository", required=True, type=Path, help="Path to a local git checkout"
    )
    context_parser.add_argument(
        "--full-name",
        default=None,
        help="Repository identity, e.g. 'owner/repo' (defaults to the directory name)",
    )
    context_parser.add_argument("--finding-id", default=None, help="Build context for this Finding's id")
    context_parser.add_argument("--file", default=None, help="Repo-relative file path (with --line)")
    context_parser.add_argument("--line", default=None, type=int, help="1-indexed line number (with --file)")
    context_parser.add_argument(
        "--show-content", action="store_true", help="Also print each item's extracted source content"
    )

    args = parser.parse_args(argv)
    if args.command == "index":
        return _run_index(args)
    if args.command == "analyze":
        return _run_analyze(args)
    if args.command == "context":
        return _run_context(args)

    return 1


if __name__ == "__main__":
    sys.exit(main())
