"""Runtime provider execution failover -- Milestone U pre-merge
correction (Blocker 1). Exercises
:class:`~patchfrog.review.orchestration.AgentOrchestrator` directly
(the exact same construction pattern as
``test_executable_verification_corpus.py``'s own orchestrator test) with
real :class:`~patchfrog.review.providers.fake.FakeLLMProvider`
primary/fallback pairs.

**Deliberately distinct from Model Router's own config-time
provider-*selection* fallback** (``tests/unit/test_model_router.py``,
``PATCHFROG_ROUTER_FALLBACK_PROVIDER`` chosen because the preferred
provider has no credential at all, before any call is ever made). This
file only ever exercises *runtime execution failover*: the primary
provider is fully configured and used, but an actual bounded review call
to it fails."""

from __future__ import annotations

import asyncio

import structlog

from patchfrog.analysis.domain import Confidence
from patchfrog.context.tokens import estimate_tokens
from patchfrog.review.agents.evidence import CandidateEvidencePackage
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.critic import CriticService
from patchfrog.review.domain import ReviewCandidate, ReviewCandidateReason
from patchfrog.review.effort import ReviewEffortDecision
from patchfrog.review.effort_types import CriticExpectation, ReviewEffortTier
from patchfrog.review.orchestration import AgentOrchestrator
from patchfrog.review.prompt import build_agent_prompt
from patchfrog.review.provider import ProviderFatalError, ProviderTransientError
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse

_VALID = ScriptedResponse(raw_json='{"findings": []}')
_MALFORMED = ScriptedResponse(raw_json="not valid json at all")

_log = structlog.get_logger(__name__)


def _candidate() -> ReviewCandidate:
    return ReviewCandidate(
        file_path="m.py", symbol_id=None, symbol_name="f", qualified_name="f",
        start_line=1, end_line=2, changed_lines=(1,), static_finding_ids=(),
        reason=ReviewCandidateReason.CHANGED_SYMBOL,
    )


def _evidence() -> CandidateEvidencePackage:
    return CandidateEvidencePackage(
        candidate=_candidate(), context_text="ctx", diff_excerpt="", static_findings=(),
        allowed_file_paths=frozenset({"m.py"}), context_bundle_id=None,
    )


def _effort_decision(
    *, roles: frozenset[AgentRole] = frozenset({AgentRole.CORRECTNESS}), retry_limit: int = 1,
) -> ReviewEffortDecision:
    return ReviewEffortDecision(
        tier=ReviewEffortTier.LIGHT, reasons=(), selected_roles=roles,
        context_token_fraction=0.5, context_adaptive_enabled=False,
        critic_expectation=CriticExpectation.OPTIONAL, retry_limit=retry_limit, per_role_output_token_fraction=0.5,
    )


async def _run_review(orchestrator: AgentOrchestrator, *, roles: frozenset[AgentRole], retry_limit: int = 1):  # type: ignore[no-untyped-def]
    return await orchestrator.review_candidate(
        _evidence(), effort_decision=_effort_decision(roles=roles, retry_limit=retry_limit),
        min_final_confidence=Confidence.MEDIUM, max_total_input_tokens=100_000,
        budget_lock=asyncio.Lock(), budget_state={"used_input_tokens": 0}, log=_log,
    )


# ---- 1-2: config-time selection vs primary-available (structural, see test_model_router.py) ----
# Covered entirely in tests/unit/test_model_router.py -- not duplicated here.


# ---- 3-6: primary transient failure classes -> runtime fallback ----


async def test_primary_transient_error_triggers_bounded_runtime_fallback() -> None:
    primary = FakeLLMProvider([ProviderTransientError("rate limited")], provider_name="primary")
    fallback = FakeLLMProvider([_VALID], provider_name="fallback")
    orchestrator = AgentOrchestrator(
        reviewer_providers={AgentRole.CORRECTNESS: primary},
        reviewer_fallback_providers={AgentRole.CORRECTNESS: fallback},
        critic=None, critic_enabled=False, max_output_tokens_per_candidate=1000, max_retries=0,
    )
    result = await _run_review(orchestrator, roles=frozenset({AgentRole.CORRECTNESS}), retry_limit=0)

    assert result.failed is False
    assert result.failed_roles == ()
    assert result.fallback_used_roles == (AgentRole.CORRECTNESS,)
    assert result.executed_provider_by_role[AgentRole.CORRECTNESS] == "fallback"
    assert len(primary.calls) == 1
    assert len(fallback.calls) == 1


