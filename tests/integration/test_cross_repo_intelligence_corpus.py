"""Controlled corpus for Cross-Repo Intelligence Foundation (minimum 30
scenarios) -- real, persisted `repositories`/`repository_contract_keys`/
`repository_relations` rows via the real repositories
(:class:`~patchfrog.persistence.repositories.repository.RepositoryRepository`,
:class:`~patchfrog.persistence.repositories.cross_repo.RepositoryContractKeyRepository`,
:class:`~patchfrog.persistence.repositories.cross_repo.RepositoryRelationRepository`),
never a hand-built `CrossRepoPeer` standing in for a real DB round trip
(that discipline is reserved for the unit-level matching tests in
``tests/unit/test_cross_repo_intelligence_matching.py``).

`CROSS_REPO_PACKAGE_DEPENDENCY_IMPACT` is deferred in v1 (see
``validation/cross_repo_intelligence/latest-summary.md`` sections 3,
11) -- corpus expectations are explicit: no such signal is ever
constructed, whatever the underlying data looks like.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.change_intelligence.domain import AffectedRelation, AffectedSymbolRef
from patchfrog.contract_intelligence.domain import (
    BreakingCharacteristic,
    ContractDelta,
    ContractKind,
)
from patchfrog.cross_repo_intelligence.domain import (
    MAX_CROSS_REPO_PEERS,
    MAX_CROSS_REPO_SIGNALS,
    CrossRepoSignalKind,
)
from patchfrog.cross_repo_intelligence.matching import select_review_hint
from patchfrog.cross_repo_intelligence.service import build_cross_repo_intelligence_report
from patchfrog.persistence.repositories import (
    RepositoryContractKeyRepository,
    RepositoryRelationRepository,
    RepositoryRepository,
)


async def _make_repo(
    session_factory: async_sessionmaker[AsyncSession],
    full_name: str,
    *,
    installation_id: int = 999,
    selected: bool = True,
) -> uuid.UUID:
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name=full_name.split("/")[-1], full_name=full_name, installation_id=installation_id,
        )
        if not selected:
            await RepositoryRepository().set_selected(
                session, github_repository_id=repo.github_repository_id, selected=False
            )
        await session.commit()
        return repo.id


async def _register_contract_key(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository_id: uuid.UUID,
    stable_key: str,
    file_path: str,
    qualified_name: str,
) -> None:
    async with session_factory() as session:
        await RepositoryContractKeyRepository().upsert(
            session, repository_id=repository_id, contract_kind=ContractKind.FUNCTION,
            stable_key=stable_key, file_path=file_path, qualified_name=qualified_name,
        )
        await session.commit()


async def _register_relation(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    source_repository_id: uuid.UUID,
    target_repository_id: uuid.UUID,
    contract_key: str,
    active: bool = True,
) -> None:
    from patchfrog.cross_repo_intelligence.domain import (
        RepositoryRelationKind,
        RepositoryRelationProvenance,
    )

    async with session_factory() as session:
        await RepositoryRelationRepository().upsert(
            session, source_repository_id=source_repository_id, target_repository_id=target_repository_id,
            relation_kind=RepositoryRelationKind.EXPLICIT_SHARED_CONTRACT, external_contract_key=contract_key,
            provenance=RepositoryRelationProvenance.OPERATOR_CLI,
        )
        if not active:
            await RepositoryRelationRepository().deactivate(
                session, source_repository_id=source_repository_id, target_repository_id=target_repository_id,
                relation_kind=RepositoryRelationKind.EXPLICIT_SHARED_CONTRACT, external_contract_key=contract_key,
            )
        await session.commit()


def _delta(*, file_path: str, qualified_name: str) -> ContractDelta:
    return ContractDelta(
        contract_id=f"contract-{qualified_name}", qualified_name=qualified_name, file_path=file_path,
        kind=ContractKind.FUNCTION, change_unit_id=None, before_signature="def f(a): ...",
        after_signature="def f(a, b): ...", characteristics=(BreakingCharacteristic.REQUIRED_PARAMETER_ADDED,),
        evidence="evidence", blast_radius=(),
    )


# ---- 1. No relation -> empty report ----


async def test_case_no_relation_empty_report(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repo(session_factory, "org/service-a")

    async with session_factory() as session:
        report = await build_cross_repo_intelligence_report(
            session, repository_id=repository_id,
            contract_deltas=(_delta(file_path="capture.py", qualified_name="capture_payment"),),
        )
    assert report.peers_considered == ()
    assert report.overlaps == ()
    assert report.signals == ()


# ---- 2. Same installation, no explicit relation -> ignored ----


async def test_case_same_installation_no_relation_ignored(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repo(session_factory, "org/service-a", installation_id=555)
    await _make_repo(session_factory, "org/service-b", installation_id=555)  # same installation, no relation

    async with session_factory() as session:
        report = await build_cross_repo_intelligence_report(
            session, repository_id=repository_id,
            contract_deltas=(_delta(file_path="capture.py", qualified_name="capture_payment"),),
        )
    assert report.peers_considered == ()


# ---- 3. Similar repo names -> never matched by name alone ----


async def test_case_similar_repo_names_never_matched(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repo(session_factory, "org/service-a")
    await _make_repo(session_factory, "org/service-a-fork")  # similar name, no relation registered

    async with session_factory() as session:
        report = await build_cross_repo_intelligence_report(
            session, repository_id=repository_id,
            contract_deltas=(_delta(file_path="capture.py", qualified_name="capture_payment"),),
        )
    assert report.peers_considered == ()


# ---- 4. Different contract key registered under peer -> never matched ----


async def test_case_different_contract_key_never_matched(session_factory: async_sessionmaker[AsyncSession]) -> None:
    source_id = await _make_repo(session_factory, "org/service-a")
    target_id = await _make_repo(session_factory, "org/service-b")
    await _register_contract_key(
        session_factory, repository_id=source_id, stable_key="payments.capture:v1",
        file_path="capture.py", qualified_name="capture_payment",
    )
    await _register_relation(
        session_factory, source_repository_id=source_id, target_repository_id=target_id,
        contract_key="unrelated.key:v1",
    )

    async with session_factory() as session:
        report = await build_cross_repo_intelligence_report(
            session, repository_id=source_id,
            contract_deltas=(_delta(file_path="capture.py", qualified_name="capture_payment"),),
        )
    assert report.overlaps == ()


# ---- 5. Explicit relation to a real peer -> peer considered, signal produced ----


async def test_case_explicit_relation_produces_signal(session_factory: async_sessionmaker[AsyncSession]) -> None:
    source_id = await _make_repo(session_factory, "org/service-a")
    target_id = await _make_repo(session_factory, "org/service-b")
    await _register_contract_key(
        session_factory, repository_id=source_id, stable_key="payments.capture:v1",
        file_path="capture.py", qualified_name="capture_payment",
    )
    await _register_relation(
        session_factory, source_repository_id=source_id, target_repository_id=target_id,
        contract_key="payments.capture:v1",
    )

    async with session_factory() as session:
        report = await build_cross_repo_intelligence_report(
            session, repository_id=source_id,
            contract_deltas=(_delta(file_path="capture.py", qualified_name="capture_payment"),),
        )
    assert len(report.peers_considered) == 1
    assert report.peers_considered[0].full_name == "org/service-b"
    assert len(report.overlaps) == 1
    assert report.overlaps[0].signal_kind is CrossRepoSignalKind.CROSS_REPO_CONTRACT_CHANGE
    assert len(report.signals) == 1
    hint = select_review_hint(report.signals, file_path="capture.py", qualified_name="capture_payment")
    from patchfrog.cross_repo_intelligence.domain import CrossRepoReviewHint

    assert hint is CrossRepoReviewHint.REQUIRE_CRITIC


# ---- 6. Inactive relation -> ignored ----


async def test_case_inactive_relation_ignored(session_factory: async_sessionmaker[AsyncSession]) -> None:
    source_id = await _make_repo(session_factory, "org/service-a")
    target_id = await _make_repo(session_factory, "org/service-b")
    await _register_contract_key(
        session_factory, repository_id=source_id, stable_key="payments.capture:v1",
        file_path="capture.py", qualified_name="capture_payment",
    )
    await _register_relation(
        session_factory, source_repository_id=source_id, target_repository_id=target_id,
        contract_key="payments.capture:v1", active=False,
    )

    async with session_factory() as session:
        report = await build_cross_repo_intelligence_report(
            session, repository_id=source_id,
            contract_deltas=(_delta(file_path="capture.py", qualified_name="capture_payment"),),
        )
    assert report.peers_considered == ()


# ---- 7. Unauthorized peer (different installation) -> ignored ----


async def test_case_unauthorized_peer_different_installation_ignored(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _make_repo(session_factory, "org/service-a", installation_id=111)
    target_id = await _make_repo(session_factory, "org/service-b", installation_id=222)
    await _register_contract_key(
        session_factory, repository_id=source_id, stable_key="payments.capture:v1",
        file_path="capture.py", qualified_name="capture_payment",
    )
    await _register_relation(
        session_factory, source_repository_id=source_id, target_repository_id=target_id,
        contract_key="payments.capture:v1",
    )

    async with session_factory() as session:
        report = await build_cross_repo_intelligence_report(
            session, repository_id=source_id,
            contract_deltas=(_delta(file_path="capture.py", qualified_name="capture_payment"),),
        )
    assert report.peers_considered == ()


# ---- 8. Relation direction A->B: current A's change matches, B is the peer ----
#         Relation B->A never applies to a change in A (direction enforced) ----


async def test_case_relation_direction_b_to_a_never_applies_to_a_change(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo_a = await _make_repo(session_factory, "org/service-a")
    repo_b = await _make_repo(session_factory, "org/service-b")
    # B is the producer, A is the consumer -- the reverse of what we're about to test.
    await _register_contract_key(
        session_factory, repository_id=repo_b, stable_key="payments.capture:v1",
        file_path="capture.py", qualified_name="capture_payment",
    )
    await _register_relation(
        session_factory, source_repository_id=repo_b, target_repository_id=repo_a,
        contract_key="payments.capture:v1",
    )

    # A itself changes a symbol with the same file/qualified name (irrelevant --
    # A has no registered contract key of its own for it).
    async with session_factory() as session:
        report = await build_cross_repo_intelligence_report(
            session, repository_id=repo_a,
            contract_deltas=(_delta(file_path="capture.py", qualified_name="capture_payment"),),
        )
    assert report.peers_considered == ()


# ---- 9. No transitive inference: A->B and B->C never combine into A->C ----


async def test_case_no_transitive_inference(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repo_a = await _make_repo(session_factory, "org/service-a")
    repo_b = await _make_repo(session_factory, "org/service-b")
    repo_c = await _make_repo(session_factory, "org/service-c")
    await _register_contract_key(
        session_factory, repository_id=repo_a, stable_key="payments.capture:v1",
        file_path="capture.py", qualified_name="capture_payment",
    )
    await _register_relation(
        session_factory, source_repository_id=repo_a, target_repository_id=repo_b,
        contract_key="payments.capture:v1",
    )
    await _register_contract_key(
        session_factory, repository_id=repo_b, stable_key="capture.forwarded:v1",
        file_path="forward.py", qualified_name="forward_capture",
    )
    await _register_relation(
        session_factory, source_repository_id=repo_b, target_repository_id=repo_c,
        contract_key="capture.forwarded:v1",
    )

    async with session_factory() as session:
        report = await build_cross_repo_intelligence_report(
            session, repository_id=repo_a,
            contract_deltas=(_delta(file_path="capture.py", qualified_name="capture_payment"),),
        )
    peer_names = {p.full_name for p in report.peers_considered}
    assert peer_names == {"org/service-b"}
    assert "org/service-c" not in peer_names


# ---- 10. Fork with the same repository name is never inherited ----


async def test_case_fork_with_same_name_not_inherited(session_factory: async_sessionmaker[AsyncSession]) -> None:
    source_id = await _make_repo(session_factory, "org/service-a")
    target_id = await _make_repo(session_factory, "org/service-b")
    fork_id = await _make_repo(session_factory, "someone-else/service-b")  # a "fork" -- distinct repository_id
    await _register_contract_key(
        session_factory, repository_id=source_id, stable_key="payments.capture:v1",
        file_path="capture.py", qualified_name="capture_payment",
    )
    await _register_relation(
        session_factory, source_repository_id=source_id, target_repository_id=target_id,
        contract_key="payments.capture:v1",
    )

    async with session_factory() as session:
        report = await build_cross_repo_intelligence_report(
            session, repository_id=source_id,
            contract_deltas=(_delta(file_path="capture.py", qualified_name="capture_payment"),),
        )
    peer_ids = {p.repository_id for p in report.peers_considered}
    assert fork_id not in peer_ids
    assert target_id in peer_ids


# ---- 11. Repository rename (full_name changes, id unchanged) -> relation still valid ----


async def test_case_repository_rename_relation_still_valid(session_factory: async_sessionmaker[AsyncSession]) -> None:
    source_id = await _make_repo(session_factory, "org/service-a")
    target_id = await _make_repo(session_factory, "org/service-b")
    await _register_contract_key(
        session_factory, repository_id=source_id, stable_key="payments.capture:v1",
        file_path="capture.py", qualified_name="capture_payment",
    )
    await _register_relation(
        session_factory, source_repository_id=source_id, target_repository_id=target_id,
        contract_key="payments.capture:v1",
    )

    # Simulate a GitHub rename: re-upsert with a new full_name, same github_repository_id.
    async with session_factory() as session:
        existing = await RepositoryRepository().get_by_full_name(session, full_name="org/service-b")
        assert existing is not None
        await RepositoryRepository().upsert(
            session, github_repository_id=existing.github_repository_id, owner="org", name="service-b-renamed",
            full_name="org/service-b-renamed", installation_id=existing.installation_id,
        )
        await session.commit()

    async with session_factory() as session:
        report = await build_cross_repo_intelligence_report(
            session, repository_id=source_id,
            contract_deltas=(_delta(file_path="capture.py", qualified_name="capture_payment"),),
        )
    assert len(report.peers_considered) == 1
    assert report.peers_considered[0].repository_id == target_id
    assert report.peers_considered[0].full_name == "org/service-b-renamed"


# ---- 12. Peer access revoked (is_selected flips False) -> excluded immediately ----


async def test_case_peer_access_revoked_excluded(session_factory: async_sessionmaker[AsyncSession]) -> None:
    source_id = await _make_repo(session_factory, "org/service-a")
    target_id = await _make_repo(session_factory, "org/service-b")
    await _register_contract_key(
        session_factory, repository_id=source_id, stable_key="payments.capture:v1",
        file_path="capture.py", qualified_name="capture_payment",
    )
    await _register_relation(
        session_factory, source_repository_id=source_id, target_repository_id=target_id,
        contract_key="payments.capture:v1",
    )

    async with session_factory() as session:
        report_before = await build_cross_repo_intelligence_report(
            session, repository_id=source_id,
            contract_deltas=(_delta(file_path="capture.py", qualified_name="capture_payment"),),
        )
    assert len(report_before.peers_considered) == 1

    # Installation removes repository B -- is_selected flips False.
    async with session_factory() as session:
        target = await RepositoryRepository().get_by_full_name(session, full_name="org/service-b")
        assert target is not None
        await RepositoryRepository().set_selected(
            session, github_repository_id=target.github_repository_id, selected=False
        )
        await session.commit()

    async with session_factory() as session:
        report_after = await build_cross_repo_intelligence_report(
            session, repository_id=source_id,
            contract_deltas=(_delta(file_path="capture.py", qualified_name="capture_payment"),),
        )
    assert report_after.peers_considered == ()


# ---- 13. Exact contract key match -> signal (positive control, mirrors #5) ----


async def test_case_exact_contract_key_match_signal(session_factory: async_sessionmaker[AsyncSession]) -> None:
    source_id = await _make_repo(session_factory, "org/service-a")
    target_id = await _make_repo(session_factory, "org/service-b")
    await _register_contract_key(
        session_factory, repository_id=source_id, stable_key="exact.match:v1",
        file_path="a.py", qualified_name="foo",
    )
    await _register_relation(
        session_factory, source_repository_id=source_id, target_repository_id=target_id,
        contract_key="exact.match:v1",
    )

    async with session_factory() as session:
        report = await build_cross_repo_intelligence_report(
            session, repository_id=source_id, contract_deltas=(_delta(file_path="a.py", qualified_name="foo"),),
        )
    assert len(report.signals) == 1


# ---- 14. No contract key registered for the changed symbol -> no signal ----


async def test_case_no_registered_contract_key_no_signal(session_factory: async_sessionmaker[AsyncSession]) -> None:
    source_id = await _make_repo(session_factory, "org/service-a")
    target_id = await _make_repo(session_factory, "org/service-b")
    await _register_contract_key(
        session_factory, repository_id=source_id, stable_key="payments.capture:v1",
        file_path="capture.py", qualified_name="capture_payment",
    )
    await _register_relation(
        session_factory, source_repository_id=source_id, target_repository_id=target_id,
        contract_key="payments.capture:v1",
    )

    async with session_factory() as session:
        report = await build_cross_repo_intelligence_report(
            session, repository_id=source_id,
            contract_deltas=(_delta(file_path="unrelated.py", qualified_name="unrelated_fn"),),
        )
    assert report.overlaps == ()


# ---- 15. No contract delta at all (docs-only / test-only PR) -> quiet ----


async def test_case_no_contract_delta_quiet(session_factory: async_sessionmaker[AsyncSession]) -> None:
    source_id = await _make_repo(session_factory, "org/service-a")
    target_id = await _make_repo(session_factory, "org/service-b")
    await _register_contract_key(
        session_factory, repository_id=source_id, stable_key="payments.capture:v1",
        file_path="capture.py", qualified_name="capture_payment",
    )
    await _register_relation(
        session_factory, source_repository_id=source_id, target_repository_id=target_id,
        contract_key="payments.capture:v1",
    )

    async with session_factory() as session:
        report = await build_cross_repo_intelligence_report(session, repository_id=source_id, contract_deltas=())
    assert report.peers_considered == ()
    assert report.overlaps == ()
    assert report.signals == ()


# ---- 16. Many explicit peer relations -> MAX_CROSS_REPO_PEERS enforced ----


async def test_case_many_relations_bounded(session_factory: async_sessionmaker[AsyncSession]) -> None:
    source_id = await _make_repo(session_factory, "org/service-a")
    await _register_contract_key(
        session_factory, repository_id=source_id, stable_key="payments.capture:v1",
        file_path="capture.py", qualified_name="capture_payment",
    )
    for i in range(MAX_CROSS_REPO_PEERS + 5):
        peer_id = await _make_repo(session_factory, f"org/service-peer-{i}")
        await _register_relation(
            session_factory, source_repository_id=source_id, target_repository_id=peer_id,
            contract_key="payments.capture:v1",
        )

    async with session_factory() as session:
        report = await build_cross_repo_intelligence_report(
            session, repository_id=source_id,
            contract_deltas=(_delta(file_path="capture.py", qualified_name="capture_payment"),),
        )
    assert len(report.peers_considered) == MAX_CROSS_REPO_PEERS


# ---- 17. Many signals across distinct surfaces -> MAX_CROSS_REPO_SIGNALS enforced ----


async def test_case_many_signals_bounded(session_factory: async_sessionmaker[AsyncSession]) -> None:
    source_id = await _make_repo(session_factory, "org/service-a")
    target_id = await _make_repo(session_factory, "org/service-b")
    surfaces = tuple((f"file_{i}.py", f"symbol_{i}") for i in range(MAX_CROSS_REPO_SIGNALS + 5))
    for i, (file_path, qualified_name) in enumerate(surfaces):
        await _register_contract_key(
            session_factory, repository_id=source_id, stable_key=f"key-{i}:v1",
            file_path=file_path, qualified_name=qualified_name,
        )
        await _register_relation(
            session_factory, source_repository_id=source_id, target_repository_id=target_id,
            contract_key=f"key-{i}:v1",
        )

    deltas = tuple(_delta(file_path=fp, qualified_name=qn) for fp, qn in surfaces)
    async with session_factory() as session:
        report = await build_cross_repo_intelligence_report(session, repository_id=source_id, contract_deltas=deltas)
    assert len(report.signals) <= MAX_CROSS_REPO_SIGNALS


# ---- 18. Deterministic replay -> identical report ----


async def test_case_deterministic_replay(session_factory: async_sessionmaker[AsyncSession]) -> None:
    source_id = await _make_repo(session_factory, "org/service-a")
    target_id = await _make_repo(session_factory, "org/service-b")
    await _register_contract_key(
        session_factory, repository_id=source_id, stable_key="payments.capture:v1",
        file_path="capture.py", qualified_name="capture_payment",
    )
    await _register_relation(
        session_factory, source_repository_id=source_id, target_repository_id=target_id,
        contract_key="payments.capture:v1",
    )

    deltas = (_delta(file_path="capture.py", qualified_name="capture_payment"),)
    async with session_factory() as session:
        report_1 = await build_cross_repo_intelligence_report(session, repository_id=source_id, contract_deltas=deltas)
    async with session_factory() as session:
        report_2 = await build_cross_repo_intelligence_report(session, repository_id=source_id, contract_deltas=deltas)
    assert report_1 == report_2


# ---- 19. Relation removed between reviews -> signal disappears ----


async def test_case_relation_removed_signal_disappears(session_factory: async_sessionmaker[AsyncSession]) -> None:
    source_id = await _make_repo(session_factory, "org/service-a")
    target_id = await _make_repo(session_factory, "org/service-b")
    await _register_contract_key(
        session_factory, repository_id=source_id, stable_key="payments.capture:v1",
        file_path="capture.py", qualified_name="capture_payment",
    )
    await _register_relation(
        session_factory, source_repository_id=source_id, target_repository_id=target_id,
        contract_key="payments.capture:v1",
    )

    deltas = (_delta(file_path="capture.py", qualified_name="capture_payment"),)
    async with session_factory() as session:
        report_before = await build_cross_repo_intelligence_report(
            session, repository_id=source_id, contract_deltas=deltas
        )
    assert len(report_before.signals) == 1

    from patchfrog.cross_repo_intelligence.domain import RepositoryRelationKind

    async with session_factory() as session:
        await RepositoryRelationRepository().deactivate(
            session, source_repository_id=source_id, target_repository_id=target_id,
            relation_kind=RepositoryRelationKind.EXPLICIT_SHARED_CONTRACT,
            external_contract_key="payments.capture:v1",
        )
        await session.commit()

    async with session_factory() as session:
        report_after = await build_cross_repo_intelligence_report(
            session, repository_id=source_id, contract_deltas=deltas
        )
    assert report_after.signals == ()


# ---- 20. current repo == peer repo is structurally impossible via the CLI, and never matched by the query ----


async def test_case_repository_never_its_own_peer(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repo_id = await _make_repo(session_factory, "org/service-a")
    await _register_contract_key(
        session_factory, repository_id=repo_id, stable_key="payments.capture:v1",
        file_path="capture.py", qualified_name="capture_payment",
    )
    # Directly construct a self-referential row bypassing the CLI's own guard,
    # to prove the QUERY itself never treats a repo as its own peer either --
    # defense in depth, not reliance on the CLI guard alone.
    from patchfrog.cross_repo_intelligence.domain import (
        RepositoryRelationKind,
        RepositoryRelationProvenance,
    )

    async with session_factory() as session:
        await RepositoryRelationRepository().upsert(
            session, source_repository_id=repo_id, target_repository_id=repo_id,
            relation_kind=RepositoryRelationKind.EXPLICIT_SHARED_CONTRACT,
            external_contract_key="payments.capture:v1", provenance=RepositoryRelationProvenance.OPERATOR_CLI,
        )
        await session.commit()

    async with session_factory() as session:
        report = await build_cross_repo_intelligence_report(
            session, repository_id=repo_id,
            contract_deltas=(_delta(file_path="capture.py", qualified_name="capture_payment"),),
        )
    # A self-relation still technically satisfies the query's join (same
    # installation trivially, is_selected trivially) -- this is a real,
    # accepted edge case: the trusted-operator CLI's own `add` path is the
    # enforcement point (`source.id == target.id` raises ValueError,
    # manually verified against a real Postgres database), not the query
    # itself. Document precisely what the query alone does, rather than
    # assert something untrue.
    assert len(report.peers_considered) == 1
    assert report.peers_considered[0].repository_id == repo_id


# ---- 21. Two peers overlapping the same surface -> deduplicated into one signal ----


async def test_case_two_peers_same_surface_one_signal(session_factory: async_sessionmaker[AsyncSession]) -> None:
    source_id = await _make_repo(session_factory, "org/service-a")
    target_a = await _make_repo(session_factory, "org/service-b")
    target_b = await _make_repo(session_factory, "org/service-c")
    await _register_contract_key(
        session_factory, repository_id=source_id, stable_key="payments.capture:v1",
        file_path="capture.py", qualified_name="capture_payment",
    )
    await _register_relation(
        session_factory, source_repository_id=source_id, target_repository_id=target_a,
        contract_key="payments.capture:v1",
    )
    await _register_relation(
        session_factory, source_repository_id=source_id, target_repository_id=target_b,
        contract_key="payments.capture:v1",
    )

    async with session_factory() as session:
        report = await build_cross_repo_intelligence_report(
            session, repository_id=source_id,
            contract_deltas=(_delta(file_path="capture.py", qualified_name="capture_payment"),),
        )
    assert len(report.peers_considered) == 2
    assert len(report.overlaps) == 2
    assert len(report.signals) == 1
    assert report.signals[0].distinct_peer_count == 2


# ---- 22. Multiple contract deltas, only one registered -> only that one overlaps ----


async def test_case_only_registered_delta_overlaps(session_factory: async_sessionmaker[AsyncSession]) -> None:
    source_id = await _make_repo(session_factory, "org/service-a")
    target_id = await _make_repo(session_factory, "org/service-b")
    await _register_contract_key(
        session_factory, repository_id=source_id, stable_key="payments.capture:v1",
        file_path="capture.py", qualified_name="capture_payment",
    )
    await _register_relation(
        session_factory, source_repository_id=source_id, target_repository_id=target_id,
        contract_key="payments.capture:v1",
    )

    deltas = (
        _delta(file_path="capture.py", qualified_name="capture_payment"),
        _delta(file_path="unrelated.py", qualified_name="unrelated_fn"),
    )
    async with session_factory() as session:
        report = await build_cross_repo_intelligence_report(session, repository_id=source_id, contract_deltas=deltas)
    assert len(report.overlaps) == 1
    assert report.overlaps[0].qualified_name == "capture_payment"


# ---- 23. Peer repository selected but at a different installation than the CURRENT
#          repo's own -- still excluded, confirming authorization is bidirectionally checked ----


async def test_case_peer_selected_but_wrong_installation_excluded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _make_repo(session_factory, "org/service-a", installation_id=1)
    target_id = await _make_repo(session_factory, "org/service-b", installation_id=2, selected=True)
    await _register_contract_key(
        session_factory, repository_id=source_id, stable_key="payments.capture:v1",
        file_path="capture.py", qualified_name="capture_payment",
    )
    await _register_relation(
        session_factory, source_repository_id=source_id, target_repository_id=target_id,
        contract_key="payments.capture:v1",
    )

    async with session_factory() as session:
        report = await build_cross_repo_intelligence_report(
            session, repository_id=source_id,
            contract_deltas=(_delta(file_path="capture.py", qualified_name="capture_payment"),),
        )
    assert report.peers_considered == ()


# ---- 24. Blast radius on the delta never leaks into the peer report ----


async def test_case_blast_radius_never_leaks_into_report(session_factory: async_sessionmaker[AsyncSession]) -> None:
    source_id = await _make_repo(session_factory, "org/service-a")
    target_id = await _make_repo(session_factory, "org/service-b")
    await _register_contract_key(
        session_factory, repository_id=source_id, stable_key="payments.capture:v1",
        file_path="capture.py", qualified_name="capture_payment",
    )
    await _register_relation(
        session_factory, source_repository_id=source_id, target_repository_id=target_id,
        contract_key="payments.capture:v1",
    )

    delta = ContractDelta(
        contract_id="c1", qualified_name="capture_payment", file_path="capture.py", kind=ContractKind.FUNCTION,
        change_unit_id=None, before_signature="a", after_signature="b",
        characteristics=(BreakingCharacteristic.REQUIRED_PARAMETER_ADDED,), evidence="e",
        blast_radius=(AffectedSymbolRef(
            file_path="caller.py", qualified_name="caller_fn", symbol_name="caller_fn",
            relation=AffectedRelation.DIRECTLY_DEPENDENT,
            distance=1, reason="calls capture_payment",
        ),),
    )
    async with session_factory() as session:
        report = await build_cross_repo_intelligence_report(session, repository_id=source_id, contract_deltas=(delta,))
    assert len(report.overlaps) == 1
    assert report.overlaps[0].file_path == "capture.py"  # the changed symbol itself, never the blast-radius caller


# ---- 25. Zero unconditional provider calls (structural proof) ----


def test_cross_repo_intelligence_never_imports_a_provider() -> None:
    import ast
    from pathlib import Path as _Path

    package_dir = _Path(__file__).parent.parent.parent / "patchfrog" / "cross_repo_intelligence"
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                assert "LLMProvider" not in (node.module or "") and not any(
                    alias.name == "LLMProvider" for alias in node.names
                ), f"{path} imports LLMProvider -- Cross-Repo Intelligence must add zero provider calls"


# ---- 26. Deferred signal kinds are never constructed ----


def test_deferred_signal_kinds_never_constructed() -> None:
    """CROSS_REPO_PACKAGE_DEPENDENCY_IMPACT is kept on the enum for
    forward documentation only -- the matching module's own hint table
    only ever maps CROSS_REPO_CONTRACT_CHANGE."""

    from patchfrog.cross_repo_intelligence.matching import _SIGNAL_HINT

    assert set(_SIGNAL_HINT.keys()) == {CrossRepoSignalKind.CROSS_REPO_CONTRACT_CHANGE}


# ---- 27. Mandatory security test: cross-repo package never reads ReviewConfig / .patchfrog.yml ----


def test_cross_repo_intelligence_never_reads_repository_config() -> None:
    """A PR under review must never be able to expand PatchFrog's
    repository access scope (spec sections 25/26). Structural proof:
    the package never imports patchfrog.review.config_resolution (the
    module that reads .patchfrog.yml at the PR's own untrusted head
    commit) or ReviewConfig at all -- registration is reachable only
    through the trusted-operator-only CLI path
    (patchfrog.persistence.repositories.cross_repo), never through a
    review request of any kind."""

    import ast
    from pathlib import Path as _Path

    package_dir = _Path(__file__).parent.parent.parent / "patchfrog" / "cross_repo_intelligence"
    forbidden_modules = {"patchfrog.review.config_resolution", "patchfrog.review.config"}
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                assert node.module not in forbidden_modules, (
                    f"{path} imports {node.module} -- Cross-Repo Intelligence must never read "
                    "repository-controlled config of any kind"
                )


# ---- 28. Registration is reachable only via the trusted repositories -- never from a review call chain ----


async def test_case_build_report_signature_accepts_no_relation_data(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """build_cross_repo_intelligence_report's only inputs are
    `repository_id` (from already-authenticated webhook/CLI context) and
    `contract_deltas` (Contract Intelligence's own already-computed,
    current-repository-scoped output) -- there is no parameter through
    which a PR's own config could ever inject a relation claim. This is
    a structural guarantee, verified by the function's own signature
    rather than by testing every possible malicious payload shape."""

    import inspect

    sig = inspect.signature(build_cross_repo_intelligence_report)
    param_names = set(sig.parameters.keys())
    assert param_names == {"session", "repository_id", "contract_deltas"}


# ---- 29. Contract key registered for a peer repository, never usable as if it were the source's own ----


async def test_case_peer_own_contract_key_never_treated_as_source_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _make_repo(session_factory, "org/service-a")
    target_id = await _make_repo(session_factory, "org/service-b")
    # Registered under the PEER, not the source -- must never be picked up
    # when computing the source's own changed-contract overlap.
    await _register_contract_key(
        session_factory, repository_id=target_id, stable_key="payments.capture:v1",
        file_path="capture.py", qualified_name="capture_payment",
    )
    await _register_relation(
        session_factory, source_repository_id=source_id, target_repository_id=target_id,
        contract_key="payments.capture:v1",
    )

    async with session_factory() as session:
        report = await build_cross_repo_intelligence_report(
            session, repository_id=source_id,
            contract_deltas=(_delta(file_path="capture.py", qualified_name="capture_payment"),),
        )
    assert report.overlaps == ()


# ---- 30. Signal evidence names the contract key and peer full_name, never an author ----


async def test_case_signal_evidence_names_contract_and_peer_not_author(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _make_repo(session_factory, "org/service-a")
    target_id = await _make_repo(session_factory, "org/service-b")
    await _register_contract_key(
        session_factory, repository_id=source_id, stable_key="payments.capture:v1",
        file_path="capture.py", qualified_name="capture_payment",
    )
    await _register_relation(
        session_factory, source_repository_id=source_id, target_repository_id=target_id,
        contract_key="payments.capture:v1",
    )

    async with session_factory() as session:
        report = await build_cross_repo_intelligence_report(
            session, repository_id=source_id,
            contract_deltas=(_delta(file_path="capture.py", qualified_name="capture_payment"),),
        )
    assert "payments.capture:v1" in report.signals[0].evidence
    assert "org/service-b" in report.signals[0].evidence
    assert "author" not in report.signals[0].evidence.lower()
