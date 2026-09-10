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
from patchfrog.review.prompt import build_agent_prompt, build_critic_prompt
from patchfrog.review.provider import (
    ProviderFatalError,
    ProviderIdentity,
    ProviderRequest,
    ProviderResult,
    ProviderTransientError,
)
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse
from patchfrog.review.validation import parse_findings

_VALID = ScriptedResponse(raw_json='{"findings": []}')
_MALFORMED = ScriptedResponse(raw_json="not valid json at all")

# A real, schema-valid finding whose evidence quotes verbatim from
# _evidence()'s own context_text ("ctx") -- needed only for the critic
# fallback tests below, since an empty findings array (like _VALID)
# never reaches the critic at all (CriticSelectionPolicy has nothing to
# critique).
_ONE_FINDING = ScriptedResponse(
    raw_json='{"findings": [{'
    '"title": "t", "message": "m", "category": "correctness", "severity": "medium", '
    '"confidence": "medium", "file_path": "m.py", "start_line": 1, "end_line": 2, '
    '"evidence": [{"file_path": "m.py", "start_line": 1, "end_line": 2, "quoted_text": "ctx"}], '
    '"reasoning_summary": "r", "suggested_fix": null'
    '}]}'
)

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
    critic_expectation: CriticExpectation = CriticExpectation.OPTIONAL,
) -> ReviewEffortDecision:
    return ReviewEffortDecision(
        tier=ReviewEffortTier.LIGHT, reasons=(), selected_roles=roles,
        context_token_fraction=0.5, context_adaptive_enabled=False,
        critic_expectation=critic_expectation, retry_limit=retry_limit, per_role_output_token_fraction=0.5,
    )


class _DelayedFakeProvider:
    """Wraps a real :class:`FakeLLMProvider` but adds a genuine
    ``await asyncio.sleep`` before delegating -- ``FakeLLMProvider``
    itself never actually suspends (no real I/O), so two concurrent
    ``asyncio.gather``ed roles calling it never truly interleave; this
    forces a real yield point so a genuine race between two roles'
    fallback attempts can actually be exercised and proven bounded."""

    def __init__(self, inner: FakeLLMProvider, *, delay_seconds: float = 0.05) -> None:
        self._inner = inner
        self._delay_seconds = delay_seconds

    @property
    def identity(self) -> ProviderIdentity:
        return self._inner.identity

    @property
    def calls(self) -> list[ProviderRequest]:
        return self._inner.calls

    async def generate_structured(self, request: ProviderRequest) -> ProviderResult:
        await asyncio.sleep(self._delay_seconds)
        return await self._inner.generate_structured(request)


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


# ---- Final correction: atomic budget reservation for the fallback hop ----


async def test_concurrent_two_role_fallback_with_budget_room_for_one_allows_exactly_one() -> None:
    # Both CORRECTNESS and SECURITY are selected and run concurrently
    # (asyncio.gather); both primaries fail transiently and both have a
    # fallback configured. The budget ceiling is set so there is room
    # for the upfront reservation of *both* roles' prompts, plus exactly
    # one more role-sized fallback attempt -- never two. This proves the
    # check-and-reserve is genuinely atomic under the shared budget_lock:
    # a non-atomic (check, then separately increment) implementation
    # could let both concurrent fallback attempts pass a stale check
    # before either reserves, overspending the budget.
    correctness_estimate = sum(
        estimate_tokens(t) for t in build_agent_prompt(
            AgentRole.CORRECTNESS, candidate=_candidate(), context_text="ctx", diff_excerpt="", static_findings=(),
        )
    )
    security_estimate = sum(
        estimate_tokens(t) for t in build_agent_prompt(
            AgentRole.SECURITY, candidate=_candidate(), context_text="ctx", diff_excerpt="", static_findings=(),
        )
    )
    combined_estimate = correctness_estimate + security_estimate
    max_total_input_tokens = combined_estimate + max(correctness_estimate, security_estimate)

    # FakeLLMProvider never actually suspends (no real I/O), so without
    # an artificial delay the two roles would never genuinely interleave
    # under asyncio.gather -- one would run its entire primary-fails ->
    # fallback-succeeds -> release-hold sequence to completion before
    # the other even started, which would trivially "pass" this test
    # for the wrong reason. Both primaries get a small delay so they
    # fail at roughly the same simulated time; the shared fallback gets
    # a longer delay so whichever role wins the reservation race is
    # still holding it when the other role's check runs.
    correctness_primary = _DelayedFakeProvider(
        FakeLLMProvider([ProviderTransientError("down")], provider_name="correctness-primary"), delay_seconds=0.01,
    )
    security_primary = _DelayedFakeProvider(
        FakeLLMProvider([ProviderTransientError("down")], provider_name="security-primary"), delay_seconds=0.01,
    )
    fallback = _DelayedFakeProvider(
        FakeLLMProvider([_VALID], provider_name="shared-fallback"), delay_seconds=0.05,
    )
    orchestrator = AgentOrchestrator(
        reviewer_providers={AgentRole.CORRECTNESS: correctness_primary, AgentRole.SECURITY: security_primary},
        reviewer_fallback_providers={AgentRole.CORRECTNESS: fallback, AgentRole.SECURITY: fallback},
        critic=None, critic_enabled=False, max_output_tokens_per_candidate=1000, max_retries=0,
    )
    result = await orchestrator.review_candidate(
        _evidence(), effort_decision=_effort_decision(roles=frozenset({AgentRole.CORRECTNESS, AgentRole.SECURITY}), retry_limit=0),
        min_final_confidence=Confidence.MEDIUM, max_total_input_tokens=max_total_input_tokens,
        budget_lock=asyncio.Lock(), budget_state={"used_input_tokens": 0}, log=_log,
    )

    # Exactly one role's fallback was actually attempted -- never both,
    # never zero.
    assert len(fallback.calls) == 1
    assert len(result.fallback_used_roles) == 1
    assert len(result.failed_roles) == 1
    assert result.failed is False  # the candidate still has one surviving role