async def test_primary_timeout_shaped_transient_error_triggers_fallback() -> None:
    # "Timeout" surfaces as ProviderTransientError at the adapter
    # boundary (see e.g. openai_provider.py's own APIConnectionError
    # mapping) -- this asserts the orchestrator treats it identically to
    # any other transient failure, never specially.
    primary = FakeLLMProvider([ProviderTransientError("connection/timeout error: timed out")])
    fallback = FakeLLMProvider([_VALID])
    orchestrator = AgentOrchestrator(
        reviewer_providers={AgentRole.CORRECTNESS: primary},
        reviewer_fallback_providers={AgentRole.CORRECTNESS: fallback},
        critic=None, critic_enabled=False, max_output_tokens_per_candidate=1000, max_retries=0,
    )
    result = await _run_review(orchestrator, roles=frozenset({AgentRole.CORRECTNESS}), retry_limit=0)
    assert result.fallback_used_roles == (AgentRole.CORRECTNESS,)


async def test_primary_rate_limit_shaped_transient_error_triggers_fallback() -> None:
    primary = FakeLLMProvider([ProviderTransientError("rate limited: 429")])
    fallback = FakeLLMProvider([_VALID])
    orchestrator = AgentOrchestrator(
        reviewer_providers={AgentRole.CORRECTNESS: primary},
        reviewer_fallback_providers={AgentRole.CORRECTNESS: fallback},
        critic=None, critic_enabled=False, max_output_tokens_per_candidate=1000, max_retries=0,
    )
    result = await _run_review(orchestrator, roles=frozenset({AgentRole.CORRECTNESS}), retry_limit=0)
    assert result.fallback_used_roles == (AgentRole.CORRECTNESS,)


async def test_primary_5xx_shaped_transient_error_triggers_fallback() -> None:
    primary = FakeLLMProvider([ProviderTransientError("server error: 503")])
    fallback = FakeLLMProvider([_VALID])
    orchestrator = AgentOrchestrator(
        reviewer_providers={AgentRole.CORRECTNESS: primary},
        reviewer_fallback_providers={AgentRole.CORRECTNESS: fallback},
        critic=None, critic_enabled=False, max_output_tokens_per_candidate=1000, max_retries=0,
    )
    result = await _run_review(orchestrator, roles=frozenset({AgentRole.CORRECTNESS}), retry_limit=0)
    assert result.fallback_used_roles == (AgentRole.CORRECTNESS,)


# ---- 7-8: fallback succeeds vs fallback also fails ----


async def test_transient_failure_then_fallback_succeeds_review_completes() -> None:
    primary = FakeLLMProvider([ProviderTransientError("rate limited")])
    fallback = FakeLLMProvider([_VALID])
    orchestrator = AgentOrchestrator(
        reviewer_providers={AgentRole.CORRECTNESS: primary},
        reviewer_fallback_providers={AgentRole.CORRECTNESS: fallback},
        critic=None, critic_enabled=False, max_output_tokens_per_candidate=1000, max_retries=0,
    )
    result = await _run_review(orchestrator, roles=frozenset({AgentRole.CORRECTNESS}), retry_limit=0)
    assert result.failed is False
    assert result.failed_roles == ()


async def test_transient_failure_and_fallback_also_fails_role_marked_failed_honestly() -> None:
    primary = FakeLLMProvider([ProviderTransientError("rate limited")])
    fallback = FakeLLMProvider([ProviderTransientError("fallback also rate limited")])
    orchestrator = AgentOrchestrator(
        reviewer_providers={AgentRole.CORRECTNESS: primary},
        reviewer_fallback_providers={AgentRole.CORRECTNESS: fallback},
        critic=None, critic_enabled=False, max_output_tokens_per_candidate=1000, max_retries=0,
    )
    result = await _run_review(orchestrator, roles=frozenset({AgentRole.CORRECTNESS}), retry_limit=0)
    # Both primary and its one permitted fallback failed -- the role is
    # honestly marked failed, never silently reported as "zero findings".
    assert result.failed is True
    assert result.failed_roles == (AgentRole.CORRECTNESS,)
    assert len(primary.calls) == 1
    assert len(fallback.calls) == 1  # exactly one fallback attempt, no retry of its own


