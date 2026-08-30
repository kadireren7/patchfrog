"""Integration coverage for adaptive multi-hop context generation
(:mod:`patchfrog.context.adaptive`, :class:`ContextService`'s adaptive
branch), against the real ``context_python`` fixture repository's
``network.py`` (proven 2-hop regression case), ``cycles.py`` (cycle/
self-call safety), ``both_directions.py`` (bidirectional expansion), and
``hub.py`` (high-connectivity bounding)."""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.context.config import AdaptiveContextConfig, ContextConfig
from patchfrog.context.domain import ContextBundle, ContextRelationship, ContextTargetType
from patchfrog.context.service import ContextService
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.intelligence.queries import RepositoryQueryService
from patchfrog.persistence.repositories import RepositoryRepository
from tests.support.git_repo import materialize_fixture_repo


async def _create_repository(session_factory: async_sessionmaker[AsyncSession], *, full_name: str) -> uuid.UUID:
    async with session_factory() as session:
        row = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner=full_name.split("/")[0], name=full_name.split("/")[-1],
            full_name=full_name, installation_id=0,
        )
        await session.commit()
        return row.id


async def _index(session_factory: async_sessionmaker[AsyncSession], *, repository_id: uuid.UUID, root_path: Path, full_name: str) -> None:
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root_path, repository_full_name=full_name
    )


async def _symbol_id(
    session_factory: async_sessionmaker[AsyncSession], *, repository_id: uuid.UUID, name: str
) -> uuid.UUID:
    async with session_factory() as session:
        queries = RepositoryQueryService()
        active_index = await queries.get_active_index(session, repository_id=repository_id)
        assert active_index is not None
        matches = await queries.find_symbol_by_name(session, repository_index_id=active_index.id, name=name)
        assert len(matches) == 1, f"expected exactly one symbol named {name!r}, found {len(matches)}"
        return matches[0].id


async def _build(
    service: ContextService, *, repository_id: uuid.UUID, root_path: Path, full_name: str, symbol_id: uuid.UUID,
    file_path: str, config: ContextConfig,
) -> ContextBundle:
    return await service.build_context_local(
        repository_id=repository_id, root_path=root_path, repository_full_name=full_name,
        target_type=ContextTargetType.SYMBOL, file_path=file_path, symbol_id=symbol_id, config=config,
    )


# -- Proven 2-hop regression case (spec section 4 / required scenarios 30, 31) --


async def test_fixed_depth_1_excludes_the_deeper_dependency(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/adaptive-proven-fixed1")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/adaptive-proven-fixed1")
    target_id = await _symbol_id(session_factory, repository_id=repository_id, name="connect_on_startup")

    service = ContextService(session_factory=session_factory)
    bundle = await _build(
        service, repository_id=repository_id, root_path=snapshot.root_path,
        full_name="test/adaptive-proven-fixed1", symbol_id=target_id, file_path="src/network.py", config=ContextConfig(),
    )

    names = {item.qualified_name for item in bundle.items}
    assert any("reconnect_with_backoff" in (n or "") for n in names)
    assert not any("compute_backoff_ms" in (n or "") for n in names)


