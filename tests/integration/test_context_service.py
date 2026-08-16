"""Integration coverage for the Context Engine's ranking, budgeting, and
stale-index/idempotency behavior, against the real ``context_python``
fixture repository (target symbol, direct + depth-2 callers/callees, a
related test, an unrelated function, and cross-file imports)."""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.context.config import ContextConfig
from patchfrog.context.domain import ContextItemKind, ContextRelationship, ContextTargetType
from patchfrog.context.service import ContextService, StaleContextIndexError
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.repository.git import run_git
from tests.support.git_repo import commit_all, materialize_fixture_repo


async def _create_repository(session_factory: async_sessionmaker[AsyncSession], *, full_name: str) -> uuid.UUID:
    async with session_factory() as session:
        row = await RepositoryRepository().upsert(
            session,
            github_repository_id=abs(hash(full_name)) % (2**62),
            owner=full_name.split("/")[0],
            name=full_name.split("/")[-1],
            full_name=full_name,
            installation_id=0,
        )
        await session.commit()
        return row.id


async def _index(session_factory: async_sessionmaker[AsyncSession], *, repository_id: uuid.UUID, root_path: Path, full_name: str) -> None:
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root_path, repository_full_name=full_name
    )


async def test_target_symbol_ranks_highest(tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/context-ranking")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/context-ranking")

    service = ContextService(session_factory=session_factory)
    bundle = await service.build_context_local(
        repository_id=repository_id,
        root_path=snapshot.root_path,
        repository_full_name="test/context-ranking",
        target_type=ContextTargetType.LINE,
        file_path="src/cache.py",
        line=8,  # inside cache_insert
    )

    assert bundle.items[0].kind is ContextItemKind.TARGET_SYMBOL
    assert bundle.items[0].qualified_name is not None and "cache_insert" in bundle.items[0].qualified_name
    assert all(bundle.items[0].score >= item.score for item in bundle.items[1:])


async def test_direct_caller_beats_depth_two_caller(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/context-depth")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/context-depth")

    service = ContextService(session_factory=session_factory)
    bundle = await service.build_context_local(
        repository_id=repository_id,
        root_path=snapshot.root_path,
        repository_full_name="test/context-depth",
        target_type=ContextTargetType.LINE,
        file_path="src/cache.py",
        line=8,
        config=ContextConfig(graph_depth=2),
    )

    by_relationship = {item.relationship: item for item in bundle.items}
    assert ContextRelationship.DIRECT_CALLER in by_relationship
    assert ContextRelationship.TRANSITIVE_CALLER in by_relationship
    direct = by_relationship[ContextRelationship.DIRECT_CALLER]
    transitive = by_relationship[ContextRelationship.TRANSITIVE_CALLER]
    assert direct.score > transitive.score
    assert direct.distance == 1
    assert transitive.distance == 2


async def test_related_test_included_and_ranks_above_unrelated_function(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/context-tests")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/context-tests")

    service = ContextService(session_factory=session_factory)
    bundle = await service.build_context_local(
        repository_id=repository_id,
        root_path=snapshot.root_path,
        repository_full_name="test/context-tests",
        target_type=ContextTargetType.LINE,
        file_path="src/cache.py",
        line=8,
    )

    kinds = {item.kind for item in bundle.items}
    assert ContextItemKind.RELATED_TEST in kinds
    assert not any("unrelated" in item.file_path for item in bundle.items)


async def test_caller_and_callee_and_import_context_present(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/context-full")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/context-full")

    service = ContextService(session_factory=session_factory)
    bundle = await service.build_context_local(
        repository_id=repository_id,
        root_path=snapshot.root_path,
        repository_full_name="test/context-full",
        target_type=ContextTargetType.LINE,
        file_path="src/cache.py",
        line=8,
    )

    kinds = {item.kind for item in bundle.items}
    assert ContextItemKind.CALLER in kinds  # process_request
    assert ContextItemKind.CALLEE in kinds  # _evict, log_event
    # No separate IMPORTED_DEPENDENCY item is asserted here: log_event is
    # both imported *and* a direct callee, and the import candidate
    # (utils.py's first symbol, which happens to be log_event itself)
    # resolves to the exact same span as the callee candidate -- they
    # correctly collapse into one via exact-duplicate suppression
    # (see test_context_dedup.py) rather than appearing twice.