# ---- 9-10: no fallback configured / fallback uncredentialed (modeled as "not passed") ----


async def test_no_fallback_configured_means_no_hidden_second_provider() -> None:
    primary = FakeLLMProvider([ProviderTransientError("rate limited")])
    orchestrator = AgentOrchestrator(
        reviewer_providers={AgentRole.CORRECTNESS: primary},
        reviewer_fallback_providers=None,
        critic=None, critic_enabled=False, max_output_tokens_per_candidate=1000, max_retries=0,
    )
    result = await _run_review(orchestrator, roles=frozenset({AgentRole.CORRECTNESS}), retry_limit=0)
    assert result.failed is True
    assert result.failed_roles == (AgentRole.CORRECTNESS,)


async def test_fallback_mapping_missing_this_role_behaves_as_no_fallback() -> None:
    # Models "fallback provider has no credential" -- the router simply
    # never populates a fallback entry for a role it can't build one for
    # (see patchfrog.routing.router), so from the orchestrator's
    # perspective this is identical to "no fallback configured".
    primary = FakeLLMProvider([ProviderTransientError("rate limited")])
    orchestrator = AgentOrchestrator(
        reviewer_providers={AgentRole.CORRECTNESS: primary},
        reviewer_fallback_providers={},  # no entry for CORRECTNESS
        critic=None, critic_enabled=False, max_output_tokens_per_candidate=1000, max_retries=0,
    )
    result = await _run_review(orchestrator, roles=frozenset({AgentRole.CORRECTNESS}), retry_limit=0)
    assert result.failed is True


# ---- 11-12: no more than one hop, never loops back to primary ----


async def test_fallback_never_attempts_a_second_cross_provider_hop() -> None:
    # The fallback itself is called with max_retries=0 -- a failing
    # fallback call is attempted exactly once, never retried, and never
    # routed to a third provider (there is no third provider to route to
    # in this orchestrator's own API shape -- reviewer_fallback_providers
    # names exactly one provider per role, structurally bounding this).
    primary = FakeLLMProvider([ProviderTransientError("primary down")])
    fallback = FakeLLMProvider([ProviderTransientError("fallback also down"), _VALID])
    orchestrator = AgentOrchestrator(
        reviewer_providers={AgentRole.CORRECTNESS: primary},
        reviewer_fallback_providers={AgentRole.CORRECTNESS: fallback},
        critic=None, critic_enabled=False, max_output_tokens_per_candidate=1000, max_retries=0,
    )
    result = await _run_review(orchestrator, roles=frozenset({AgentRole.CORRECTNESS}), retry_limit=0)
    assert result.failed is True
    assert len(fallback.calls) == 1  # never consumed the second scripted (recovering) response


async def test_fallback_never_loops_back_to_primary() -> None:
    primary = FakeLLMProvider([ProviderTransientError("primary down")])
    fallback = FakeLLMProvider([ProviderTransientError("fallback down too")])
    orchestrator = AgentOrchestrator(
        reviewer_providers={AgentRole.CORRECTNESS: primary},
        reviewer_fallback_providers={AgentRole.CORRECTNESS: fallback},
        critic=None, critic_enabled=False, max_output_tokens_per_candidate=1000, max_retries=0,
    )
    await _run_review(orchestrator, roles=frozenset({AgentRole.CORRECTNESS}), retry_limit=0)
    assert len(primary.calls) == 1  # never called a second time after the fallback also failed


# ---- 13-15: bounded total calls + Quality + Cost budget ----


async def test_retry_plus_fallback_total_calls_remain_bounded() -> None:
    # max_retries=2 on the primary -> up to 3 primary attempts, then
    # exactly one fallback attempt if all 3 are transient -- 4 total,
    # never unbounded.
    primary = FakeLLMProvider(
        [ProviderTransientError("1"), ProviderTransientError("2"), ProviderTransientError("3")]
    )
    fallback = FakeLLMProvider([_VALID])
    orchestrator = AgentOrchestrator(
        reviewer_providers={AgentRole.CORRECTNESS: primary},
        reviewer_fallback_providers={AgentRole.CORRECTNESS: fallback},
        critic=None, critic_enabled=False, max_output_tokens_per_candidate=1000, max_retries=2,
    )
    result = await _run_review(orchestrator, roles=frozenset({AgentRole.CORRECTNESS}), retry_limit=2)
    assert result.fallback_used_roles == (AgentRole.CORRECTNESS,)
    assert len(primary.calls) == 3
    assert len(fallback.calls) == 1


