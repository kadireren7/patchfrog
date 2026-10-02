"""M10.5/M10.7/M11.7: freshness windows, the publication gate, internal-contract identity."""

from __future__ import annotations

from datetime import timedelta

import pytest

from patchfrog.campaigns.domain import EnrolledRepository, Freshness, RepositoryAccess
from patchfrog.campaigns.freshness import FreshnessPolicy, assess_freshness
from patchfrog.campaigns.internal import (
    InternalContract,
    InternalContractError,
    InternalContractKind,
    contract_target,
)
from patchfrog.campaigns.policy import (
    PublicationAction,
    PublicationMode,
    VerificationRequirement,
    WatchMode,
    WorkspacePolicy,
    decide_publication,
)
from patchfrog.dependencies.domain import Ecosystem
from patchfrog.migration_pr.domain import MigrationPREligibility
from tests.support.campaigns import NOW


def _repo(**kw: object) -> EnrolledRepository:
    return EnrolledRepository(full_name="o/r", **kw)  # type: ignore[arg-type]


def test_freshness_states() -> None:
    assert assess_freshness(_repo(last_discovery_at=NOW - timedelta(hours=1)), now=NOW) is Freshness.FRESH
    assert assess_freshness(_repo(last_discovery_at=NOW - timedelta(days=8)), now=NOW) is Freshness.STALE
    assert assess_freshness(_repo(), now=NOW) is Freshness.UNKNOWN
    lost = _repo(access=RepositoryAccess.ACCESS_LOST, last_discovery_at=NOW)
    assert assess_freshness(lost, now=NOW) is Freshness.ACCESS_LOST


def test_freshness_window_is_configurable_and_boundary_is_inclusive() -> None:
    repo = _repo(last_discovery_at=NOW - timedelta(hours=5))
    assert assess_freshness(repo, now=NOW, policy=FreshnessPolicy(window=timedelta(hours=4))) is Freshness.STALE
    assert assess_freshness(repo, now=NOW, policy=FreshnessPolicy(window=timedelta(hours=5))) is Freshness.FRESH
    with pytest.raises(ValueError):
        FreshnessPolicy(window=timedelta(0))


def test_future_discovery_timestamp_is_never_treated_as_fresh() -> None:
    assert assess_freshness(_repo(last_discovery_at=NOW + timedelta(days=1)), now=NOW) is Freshness.UNKNOWN


def test_naive_timestamps_are_treated_as_utc() -> None:
    naive = (NOW - timedelta(hours=1)).replace(tzinfo=None)
    assert assess_freshness(_repo(last_discovery_at=naive), now=NOW) is Freshness.FRESH


E = MigrationPREligibility


def test_safe_defaults_detect_and_never_publish() -> None:
    policy = WorkspacePolicy()
    assert policy.watch_mode is WatchMode.DETECT_ONLY
    assert policy.verification is VerificationRequirement.REQUIRE_VERIFIED
    assert policy.publication is PublicationMode.MANUAL_APPROVAL
    assert not policy.migrates and not policy.may_publish
    assert decide_publication(policy, E.AUTO_OPEN).action is PublicationAction.NONE
    assert not policy.pr_policy().allow_partially_verified


def test_off_mode_analyzes_nothing() -> None:
    assert not WorkspacePolicy(watch_mode=WatchMode.OFF).analyzes


@pytest.mark.parametrize(
    ("mode", "eligibility", "expected"),
    [
        (PublicationMode.AUTOMATIC, E.AUTO_OPEN, PublicationAction.PUBLISH),
        (PublicationMode.DRAFT_ONLY, E.AUTO_OPEN, PublicationAction.PUBLISH_DRAFT),
        (PublicationMode.MANUAL_APPROVAL, E.AUTO_OPEN, PublicationAction.AWAIT_APPROVAL),
        # a partially verified PR is a draft even under automatic publication
        (PublicationMode.AUTOMATIC, E.OPEN_WITH_OPERATOR_APPROVAL, PublicationAction.PUBLISH_DRAFT),
        (PublicationMode.MANUAL_APPROVAL, E.OPEN_WITH_OPERATOR_APPROVAL, PublicationAction.AWAIT_APPROVAL),
        # human-required and not-eligible never publish, whatever the mode
        (PublicationMode.AUTOMATIC, E.PLAN_ONLY, PublicationAction.NONE),
        (PublicationMode.AUTOMATIC, E.NOT_ELIGIBLE, PublicationAction.NONE),
    ],
)
def test_publication_gate(mode: PublicationMode, eligibility: MigrationPREligibility, expected: PublicationAction) -> None:
    policy = WorkspacePolicy(watch_mode=WatchMode.MIGRATE_AND_OPEN_PR, publication=mode)
    assert decide_publication(policy, eligibility).action is expected


def test_migrate_mode_never_publishes() -> None:
    policy = WorkspacePolicy(watch_mode=WatchMode.MIGRATE, publication=PublicationMode.AUTOMATIC)
    assert policy.migrates and not policy.may_publish
    assert decide_publication(policy, E.AUTO_OPEN).action is PublicationAction.NONE


def test_partially_verified_requires_explicit_opt_in() -> None:
    policy = WorkspacePolicy(verification=VerificationRequirement.ALLOW_PARTIALLY_VERIFIED)
    assert policy.pr_policy().allow_partially_verified


def test_internal_contract_needs_explicit_identity() -> None:
    with pytest.raises(InternalContractError, match="no explicit identity"):
        InternalContract("c", InternalContractKind.PACKAGE, "org/lib", contract_target(display_name="looks-related"))
    with pytest.raises(InternalContractError):
        InternalContract("", InternalContractKind.PACKAGE, "org/lib",
                         contract_target(package_name="x", ecosystem=Ecosystem.PYPI))
    with pytest.raises(InternalContractError):
        InternalContract("c", InternalContractKind.PACKAGE, "",
                         contract_target(package_name="x", ecosystem=Ecosystem.PYPI))
    # a package name without an ecosystem is not an identity either
    with pytest.raises(InternalContractError):
        InternalContract("c", InternalContractKind.PACKAGE, "org/lib", contract_target(package_name="x"))


def test_internal_contract_identity_forms_are_accepted_and_never_use_provider_key() -> None:
    for target in (
        contract_target(package_name="acme-shared", ecosystem=Ecosystem.PYPI),
        contract_target(api_hosts=["api.internal.example"]),
        contract_target(dependency_key="acme-shared:pypi"),
    ):
        contract = InternalContract("c", InternalContractKind.OPENAPI, "org/lib", target)
        assert contract.target.provider_key is None
