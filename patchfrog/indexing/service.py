"""Repository indexing orchestration.

Wires together: snapshot acquisition -> file inventory -> Tree-sitter
parsing (content-hash cached, see :mod:`patchfrog.indexing.parse_cache`)
-> repository-wide resolution (:mod:`patchfrog.intelligence.resolution`)
-> graph construction (:mod:`patchfrog.intelligence.graph`) -> persistence,
as one indexing run tied to a commit SHA.

Transaction strategy: creating the ``repository_indexes`` row (status
``running``) is committed immediately so it's observable while indexing
runs. Every row the pipeline produces — files, symbols, imports, calls,
edges — is then inserted in a single second transaction that only
commits once the whole pipeline has succeeded, ending with
:meth:`~patchfrog.persistence.repositories.repository_index.RepositoryIndexRepository.mark_succeeded`
flipping ``is_active``. Any exception before that commit leaves nothing
persisted beyond the ``running`` row, which a third, independent
transaction then marks ``failed`` — the previously active index (if any)
is never touched by a failed run.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.domain.code import ParsedFile
from patchfrog.indexing.incremental import compute_change_set
from patchfrog.indexing.inventory import build_inventory
from patchfrog.indexing.models import FileInventoryEntry, IndexingSummary
from patchfrog.indexing.parse_cache import deserialize_parsed_file, serialize_parsed_file
from patchfrog.intelligence.graph import RepositoryEdge, build_graph
from patchfrog.intelligence.resolution import (
    RepositoryResolver,
    ResolutionStatus,
    ResolvedCall,
    ResolvedImport,
)
from patchfrog.intelligence.tests import infer_test_relationships
from patchfrog.parsing.registry import ParserRegistry, default_registry
from patchfrog.persistence.models.code_index import (
    CallReferenceModel,
    FileIndexStatus,
    ImportReferenceModel,
    IndexedFileModel,
    RepositoryEdgeModel,
    SymbolModel,
)
from patchfrog.persistence.repositories import (
    CallReferenceRepository,
    ImportReferenceRepository,
    IndexedFileRepository,
    ParsedFileCacheRepository,
    RepositoryEdgeRepository,
    RepositoryIndexRepository,
    SymbolRepository,
)
from patchfrog.repository.snapshot import RepositorySnapshot, RepositorySnapshotProvider

logger = structlog.get_logger(__name__)


class RepositoryIndexingService:
    """Orchestrates one full or incremental repository indexing run."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        snapshot_provider: RepositorySnapshotProvider | None = None,
        parser_registry: ParserRegistry | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._snapshot_provider = snapshot_provider or RepositorySnapshotProvider()
        self._registry = parser_registry or default_registry()
        self._index_repo = RepositoryIndexRepository()
        self._file_repo = IndexedFileRepository()
        self._symbol_repo = SymbolRepository()
        self._import_repo = ImportReferenceRepository()
        self._call_repo = CallReferenceRepository()
        self._edge_repo = RepositoryEdgeRepository()
        self._cache_repo = ParsedFileCacheRepository()

    async def index_repository(
        self,
        *,
        repository_id: uuid.UUID,
        clone_url: str,
        commit_sha: str,
        repository_full_name: str,
        token: str | None = None,
    ) -> IndexingSummary:
        """Index a repository fetched from ``clone_url`` at ``commit_sha``."""

        start = time.monotonic()
        index_id, old_commit_sha = await self._start_run(
            repository_id=repository_id, commit_sha=commit_sha
        )
        log = logger.bind(
            repository=repository_full_name, commit_sha=commit_sha, index_id=str(index_id)
        )

        try:
            with self._snapshot_provider.acquire(
                clone_url=clone_url,
                commit_sha=commit_sha,
                repository_full_name=repository_full_name,
                token=token,
                also_fetch=[old_commit_sha] if old_commit_sha else None,
            ) as snapshot:
                return await self._run_pipeline(
                    snapshot=snapshot, index_id=index_id, old_commit_sha=old_commit_sha,
                    start=start, log=log,
                )
        except Exception as exc:
            await self._fail_run(index_id=index_id, error_message=str(exc))
            log.error("repository_indexing_failed", error=str(exc))
            raise

    async def index_local_repository(
        self, *, repository_id: uuid.UUID, root_path: Path, repository_full_name: str
    ) -> IndexingSummary:
        """Index a repository already checked out on disk (developer/CLI use)."""

        start = time.monotonic()
        commit_sha_probe = self._snapshot_provider.acquire_local(
            root_path=root_path, repository_full_name=repository_full_name
        )
        index_id, old_commit_sha = await self._start_run(
            repository_id=repository_id, commit_sha=commit_sha_probe.commit_sha
        )
        log = logger.bind(
            repository=repository_full_name,
            commit_sha=commit_sha_probe.commit_sha,
            index_id=str(index_id),
        )

        try:
            return await self._run_pipeline(
                snapshot=commit_sha_probe, index_id=index_id, old_commit_sha=old_commit_sha,
                start=start, log=log,
            )
        except Exception as exc:
            await self._fail_run(index_id=index_id, error_message=str(exc))
            log.error("repository_indexing_failed", error=str(exc))
            raise

    async def _start_run(
        self, *, repository_id: uuid.UUID, commit_sha: str
    ) -> tuple[uuid.UUID, str | None]:
        async with self._session_factory() as session:
            previous_active = await self._index_repo.get_active(session, repository_id=repository_id)
            old_commit_sha = previous_active.commit_sha if previous_active is not None else None
            index_row = await self._index_repo.create_running(
                session, repository_id=repository_id, commit_sha=commit_sha
            )
            await session.commit()
            return index_row.id, old_commit_sha

    async def _fail_run(self, *, index_id: uuid.UUID, error_message: str) -> None:
        async with self._session_factory() as session:
            await self._index_repo.mark_failed(session, index_id=index_id, error_message=error_message)
            await session.commit()

    async def _run_pipeline(
        self,
        *,
        snapshot: RepositorySnapshot,
        index_id: uuid.UUID,
        old_commit_sha: str | None,
        start: float,
        log: structlog.stdlib.BoundLogger,
    ) -> IndexingSummary:
        inventory = build_inventory(snapshot)
        change_set = compute_change_set(snapshot, old_commit_sha=old_commit_sha)

        async with self._session_factory() as session:
            parsed_by_path, statuses, parse_counts = await self._parse_all(
                session=session, inventory=inventory, snapshot=snapshot, log=log
            )

            file_id_by_path = await self._persist_files(
                session=session, index_id=index_id, inventory=inventory, statuses=statuses
            )

            parsed_files = list(parsed_by_path.values())
            resolver = RepositoryResolver(parsed_files)
            resolved_imports = resolver.resolve_imports()
            resolved_calls = resolver.resolve_calls()
            test_relationships = infer_test_relationships(inventory, resolved_imports)
            edges = build_graph(
                parsed_files=parsed_files,
                resolved_imports=resolved_imports,
                resolved_calls=resolved_calls,
                test_relationships=test_relationships,
            )

            symbol_id_by_qualified = await self._persist_symbols(
                session=session, index_id=index_id, parsed_files=parsed_files, file_id_by_path=file_id_by_path
            )
            await self._persist_imports(
                session=session, index_id=index_id, resolved_imports=resolved_imports,
                file_id_by_path=file_id_by_path,
            )
            await self._persist_calls(
                session=session, index_id=index_id, resolved_calls=resolved_calls,
                file_id_by_path=file_id_by_path, symbol_id_by_qualified=symbol_id_by_qualified,
            )
            await self._persist_edges(
                session=session, index_id=index_id, edges=edges,
                file_id_by_path=file_id_by_path, symbol_id_by_qualified=symbol_id_by_qualified,
            )

            symbols_extracted = sum(len(pf.symbols) for pf in parsed_files)
            duration_ms = (time.monotonic() - start) * 1000
            await self._index_repo.mark_succeeded(
                session,
                index_id=index_id,
                files_total=len(inventory),
                files_parsed=parse_counts.files_parsed,
                files_failed=parse_counts.files_failed,
                files_reused=parse_counts.files_reused,
                symbols_extracted=symbols_extracted,
                edges_created=len(edges),
                duration_ms=duration_ms,
            )
            await session.commit()

        log.info(
            "repository_indexed",
            files_total=len(inventory),
            files_parsed=parse_counts.files_parsed,
            files_failed=parse_counts.files_failed,
            files_reused=parse_counts.files_reused,
            symbols_extracted=symbols_extracted,
            edges_created=len(edges),
            duration_ms=duration_ms,
            incremental=old_commit_sha is not None,
            changed_paths=len(change_set.changes),
        )

        return IndexingSummary(
            files_total=len(inventory),
            files_parsed=parse_counts.files_parsed,
            files_failed=parse_counts.files_failed,
            files_reused=parse_counts.files_reused,
            symbols_extracted=symbols_extracted,
            edges_created=len(edges),
            duration_ms=duration_ms,
            incremental=old_commit_sha is not None,
        )

    async def _parse_all(
        self,
        *,
        session: AsyncSession,
        inventory: list[FileInventoryEntry],
        snapshot: RepositorySnapshot,
        log: structlog.stdlib.BoundLogger,
    ) -> tuple[dict[str, ParsedFile], dict[str, tuple[FileIndexStatus, str | None]], _ParseCounts]:
        parsed_by_path: dict[str, ParsedFile] = {}
        statuses: dict[str, tuple[FileIndexStatus, str | None]] = {}
        counts = _ParseCounts()

        for entry in inventory:
            if entry.language is None:
                statuses[entry.relative_path] = (FileIndexStatus.SKIPPED, None)
                continue
            parser = self._registry.get(entry.language)
            if parser is None:
                statuses[entry.relative_path] = (FileIndexStatus.SKIPPED, None)
                continue

            cached_payload = await self._cache_repo.get(
                session, content_hash=entry.content_hash, language=entry.language
            )
            if cached_payload is not None:
                parsed_by_path[entry.relative_path] = deserialize_parsed_file(
                    relative_path=entry.relative_path,
                    language=entry.language,
                    payload=cached_payload,
                )
                statuses[entry.relative_path] = (FileIndexStatus.PARSED, None)
                counts.files_reused += 1
                continue

            try:
                content = snapshot.resolve_path(entry.relative_path).read_bytes()
                parsed = parser.parse_file(relative_path=entry.relative_path, content=content)
            except Exception as exc:
                log.warning("file_parse_failed", path=entry.relative_path, error=str(exc))
                statuses[entry.relative_path] = (FileIndexStatus.FAILED, str(exc))
                counts.files_failed += 1
                continue

            await self._cache_repo.put(
                session,
                content_hash=entry.content_hash,
                language=entry.language,
                payload=serialize_parsed_file(parsed),
            )
            parsed_by_path[entry.relative_path] = parsed
            statuses[entry.relative_path] = (FileIndexStatus.PARSED, None)
            counts.files_parsed += 1

        return parsed_by_path, statuses, counts

    async def _persist_files(
        self,
        *,
        session: AsyncSession,
        index_id: uuid.UUID,
        inventory: list[FileInventoryEntry],
        statuses: dict[str, tuple[FileIndexStatus, str | None]],
    ) -> dict[str, uuid.UUID]:
        models = []
        for entry in inventory:
            status, error_message = statuses[entry.relative_path]
            models.append(
                IndexedFileModel(
                    repository_index_id=index_id,
                    relative_path=entry.relative_path,
                    language=entry.language,
                    size_bytes=entry.size_bytes,
                    content_hash=entry.content_hash,
                    git_blob_sha=entry.git_blob_sha,
                    is_test=entry.is_test,
                    is_generated=entry.is_generated,
                    status=status,
                    error_message=error_message,
                )
            )
        await self._file_repo.bulk_create(session, models)
        return {m.relative_path: m.id for m in models}

    async def _persist_symbols(
        self,
        *,
        session: AsyncSession,
        index_id: uuid.UUID,
        parsed_files: list[ParsedFile],
        file_id_by_path: dict[str, uuid.UUID],
    ) -> dict[tuple[str, str], uuid.UUID]:
        pairs: list[tuple[SymbolModel, str, str | None]] = []
        for pf in parsed_files:
            file_id = file_id_by_path[pf.path]
            for sym in pf.symbols:
                model = SymbolModel(
                    repository_index_id=index_id,
                    indexed_file_id=file_id,
                    name=sym.name,
                    qualified_name=sym.qualified_name,
                    kind=sym.kind,
                    language=pf.language,
                    start_line=sym.span.start_line,
                    end_line=sym.span.end_line,
                    start_column=sym.span.start_column,
                    end_column=sym.span.end_column,
                    signature=sym.signature,
                    visibility=sym.visibility,
                    content_hash=sym.content_hash,
                )
                pairs.append((model, pf.path, sym.parent_qualified_name))

        await self._symbol_repo.bulk_create(session, [model for model, _, _ in pairs])

        symbol_id_by_qualified = {
            (path, model.qualified_name): model.id for model, path, _ in pairs
        }
        for model, path, parent_qualified_name in pairs:
            if parent_qualified_name is not None:
                model.parent_symbol_id = symbol_id_by_qualified.get((path, parent_qualified_name))
        await session.flush()

        return symbol_id_by_qualified

    async def _persist_imports(
        self,
        *,
        session: AsyncSession,
        index_id: uuid.UUID,
        resolved_imports: list[ResolvedImport],
        file_id_by_path: dict[str, uuid.UUID],
    ) -> None:
        models = [
            ImportReferenceModel(
                repository_index_id=index_id,
                indexed_file_id=file_id_by_path[ri.file_path],
                resolved_file_id=(
                    file_id_by_path.get(ri.resolved_file_path) if ri.resolved_file_path else None
                ),
                raw_text=ri.import_.raw_text,
                target=ri.import_.target,
                kind=ri.import_.kind,
                line=ri.import_.line,
            )
            for ri in resolved_imports
        ]
        await self._import_repo.bulk_create(session, models)

    async def _persist_calls(
        self,
        *,
        session: AsyncSession,
        index_id: uuid.UUID,
        resolved_calls: list[ResolvedCall],
        file_id_by_path: dict[str, uuid.UUID],
        symbol_id_by_qualified: dict[tuple[str, str], uuid.UUID],
    ) -> None:
        models = []
        for rc in resolved_calls:
            caller_symbol_id = None
            if rc.call.caller_qualified_name is not None:
                caller_symbol_id = symbol_id_by_qualified.get((rc.file_path, rc.call.caller_qualified_name))

            resolved_symbol_id = None
            if rc.status is ResolutionStatus.RESOLVED and rc.resolved is not None:
                resolved_symbol_id = symbol_id_by_qualified.get(
                    (rc.resolved.file_path, rc.resolved.qualified_name)
                )

            models.append(
                CallReferenceModel(
                    repository_index_id=index_id,
                    indexed_file_id=file_id_by_path[rc.file_path],
                    caller_symbol_id=caller_symbol_id,
                    resolved_symbol_id=resolved_symbol_id,
                    callee_name=rc.call.callee_name,
                    line=rc.call.line,
                    column=rc.call.column,
                    resolution_status=rc.status,
                )
            )
        await self._call_repo.bulk_create(session, models)

    async def _persist_edges(
        self,
        *,
        session: AsyncSession,
        index_id: uuid.UUID,
        edges: list[RepositoryEdge],
        file_id_by_path: dict[str, uuid.UUID],
        symbol_id_by_qualified: dict[tuple[str, str], uuid.UUID],
    ) -> None:
        models = [
            RepositoryEdgeModel(
                repository_index_id=index_id,
                kind=edge.kind,
                source_file_id=file_id_by_path[edge.source.file_path],
                source_symbol_id=(
                    symbol_id_by_qualified.get((edge.source.file_path, edge.source.qualified_name))
                    if edge.source.qualified_name
                    else None
                ),
                target_file_id=file_id_by_path[edge.target.file_path],
                target_symbol_id=(
                    symbol_id_by_qualified.get((edge.target.file_path, edge.target.qualified_name))
                    if edge.target.qualified_name
                    else None
                ),
                reason=edge.reason,
            )
            for edge in edges
        ]
        await self._edge_repo.bulk_create(session, models)


class _ParseCounts:
    __slots__ = ("files_failed", "files_parsed", "files_reused")

    def __init__(self) -> None:
        self.files_parsed = 0
        self.files_reused = 0
        self.files_failed = 0
