"""PullRequestReviewService's Model Router integration point: a
``route_plan`` overrides ``reviewer_provider``/``critic_provider``
entirely (never merged), and exactly one of the two must be given.
Never constructs a real database session/engine -- these tests only
exercise the constructor's own provider-selection logic."""

from __future__ import annotations

import pytest

from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.providers.fake import FakeLLMProvider
from patchfrog.review.service import PullRequestReviewService
from patchfrog.routing.domain import ReviewRoutePlan, RouteReason


def _fake(provider_name: str) -> FakeLLMProvider:
    return FakeLLMProvider([], provider_name=provider_name, model_id=f"{provider_name}-model")


def _route_plan(
    *, reviewer: FakeLLMProvider, critic: FakeLLMProvider | None, reviewer_family: str, critic_family: str | None,
) -> ReviewRoutePlan:
    return ReviewRoutePlan(
        reviewer_providers={AgentRole.CORRECTNESS: reviewer, AgentRole.SECURITY: reviewer},
        critic_provider=critic,
        reviewer_provider_family=reviewer_family,
        critic_provider_family=critic_family,
        reasons=(RouteReason.SINGLE_PROVIDER_CONFIGURED,),
        diversity_available=False,
        diversity_used=False,
        fallback_used=False,
    )


def test_route_plan_governs_reviewer_providers_mapping() -> None:
    reviewer = _fake("gemini")
    service = PullRequestReviewService(
        session_factory=None,  # type: ignore[arg-type]
        route_plan=_route_plan(reviewer=reviewer, critic=None, reviewer_family="gemini", critic_family=None),
    )
    assert service._reviewer_providers[AgentRole.CORRECTNESS] is reviewer
    assert service._reviewer_providers[AgentRole.SECURITY] is reviewer
    assert service._reviewer_provider is reviewer


def test_route_plan_governs_critic_provider() -> None:
    reviewer = _fake("anthropic")
    critic = _fake("gemini")
    service = PullRequestReviewService(
        session_factory=None,  # type: ignore[arg-type]
        route_plan=_route_plan(reviewer=reviewer, critic=critic, reviewer_family="anthropic", critic_family="gemini"),
    )
    assert service._critic_provider is critic
    assert service._critic is not None


def test_route_plan_with_no_critic_disables_critic() -> None:
    reviewer = _fake("anthropic")
    service = PullRequestReviewService(
        session_factory=None,  # type: ignore[arg-type]
        route_plan=_route_plan(reviewer=reviewer, critic=None, reviewer_family="anthropic", critic_family=None),
    )
    assert service._critic_provider is None
    assert service._critic is None


def test_reviewer_provider_still_works_without_a_route_plan() -> None:
    # Pre-Milestone-U behavior, unchanged: a single reviewer_provider
    # governs every role, exactly as before route_plan existed.
    reviewer = _fake("anthropic")
    service = PullRequestReviewService(session_factory=None, reviewer_provider=reviewer)  # type: ignore[arg-type]
    assert service._reviewer_providers[AgentRole.CORRECTNESS] is reviewer
    assert service._reviewer_providers[AgentRole.SECURITY] is reviewer


def test_route_plan_overrides_reviewer_provider_entirely_never_merged() -> None:
    ignored = _fake("openai")
    route_plan_reviewer = _fake("gemini")
    service = PullRequestReviewService(
        session_factory=None,  # type: ignore[arg-type]
        reviewer_provider=ignored,
        route_plan=_route_plan(
            reviewer=route_plan_reviewer, critic=None, reviewer_family="gemini", critic_family=None,
        ),
    )
    assert service._reviewer_provider is route_plan_reviewer
    assert service._reviewer_provider is not ignored


def test_neither_route_plan_nor_reviewer_provider_raises() -> None:
    with pytest.raises(ValueError, match="route_plan or reviewer_provider"):
        PullRequestReviewService(session_factory=None)  # type: ignore[arg-type]