async def test_fallback_actual_usage_is_accounted_in_the_run_wide_budget() -> None:
    primary = FakeLLMProvider([ProviderTransientError("down")])
    fallback = FakeLLMProvider(
        [ScriptedResponse(raw_json='{"findings": []}', input_tokens=777)]
    )
    orchestrator = AgentOrchestrator(
        reviewer_providers={AgentRole.CORRECTNESS: primary},
        reviewer_fallback_providers={AgentRole.CORRECTNESS: fallback},
        critic=None, critic_enabled=False, max_output_tokens_per_candidate=1000, max_retries=0,
    )
    budget_state = {"used_input_tokens": 0}
    result = await orchestrator.review_candidate(
        _evidence(), effort_decision=_effort_decision(roles=frozenset({AgentRole.CORRECTNESS}), retry_limit=0),
        min_final_confidence=Confidence.MEDIUM, max_total_input_tokens=100_000,
        budget_lock=asyncio.Lock(), budget_state=budget_state, log=_log,
    )
    assert result.usage_by_role[AgentRole.CORRECTNESS].input_tokens == 777
    # The fallback's real usage flowed into the same reconcile-after-actual
    # pass every role already goes through -- never "free" outside accounting.
    assert budget_state["used_input_tokens"] == 777


async def test_fallback_denied_when_budget_has_no_room_for_it() -> None:
    primary = FakeLLMProvider([ProviderTransientError("down")])
    fallback = FakeLLMProvider([_VALID])
    orchestrator = AgentOrchestrator(
        reviewer_providers={AgentRole.CORRECTNESS: primary},
        reviewer_fallback_providers={AgentRole.CORRECTNESS: fallback},
        critic=None, critic_enabled=False, max_output_tokens_per_candidate=1000, max_retries=0,
    )
    # The exact same estimate the orchestrator itself computes for this
    # one selected role's prompt -- a budget ceiling set to exactly this
    # leaves room for the primary's own upfront reservation (which the
    # existing, unchanged budget gate already allows through) but zero
    # headroom left over for the fallback-specific check afterward.
    system_prompt, user_prompt = build_agent_prompt(
        AgentRole.CORRECTNESS, candidate=_candidate(), context_text="ctx", diff_excerpt="", static_findings=(),
    )
    role_estimate = estimate_tokens(system_prompt) + estimate_tokens(user_prompt)
    budget_state = {"used_input_tokens": 0}
    result = await orchestrator.review_candidate(
        _evidence(), effort_decision=_effort_decision(roles=frozenset({AgentRole.CORRECTNESS}), retry_limit=0),
        min_final_confidence=Confidence.MEDIUM, max_total_input_tokens=role_estimate,
        budget_lock=asyncio.Lock(), budget_state=budget_state, log=_log,
    )
    assert result.failed is True
    assert result.fallback_used_roles == ()
    assert fallback.calls == []


# ---- 16-18: malformed output / fatal / refusal semantics ----


async def test_malformed_structured_response_is_eligible_for_fallback() -> None:
    primary = FakeLLMProvider([_MALFORMED])
    fallback = FakeLLMProvider([_VALID])
    orchestrator = AgentOrchestrator(
        reviewer_providers={AgentRole.CORRECTNESS: primary},
        reviewer_fallback_providers={AgentRole.CORRECTNESS: fallback},
        critic=None, critic_enabled=False, max_output_tokens_per_candidate=1000, max_retries=0,
    )
    result = await _run_review(orchestrator, roles=frozenset({AgentRole.CORRECTNESS}), retry_limit=0)
    assert result.failed is False
    assert result.fallback_used_roles == (AgentRole.CORRECTNESS,)
    # Never retried against the SAME non-compliant primary first.
    assert len(primary.calls) == 1