async def test_adaptive_mode_includes_the_deeper_dependency_when_justified(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Adaptive expansion must include the second-hop symbol the old
    1-hop context missed. This proves the *context* is present -- it
    does NOT claim the underlying LLM review-quality gap is solved by
    that alone (see docs/agent-orchestration.md / docs/context-engine.md)."""

    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/adaptive-proven-adaptive")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/adaptive-proven-adaptive")
    target_id = await _symbol_id(session_factory, repository_id=repository_id, name="connect_on_startup")

    service = ContextService(session_factory=session_factory)
    bundle = await _build(
        service, repository_id=repository_id, root_path=snapshot.root_path,
        full_name="test/adaptive-proven-adaptive", symbol_id=target_id,
        file_path="src/network.py",
        config=ContextConfig(adaptive=AdaptiveContextConfig(enabled=True)),
    )

    names = {item.qualified_name for item in bundle.items}
    assert any("compute_backoff_ms" in (n or "") for n in names)
    assert bundle.adaptive_metrics is not None
    assert bundle.adaptive_metrics.attempted is True
    assert bundle.adaptive_metrics.occurred is True
    assert bundle.adaptive_metrics.direction == "callees"
    # Known limitation (spec section 14): the RETRY_POLICY_MAX_ATTEMPTS
    # module-level constant compute_backoff_ms depends on is NOT
    # resolved as context -- current repository intelligence has no
    # relationship kind for "depends on this constant." Documented, not
    # faked.
    assert not any("RETRY_POLICY_MAX_ATTEMPTS" in (n or "") for n in names)
    # Budget respected (section 8/24 scenarios 16, 17).
    assert bundle.total_tokens_estimate <= 4000
    assert bundle.total_lines <= 400


async def test_adaptive_bundle_respects_token_and_line_budget(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Required scenarios 16, 17: adaptive expansion must never push the
    bundle past the configured ceiling, even with a tight budget."""

    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/adaptive-budget")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/adaptive-budget")
    target_id = await _symbol_id(session_factory, repository_id=repository_id, name="connect_on_startup")

    service = ContextService(session_factory=session_factory)
    bundle = await _build(
        service, repository_id=repository_id, root_path=snapshot.root_path,
        full_name="test/adaptive-budget", symbol_id=target_id,
        file_path="src/network.py",
        config=ContextConfig(
            max_tokens=300, max_lines=30, adaptive=AdaptiveContextConfig(enabled=True, expansion_token_fraction=0.3, expansion_line_fraction=0.3),
        ),
    )

    assert bundle.total_tokens_estimate <= 300
    assert bundle.total_lines <= 30


# -- No trigger -> no expansion (required scenario 4) --


async def test_no_trigger_means_no_expansion(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/adaptive-no-trigger")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/adaptive-no-trigger")
    # unrelated_helper is never called by anything and calls nothing --
    # a genuinely isolated symbol, no depth-1 or depth-2 neighbors at all.
    target_id = await _symbol_id(session_factory, repository_id=repository_id, name="unrelated_helper")

    service = ContextService(session_factory=session_factory)
    bundle = await _build(
        service, repository_id=repository_id, root_path=snapshot.root_path,
        full_name="test/adaptive-no-trigger", symbol_id=target_id,
        file_path="src/utils.py",
        config=ContextConfig(adaptive=AdaptiveContextConfig(enabled=True)),
    )

    assert bundle.adaptive_metrics is not None
    assert bundle.adaptive_metrics.attempted is True
    assert bundle.adaptive_metrics.occurred is False
    assert not any(item.distance >= 2 for item in bundle.items)


# -- Directional expansion (required scenarios 6, 7, 8) --


async def test_caller_only_expansion(tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]) -> None:
    """cache_insert has a depth-2 *caller* chain (api_route ->
    process_request -> cache_insert) but no depth-2 callee chain
    (_evict/log_event call nothing further)."""

    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/adaptive-caller-only")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/adaptive-caller-only")
    target_id = await _symbol_id(session_factory, repository_id=repository_id, name="cache_insert")

    service = ContextService(session_factory=session_factory)
    bundle = await _build(
        service, repository_id=repository_id, root_path=snapshot.root_path,
        full_name="test/adaptive-caller-only", symbol_id=target_id,
        file_path="src/cache.py",
        config=ContextConfig(adaptive=AdaptiveContextConfig(enabled=True)),
    )

    assert bundle.adaptive_metrics is not None and bundle.adaptive_metrics.direction == "callers"
    assert any(item.relationship is ContextRelationship.TRANSITIVE_CALLER for item in bundle.items)
    assert not any(item.relationship is ContextRelationship.TRANSITIVE_CALLEE for item in bundle.items)