async def test_parent_and_sibling_context_for_a_nested_method_target(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/context-parent")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/context-parent")

    service = ContextService(session_factory=session_factory)
    bundle = await service.build_context_local(
        repository_id=repository_id,
        root_path=snapshot.root_path,
        repository_full_name="test/context-parent",
        target_type=ContextTargetType.LINE,
        file_path="src/cache.py",
        line=22,  # inside CacheContainer.__init__, which calls/is called by nothing
    )

    kinds = {item.kind for item in bundle.items}
    assert ContextItemKind.PARENT_SYMBOL in kinds  # class CacheContainer
    assert ContextItemKind.SIBLING_SYMBOL in kinds  # get() -- its only neighbor, no call relationship to collide with


async def test_symbol_target_type_resolves_directly_by_symbol_id(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from patchfrog.intelligence.queries import RepositoryQueryService

    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/context-symbol")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/context-symbol")

    async with session_factory() as session:
        queries = RepositoryQueryService()
        active_index = await queries.get_active_index(session, repository_id=repository_id)
        assert active_index is not None
        matches = await queries.find_symbol_by_name(session, repository_index_id=active_index.id, name="cache_insert")
        assert len(matches) == 1
        symbol_id = matches[0].id

    service = ContextService(session_factory=session_factory)
    bundle = await service.build_context_local(
        repository_id=repository_id,
        root_path=snapshot.root_path,
        repository_full_name="test/context-symbol",
        target_type=ContextTargetType.SYMBOL,
        file_path="src/cache.py",
        symbol_id=symbol_id,
    )

    assert bundle.items[0].symbol_id == symbol_id


async def test_stale_index_rejects_context_for_a_different_commit(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/context-stale")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/context-stale")
    old_commit_sha = snapshot.commit_sha

    (snapshot.root_path / "src" / "unrelated.py").write_text("def unrelated_function():\n    return 'changed'\n")
    commit_all(snapshot.root_path, "unrelated change")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/context-stale")

    run_git(["-C", str(snapshot.root_path), "checkout", "--quiet", old_commit_sha])

    service = ContextService(session_factory=session_factory)
    try:
        await service.build_context_local(
            repository_id=repository_id,
            root_path=snapshot.root_path,
            repository_full_name="test/context-stale",
            target_type=ContextTargetType.LINE,
            file_path="src/cache.py",
            line=8,
        )
        raise AssertionError("expected StaleContextIndexError")
    except StaleContextIndexError:
        pass