# ---- Final correction: critic fallback budget reservation ----


async def test_critic_fallback_is_denied_when_budget_has_no_room() -> None:
    finding = parse_findings(_ONE_FINDING.raw_json)[0]
    critic_system, critic_user = build_critic_prompt(candidate=_candidate(), context_text="ctx", finding=finding)
    critic_estimate = estimate_tokens(critic_system) + estimate_tokens(critic_user)
    # The reviewer's own *upfront* estimate is credited back and replaced
    # by its actual usage the moment it succeeds -- ScriptedResponse's
    # default input_tokens (100), not the (much larger) prompt estimate
    # -- so that reconciled, *actual* value is what remains in the budget
    # by the time the critic's own reservation is checked. Enough room
    # for that plus the critic's *primary* attempt's own reservation, but
    # zero room left over for a second, fallback-sized critic reservation.
    reviewer_actual_usage = 100
    max_total_input_tokens = reviewer_actual_usage + critic_estimate

    primary = FakeLLMProvider([_ONE_FINDING], provider_name="reviewer")
    critic_primary = CriticService(provider=FakeLLMProvider([ProviderTransientError("down")], provider_name="critic-primary"))
    critic_fallback_provider = FakeLLMProvider([], provider_name="critic-fallback")  # exhausted -- raises if ever called
    critic_fallback = CriticService(provider=critic_fallback_provider)
    orchestrator = AgentOrchestrator(
        reviewer_providers={AgentRole.CORRECTNESS: primary},
        critic=critic_primary, critic_fallback=critic_fallback, critic_enabled=True,
        max_output_tokens_per_candidate=1000, max_retries=0,
    )
    result = await orchestrator.review_candidate(
        _evidence(),
        effort_decision=_effort_decision(
            roles=frozenset({AgentRole.CORRECTNESS}), retry_limit=0, critic_expectation=CriticExpectation.MANDATORY,
        ),
        min_final_confidence=Confidence.MEDIUM, max_total_input_tokens=max_total_input_tokens,
        budget_lock=asyncio.Lock(), budget_state={"used_input_tokens": 0}, log=_log,
    )
    assert result.critic_fallback_used is False
    assert critic_fallback_provider.calls == []


async def test_critic_fallback_budget_reservation_is_released_after_success() -> None:
    primary = FakeLLMProvider([_ONE_FINDING], provider_name="reviewer")
    critic_primary_provider = FakeLLMProvider([ProviderTransientError("down")], provider_name="critic-primary")
    critic_fallback_provider = FakeLLMProvider(
        response_factory=lambda _req: ScriptedResponse(
            raw_json='{"decision": "accept", "reasoning_summary": "r"}', input_tokens=321,
        ),
        provider_name="critic-fallback",
    )
    orchestrator = AgentOrchestrator(
        reviewer_providers={AgentRole.CORRECTNESS: primary},
        critic=CriticService(provider=critic_primary_provider),
        critic_fallback=CriticService(provider=critic_fallback_provider),
        critic_enabled=True, max_output_tokens_per_candidate=1000, max_retries=0,
    )
    budget_state = {"used_input_tokens": 0}
    result = await orchestrator.review_candidate(
        _evidence(),
        effort_decision=_effort_decision(
            roles=frozenset({AgentRole.CORRECTNESS}), retry_limit=0, critic_expectation=CriticExpectation.MANDATORY,
        ),
        min_final_confidence=Confidence.MEDIUM, max_total_input_tokens=100_000,
        budget_lock=asyncio.Lock(), budget_state=budget_state, log=_log,
    )
    assert result.critic_fallback_used is True
    assert result.critic_executed_provider == "critic-fallback"
    # The critic fallback's temporary reservation was fully released --
    # never double-counted alongside the final reconciliation's own
    # crediting of the critic's real usage.
    assert budget_state["used_input_tokens"] == result.reviewer_usage.input_tokens + 321


# ---- 24: no live providers (structural -- every test above uses FakeLLMProvider) ----