async def test_callee_only_expansion(tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/adaptive-callee-only")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/adaptive-callee-only")
    target_id = await _symbol_id(session_factory, repository_id=repository_id, name="connect_on_startup")

    service = ContextService(session_factory=session_factory)
    bundle = await _build(
        service, repository_id=repository_id, root_path=snapshot.root_path,
        full_name="test/adaptive-callee-only", symbol_id=target_id,
        file_path="src/network.py",
        config=ContextConfig(adaptive=AdaptiveContextConfig(enabled=True)),
    )

    assert bundle.adaptive_metrics is not None and bundle.adaptive_metrics.direction == "callees"
    assert any(item.relationship is ContextRelationship.TRANSITIVE_CALLEE for item in bundle.items)
    assert not any(item.relationship is ContextRelationship.TRANSITIVE_CALLER for item in bundle.items)


async def test_bounded_both_direction_expansion(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/adaptive-both")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/adaptive-both")
    target_id = await _symbol_id(session_factory, repository_id=repository_id, name="both_target")

    service = ContextService(session_factory=session_factory)
    bundle = await _build(
        service, repository_id=repository_id, root_path=snapshot.root_path,
        full_name="test/adaptive-both", symbol_id=target_id,
        file_path="src/both_directions.py",
        config=ContextConfig(adaptive=AdaptiveContextConfig(enabled=True)),
    )

    assert bundle.adaptive_metrics is not None and bundle.adaptive_metrics.direction == "both"
    assert any(item.relationship is ContextRelationship.TRANSITIVE_CALLER for item in bundle.items)
    assert any(item.relationship is ContextRelationship.TRANSITIVE_CALLEE for item in bundle.items)


# -- Cycle/self-call safety (required scenarios 9, 10, 11, 12, 13, 14) --