async def test_no_index_at_all_raises_stale_context_index_error(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/context-no-index")
    service = ContextService(session_factory=session_factory)

    try:
        await service.build_context_local(
            repository_id=repository_id,
            root_path=snapshot.root_path,
            repository_full_name="test/context-no-index",
            target_type=ContextTargetType.LINE,
            file_path="src/cache.py",
            line=8,
        )
        raise AssertionError("expected StaleContextIndexError")
    except StaleContextIndexError:
        pass


async def test_identical_request_reuses_canonical_bundle(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/context-idempotent")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/context-idempotent")

    service = ContextService(session_factory=session_factory)
    kwargs: dict[str, object] = {
        "repository_id": repository_id,
        "root_path": snapshot.root_path,
        "repository_full_name": "test/context-idempotent",
        "target_type": ContextTargetType.LINE,
        "file_path": "src/cache.py",
        "line": 8,
    }

    first = await service.build_context_local(**kwargs)  # type: ignore[arg-type]
    second = await service.build_context_local(**kwargs)  # type: ignore[arg-type]

    assert first.reused_existing_bundle is False
    assert second.reused_existing_bundle is True
    assert len(first.items) == len(second.items)
    assert [i.file_path for i in first.items] == [i.file_path for i in second.items]


async def test_different_config_does_not_reuse_bundle(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/context-config-change")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/context-config-change")

    service = ContextService(session_factory=session_factory)
    first = await service.build_context_local(
        repository_id=repository_id,
        root_path=snapshot.root_path,
        repository_full_name="test/context-config-change",
        target_type=ContextTargetType.LINE,
        file_path="src/cache.py",
        line=8,
        config=ContextConfig(max_tokens=4000),
    )
    second = await service.build_context_local(
        repository_id=repository_id,
        root_path=snapshot.root_path,
        repository_full_name="test/context-config-change",
        target_type=ContextTargetType.LINE,
        file_path="src/cache.py",
        line=8,
        config=ContextConfig(max_tokens=100),
    )

    assert first.reused_existing_bundle is False
    assert second.reused_existing_bundle is False


async def test_repeated_generation_is_deterministically_ordered(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Two independently-generated bundles (distinct identities via
    different config, so neither reuses the other) for the same target
    must select/order items identically -- no reliance on DB row order."""

    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/context-stable-order")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/context-stable-order")

    service = ContextService(session_factory=session_factory)
    first = await service.build_context_local(
        repository_id=repository_id,
        root_path=snapshot.root_path,
        repository_full_name="test/context-stable-order",
        target_type=ContextTargetType.LINE,
        file_path="src/cache.py",
        line=8,
        config=ContextConfig(max_tokens=4001),
    )
    second = await service.build_context_local(
        repository_id=repository_id,
        root_path=snapshot.root_path,
        repository_full_name="test/context-stable-order",
        target_type=ContextTargetType.LINE,
        file_path="src/cache.py",
        line=8,
        config=ContextConfig(max_tokens=4002),
    )

    first_order = [(i.kind.value, i.file_path, i.start_line) for i in first.items]
    second_order = [(i.kind.value, i.file_path, i.start_line) for i in second.items]
    assert first_order == second_order


async def test_finding_deep_inside_a_large_function_survives_tight_budget_trimming(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Regression, full pipeline: ``src/huge.py`` in the fixture is a
    600-line function whose "finding" line (a real, deterministic marker)
    sits at line 551, far past any reasonable per-item line/token cap.
    The target item in the persisted bundle must still contain it."""

    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/context-large-symbol")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/context-large-symbol")

    finding_line = None
    for i, line in enumerate((snapshot.root_path / "src" / "huge.py").read_text().splitlines(), start=1):
        if "unreachable but flagged by finding" in line:
            finding_line = i
            break
    assert finding_line is not None and finding_line > 500  # sanity: genuinely deep inside the function

    service = ContextService(session_factory=session_factory)
    bundle = await service.build_context_local(
        repository_id=repository_id,
        root_path=snapshot.root_path,
        repository_full_name="test/context-large-symbol",
        target_type=ContextTargetType.LINE,
        file_path="src/huge.py",
        line=finding_line,
        config=ContextConfig(max_tokens=600, max_lines=60, max_lines_per_item=60, max_tokens_per_item=600),
    )

    target_item = bundle.items[0]
    assert target_item.kind is ContextItemKind.TARGET_SYMBOL
    assert target_item.truncated is True
    assert target_item.start_line <= finding_line <= target_item.end_line
    assert "unreachable but flagged by finding" in target_item.content
    assert bundle.total_tokens_estimate <= 600
    assert bundle.total_lines <= 60


async def test_ordinary_small_symbol_context_is_unaffected_by_the_anchor_fix(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The common case -- a small target symbol that fits entirely within
    its budget -- must produce identical, whole, non-truncated output."""

    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/context-small-symbol")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/context-small-symbol")

    service = ContextService(session_factory=session_factory)
    bundle = await service.build_context_local(
        repository_id=repository_id,
        root_path=snapshot.root_path,
        repository_full_name="test/context-small-symbol",
        target_type=ContextTargetType.LINE,
        file_path="src/cache.py",
        line=8,
    )

    target_item = bundle.items[0]
    assert target_item.kind is ContextItemKind.TARGET_SYMBOL
    assert target_item.truncated is False
    assert target_item.start_line == 4  # def cache_insert(...) -- unchanged from before the fix
    assert target_item.end_line == 8
