"""Developer CLI for repository indexing.

    python -m patchfrog.cli index --repository /path/to/repo [--full-name owner/repo]

Deliberately minimal — one subcommand, argparse only. This exists purely
as a controlled way to trigger indexing during Phase 2 development and
self-validation; it is not a general-purpose CLI framework.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from pathlib import Path

import structlog

from patchfrog.config.logging import configure_logging
from patchfrog.config.settings import get_settings
from patchfrog.indexing.models import IndexingSummary
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.database import create_engine, create_session_factory
from patchfrog.persistence.repositories import RepositoryRepository

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


async def _index_local(*, repository_path: Path, full_name: str) -> IndexingSummary:
    settings = get_settings()
    configure_logging(settings.log_level)

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
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
            repository_id = repository_row.id

        service = RepositoryIndexingService(session_factory=session_factory)
        return await service.index_local_repository(
            repository_id=repository_id,
            root_path=repository_path,
            repository_full_name=full_name,
        )
    finally:
        await engine.dispose()


def _run_index(args: argparse.Namespace) -> int:
    repository_path: Path = args.repository
    full_name = args.full_name or _default_full_name(repository_path)
    summary = asyncio.run(_index_local(repository_path=repository_path, full_name=full_name))
    print(
        f"indexed {full_name}: files_total={summary.files_total} "
        f"files_parsed={summary.files_parsed} files_reused={summary.files_reused} "
        f"files_failed={summary.files_failed} symbols_extracted={summary.symbols_extracted} "
        f"edges_created={summary.edges_created} duration_ms={summary.duration_ms:.1f} "
        f"incremental={summary.incremental}"
    )
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

    args = parser.parse_args(argv)
    if args.command == "index":
        return _run_index(args)

    return 1


if __name__ == "__main__":
    sys.exit(main())