async def test_depth_2_never_re_adds_the_target(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Required scenarios 9, 11: two-cycle A -> B -> A -- expanding B's
    callees (direction from A's perspective) must never re-add A itself
    as a transitive candidate."""

    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/adaptive-2cycle")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/adaptive-2cycle")
    target_id = await _symbol_id(session_factory, repository_id=repository_id, name="two_cycle_a")

    service = ContextService(session_factory=session_factory)
    bundle = await _build(
        service, repository_id=repository_id, root_path=snapshot.root_path,
        full_name="test/adaptive-2cycle", symbol_id=target_id, file_path="src/cycles.py", config=ContextConfig(graph_depth=2),
    )

    target_names = [item.qualified_name for item in bundle.items if item.distance == 0]
    transitive_names = [item.qualified_name for item in bundle.items if item.distance >= 2]
    assert all("two_cycle_a" not in (n or "") for n in transitive_names)
    assert len(target_names) == 1  # the target itself appears exactly once


async def test_self_recursive_symbol_is_safe(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Required scenario 13: A -> A (self-call) must never re-add the
    target as its own direct or transitive callee."""

    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/adaptive-selfcall")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/adaptive-selfcall")
    target_id = await _symbol_id(session_factory, repository_id=repository_id, name="self_recursive")

    service = ContextService(session_factory=session_factory)
    bundle = await _build(
        service, repository_id=repository_id, root_path=snapshot.root_path,
        full_name="test/adaptive-selfcall", symbol_id=target_id, file_path="src/cycles.py", config=ContextConfig(graph_depth=2),
    )

    non_target_names = [item.qualified_name for item in bundle.items if item.distance > 0]
    assert all("self_recursive" not in (n or "") for n in non_target_names)


async def test_three_cycle_is_finite_and_safe(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Required scenario 12: A -> B -> C -> A. Depth is capped at 2, so
    the third hop (which would rediscover A) is never even attempted --
    bounded, finite, terminates."""

    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/adaptive-3cycle")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/adaptive-3cycle")
    target_id = await _symbol_id(session_factory, repository_id=repository_id, name="three_cycle_a")

    service = ContextService(session_factory=session_factory)
    bundle = await _build(
        service, repository_id=repository_id, root_path=snapshot.root_path,
        full_name="test/adaptive-3cycle", symbol_id=target_id, file_path="src/cycles.py", config=ContextConfig(graph_depth=2),
    )

    # In a 3-cycle (A -> B -> C -> A), C is simultaneously A's *direct*
    # caller (C -> A) and A's callee-direction *transitive* callee
    # (A -> B -> C) -- the same symbol, same span, so exact-duplicate
    # suppression correctly collapses it to the more-direct
    # representation (DIRECT_CALLER) rather than duplicating it. The
    # only invariant that actually matters here: the target itself (A)
    # never reappears as a non-target item, and every reachable symbol
    # (B, C) still appears exactly once -- a finite, terminating result,
    # never an unbounded/duplicated one.
    non_target_names = [item.qualified_name for item in bundle.items if item.distance > 0]
    assert not any("three_cycle_a" in (n or "") for n in non_target_names)
    assert any("three_cycle_b" in (n or "") for n in non_target_names)
    assert any("three_cycle_c" in (n or "") for n in non_target_names)
    assert non_target_names.count(next(n for n in non_target_names if n and "three_cycle_c" in n)) == 1


async def test_diamond_dedup_never_duplicates_the_shared_descendant(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Required scenario 14: A -> B, A -> C, B -> D, C -> D. D must
    appear at most once."""

    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/adaptive-diamond")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/adaptive-diamond")
    target_id = await _symbol_id(session_factory, repository_id=repository_id, name="diamond_top")

    service = ContextService(session_factory=session_factory)
    bundle = await _build(
        service, repository_id=repository_id, root_path=snapshot.root_path,
        full_name="test/adaptive-diamond", symbol_id=target_id, file_path="src/cycles.py", config=ContextConfig(graph_depth=2),
    )

    bottom_items = [item for item in bundle.items if item.qualified_name and "diamond_bottom" in item.qualified_name]
    assert len(bottom_items) == 1


# -- High-connectivity bounding (required scenario 15) --


async def test_high_connectivity_expansion_is_bounded(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/adaptive-hub")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/adaptive-hub")
    target_id = await _symbol_id(session_factory, repository_id=repository_id, name="hub_target")

    service = ContextService(session_factory=session_factory)
    bundle = await _build(
        service, repository_id=repository_id, root_path=snapshot.root_path,
        full_name="test/adaptive-hub", symbol_id=target_id,
        file_path="src/hub.py",
        config=ContextConfig(graph_depth=2, max_expansion_roots=5, max_items_per_relationship=10),
    )

    # hub_target has 8 direct callers but only 3 of them have an upstream
    # caller at all -- bounded to at most 5 expansion roots regardless,
    # so at most 3 transitive callers can ever appear (never all 8).
    transitive = [item for item in bundle.items if item.relationship is ContextRelationship.TRANSITIVE_CALLER]
    assert len(transitive) <= 3
    direct = [item for item in bundle.items if item.relationship is ContextRelationship.DIRECT_CALLER]
    assert len(direct) <= 8


# -- Scoring (required scenarios 20, 21) --


async def test_direct_context_outranks_transitive_in_adaptive_mode(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "context_python")
    repository_id = await _create_repository(session_factory, full_name="test/adaptive-scoring")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/adaptive-scoring")
    target_id = await _symbol_id(session_factory, repository_id=repository_id, name="connect_on_startup")

    service = ContextService(session_factory=session_factory)
    bundle = await _build(
        service, repository_id=repository_id, root_path=snapshot.root_path,
        full_name="test/adaptive-scoring", symbol_id=target_id,
        file_path="src/network.py",
        config=ContextConfig(adaptive=AdaptiveContextConfig(enabled=True)),
    )

    by_relationship = {item.relationship: item for item in bundle.items}
    direct = by_relationship[ContextRelationship.DIRECT_CALLEE]
    transitive = by_relationship[ContextRelationship.TRANSITIVE_CALLEE]
    assert direct.score > transitive.score


# -- Fingerprint/version identity (required scenarios 23, 24) --


async def test_fingerprint_differs_across_fixed1_fixed2_and_adaptive(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    fixed1 = ContextConfig()
    fixed2 = ContextConfig(graph_depth=2)
    adaptive = ContextConfig(adaptive=AdaptiveContextConfig(enabled=True))

    fingerprints = {fixed1.fingerprint(), fixed2.fingerprint(), adaptive.fingerprint()}
    assert len(fingerprints) == 3