async def test_fatal_provider_error_is_never_eligible_for_fallback() -> None:
    primary = FakeLLMProvider([ProviderFatalError("401 unauthorized")])
    fallback = FakeLLMProvider([_VALID])
    orchestrator = AgentOrchestrator(
        reviewer_providers={AgentRole.CORRECTNESS: primary},
        reviewer_fallback_providers={AgentRole.CORRECTNESS: fallback},
        critic=None, critic_enabled=False, max_output_tokens_per_candidate=1000, max_retries=1,
    )
    result = await _run_review(orchestrator, roles=frozenset({AgentRole.CORRECTNESS}), retry_limit=1)
    assert result.failed is True
    assert result.failed_roles == (AgentRole.CORRECTNESS,)
    assert result.fallback_used_roles == ()
    assert fallback.calls == []  # never even attempted


async def test_refusal_shaped_fatal_error_is_never_eligible_for_fallback() -> None:
    primary = FakeLLMProvider([ProviderFatalError("provider refused the request")])
    fallback = FakeLLMProvider([_VALID])
    orchestrator = AgentOrchestrator(
        reviewer_providers={AgentRole.CORRECTNESS: primary},
        reviewer_fallback_providers={AgentRole.CORRECTNESS: fallback},
        critic=None, critic_enabled=False, max_output_tokens_per_candidate=1000, max_retries=1,
    )
    result = await _run_review(orchestrator, roles=frozenset({AgentRole.CORRECTNESS}), retry_limit=1)
    assert result.failed is True
    assert fallback.calls == []


# ---- 19: reviewer fallback and critic diversity remain separate ----


async def test_reviewer_fallback_and_critic_diversity_are_independent() -> None:
    reviewer_primary = FakeLLMProvider([ProviderTransientError("down")], provider_name="reviewer-primary")
    reviewer_fallback = FakeLLMProvider([_VALID], provider_name="reviewer-fallback")
    critic_primary_service = CriticService(provider=FakeLLMProvider([], provider_name="critic-primary"))
    orchestrator = AgentOrchestrator(
        reviewer_providers={AgentRole.CORRECTNESS: reviewer_primary},
        reviewer_fallback_providers={AgentRole.CORRECTNESS: reviewer_fallback},
        critic=critic_primary_service, critic_enabled=True, critic_fallback=None,
        max_output_tokens_per_candidate=1000, max_retries=0,
    )
    result = await _run_review(orchestrator, roles=frozenset({AgentRole.CORRECTNESS}), retry_limit=0)
    # The reviewer role's own runtime fallback fired; the critic's own
    # (separately configured, here absent) fallback concept is
    # untouched by that -- no proposals survived (empty findings) so the
    # critic was never even invoked, proving the two mechanisms don't
    # bleed into each other.
    assert result.fallback_used_roles == (AgentRole.CORRECTNESS,)
    assert result.critic_fallback_used is False


# ---- 20: provenance reports what actually executed ----


async def test_executed_provider_provenance_reports_fallback_family_not_primary() -> None:
    primary = FakeLLMProvider([ProviderTransientError("down")], provider_name="primary-family")
    fallback = FakeLLMProvider([_VALID], provider_name="fallback-family")
    orchestrator = AgentOrchestrator(
        reviewer_providers={AgentRole.CORRECTNESS: primary},
        reviewer_fallback_providers={AgentRole.CORRECTNESS: fallback},
        critic=None, critic_enabled=False, max_output_tokens_per_candidate=1000, max_retries=0,
    )
    result = await _run_review(orchestrator, roles=frozenset({AgentRole.CORRECTNESS}), retry_limit=0)
    assert result.executed_provider_by_role[AgentRole.CORRECTNESS] == "fallback-family"


async def test_executed_provider_provenance_reports_primary_when_no_failure() -> None:
    primary = FakeLLMProvider([_VALID], provider_name="primary-family")
    fallback = FakeLLMProvider([], provider_name="fallback-family")
    orchestrator = AgentOrchestrator(
        reviewer_providers={AgentRole.CORRECTNESS: primary},
        reviewer_fallback_providers={AgentRole.CORRECTNESS: fallback},
        critic=None, critic_enabled=False, max_output_tokens_per_candidate=1000, max_retries=0,
    )
    result = await _run_review(orchestrator, roles=frozenset({AgentRole.CORRECTNESS}), retry_limit=0)
    assert result.executed_provider_by_role[AgentRole.CORRECTNESS] == "primary-family"
    assert result.fallback_used_roles == ()
    assert fallback.calls == []


# ---- 24: no live providers (structural -- every test above uses FakeLLMProvider) ----
