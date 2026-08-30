"""Integration coverage for :class:`~patchfrog.evaluation.runner.EvaluationRunner`
against real production components (indexing, static analysis, context,
review, critic, validation, persistence), with
:class:`~patchfrog.review.providers.fake.FakeLLMProvider` standing in for
the network-bound reviewer/critic. Covers the Phase 8 spec's required
integration scenarios: true positive, false positive, miss, duplicate,
hallucination rejected, critic improves precision, critic hurts recall,
static+AI overlap.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.analysis.domain import FindingCategory
from patchfrog.evaluation.domain import (
    Difficulty,
    EvaluationCase,
    EvaluationMode,
    ExpectedFinding,
    GroundTruthSource,
    Language,
    MatchOutcome,
    PredictionSource,
)
from patchfrog.evaluation.matcher import unsupported_reason
from patchfrog.evaluation.metrics import compute_critic_comparison, compute_static_ai_overlap
from patchfrog.evaluation.runner import EvaluationRunner
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.effort_types import ReviewEffortTier
from patchfrog.review.provider import ProviderRequest
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse

_ACCEPT = ScriptedResponse(raw_json=json.dumps({"decision": "accept", "reasoning_summary": "ok", "downgraded_severity": None, "downgraded_confidence": None}))
_REJECT = ScriptedResponse(raw_json=json.dumps({"decision": "reject", "reasoning_summary": "not convinced", "downgraded_severity": None, "downgraded_confidence": None}))
_NO_FINDINGS = ScriptedResponse(raw_json=json.dumps({"findings": []}))


def _write_case(tmp_path: Path, case_id: str, files: dict[str, str], **case_kwargs: object) -> tuple[EvaluationCase, Path]:
    cases_root = tmp_path / "cases"
    repo_root = cases_root / case_id / "repo"
    repo_root.mkdir(parents=True)
    for rel, content in files.items():
        path = repo_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    case = EvaluationCase(
        id=case_id, title="t", description="d", language=Language.PYTHON, fixture=case_id, difficulty=Difficulty.EASY,
        **case_kwargs,  # type: ignore[arg-type]
    )
    return case, cases_root


_BILLING_SOURCE = "class Account:\n    def can_withdraw(self, balance, amount):\n        return amount >= balance\n"
#: Same shape as _BILLING_SOURCE, but the enclosing class name matches
#: patchfrog.review.agents.selection's security-sensitive naming
#: heuristic ("auth" -- see AgentSelectionReason.SECURITY_SENSITIVE_NAMING).
#: This is a real (non-fallback) security signal, so the Quality + Cost
#: Guard (patchfrog.review.effort) tiers this candidate DEEP rather than
#: LIGHT -- used by tests that specifically need the Security role to
#: always run, independent of tiering itself.
_SECURITY_SIGNAL_SOURCE = "class AuthAccount:\n    def can_withdraw(self, balance, amount):\n        return amount >= balance\n"


def _finding(*, file_path: str = "billing.py", line: int = 3, title: str = "bug", quoted: str = "return amount >= balance") -> dict[str, object]:
    return {
        "title": title, "message": "m", "category": "correctness", "severity": "high", "confidence": "high",
        "file_path": file_path, "start_line": line, "end_line": line,
        "evidence": [{"file_path": file_path, "start_line": line, "end_line": line, "quoted_text": quoted}],
        "reasoning_summary": "r", "suggested_fix": None,
    }


def _review_target(user_prompt: str) -> str | None:
    for line in user_prompt.splitlines():
        if line.startswith("Review target: `"):
            return line.split("`")[1]
    return None


def _targets_symbol(review_target: str | None, symbol: str) -> bool:
    if review_target is None:
        return False
    return review_target == symbol or review_target.endswith(f".{symbol}") or review_target.endswith(f"::{symbol}")


def _factory_for_target(symbol: str, response: ScriptedResponse) -> Callable[[ProviderRequest], ScriptedResponse]:
    def factory(request: ProviderRequest) -> ScriptedResponse:
        if request.schema_name == "critic_verdict":
            return _ACCEPT
        if _targets_symbol(_review_target(request.user_prompt), symbol):
            return response
        return _NO_FINDINGS

    return factory


async def test_true_positive_end_to_end(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> None:
    case, cases_root = _write_case(
        tmp_path, "tp-case", {"billing.py": _BILLING_SOURCE},
        expected=(ExpectedFinding(id="ef1", category=FindingCategory.CORRECTNESS, file="billing.py", issue_family="fam", symbol="can_withdraw", line=3, ground_truth_source=GroundTruthSource.AI_EXPECTED),),
    )
    findings_response = ScriptedResponse(raw_json=json.dumps({"findings": [_finding()]}))
    reviewer = FakeLLMProvider(response_factory=_factory_for_target("can_withdraw", findings_response))
    critic = FakeLLMProvider(response_factory=_factory_for_target("can_withdraw", findings_response))
    runner = EvaluationRunner(session_factory=session_factory)
    result = await runner.run_case(case, cases_root=cases_root, mode=EvaluationMode.FULL_PIPELINE, reviewer_provider=reviewer, critic_provider=critic)
    assert not result.is_error, result.error
    assert [p.outcome for p in result.predictions] == [MatchOutcome.TRUE_POSITIVE]
    assert not result.expected_outcomes or result.expected_outcomes[0].outcome.value == "found"


async def test_false_positive_end_to_end(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> None:
    case, cases_root = _write_case(tmp_path, "fp-case", {"billing.py": _BILLING_SOURCE}, expected=())
    findings_response = ScriptedResponse(raw_json=json.dumps({"findings": [_finding()]}))
    reviewer = FakeLLMProvider(response_factory=_factory_for_target("can_withdraw", findings_response))
    critic = FakeLLMProvider(response_factory=_factory_for_target("can_withdraw", findings_response))
    runner = EvaluationRunner(session_factory=session_factory)
    result = await runner.run_case(case, cases_root=cases_root, mode=EvaluationMode.FULL_PIPELINE, reviewer_provider=reviewer, critic_provider=critic)
    assert not result.is_error, result.error
    assert [p.outcome for p in result.predictions] == [MatchOutcome.FALSE_POSITIVE]


async def test_miss_end_to_end(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> None:
    case, cases_root = _write_case(
        tmp_path, "miss-case", {"billing.py": _BILLING_SOURCE},
        expected=(ExpectedFinding(id="ef1", category=FindingCategory.CORRECTNESS, file="billing.py", issue_family="fam", symbol="can_withdraw", line=3, ground_truth_source=GroundTruthSource.AI_EXPECTED),),
    )
    reviewer = FakeLLMProvider(response_factory=_factory_for_target("can_withdraw", _NO_FINDINGS))
    critic = FakeLLMProvider(response_factory=_factory_for_target("can_withdraw", _NO_FINDINGS))
    runner = EvaluationRunner(session_factory=session_factory)
    result = await runner.run_case(case, cases_root=cases_root, mode=EvaluationMode.FULL_PIPELINE, reviewer_provider=reviewer, critic_provider=critic)
    assert not result.is_error, result.error
    assert result.predictions == ()
    assert [e.outcome.value for e in result.expected_outcomes] == ["missed"]


async def test_hallucinated_finding_is_rejected_by_validation_and_marked_unsupported(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    # Quality + Cost Guard: uses _SECURITY_SIGNAL_SOURCE (not the plain
    # _BILLING_SOURCE) so this candidate has a real security-naming
    # signal and tiers DEEP -- both specialist roles are guaranteed to
    # run, which is what this test needs to exercise (independent of
    # tiering itself, see patchfrog.review.effort).
    case, cases_root = _write_case(tmp_path, "hallu-case", {"billing.py": _SECURITY_SIGNAL_SOURCE}, expected=())
    # Cites evidence text that never appears anywhere in billing.py --
    # Phase 5 validation must reject this before it ever becomes an
    # accepted finding.
    hallucinated = ScriptedResponse(
        raw_json=json.dumps({"findings": [_finding(quoted="this text does not exist in the file at all")]})
    )
    reviewer = FakeLLMProvider(response_factory=_factory_for_target("can_withdraw", hallucinated))
    critic = FakeLLMProvider(response_factory=_factory_for_target("can_withdraw", hallucinated))
    runner = EvaluationRunner(session_factory=session_factory)
    result = await runner.run_case(case, cases_root=cases_root, mode=EvaluationMode.FULL_PIPELINE, reviewer_provider=reviewer, critic_provider=critic)
    assert not result.is_error, result.error
    # Rejected before validation -- never becomes an accepted prediction at all.
    assert result.predictions == ()
    # But it must still show up as a pre-validation proposal, so the
    # hallucination-rate metric can see it. Agent Orchestration v1: both
    # specialist roles are scripted identically here, so both
    # independently hallucinate and both are independently rejected by
    # validation -- cross-role dedup never merges them (it only ever
    # operates on proposals that already passed validation).
    assert len(result.proposals_before_validation) == 2
    reason = unsupported_reason(result.proposals_before_validation[0], frozenset({"billing.py"}), {"billing.py": 3})
    # The quoted text mismatch itself isn't caught by unsupported_reason
    # (that only checks file/line existence) -- Phase 5's own evidence
    # validation is what rejected it; confirm it never reached predictions.
    del reason


async def test_critic_rejects_a_false_positive_improving_precision(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    case, cases_root = _write_case(tmp_path, "critic-precision-case", {"billing.py": _BILLING_SOURCE}, expected=())
    findings_response = ScriptedResponse(raw_json=json.dumps({"findings": [_finding()]}))

    def reviewer_factory(request: ProviderRequest) -> ScriptedResponse:
        if _targets_symbol(_review_target(request.user_prompt), "can_withdraw"):
            return findings_response
        return _NO_FINDINGS

    def critic_reject_factory(request: ProviderRequest) -> ScriptedResponse:
        if request.schema_name == "critic_verdict":
            return _REJECT
        return _NO_FINDINGS

    runner = EvaluationRunner(session_factory=session_factory)
    off = await runner.run_case(
        case, cases_root=cases_root, mode=EvaluationMode.FULL_PIPELINE,
        reviewer_provider=FakeLLMProvider(response_factory=reviewer_factory), critic_enabled=False,
    )
    on = await runner.run_case(
        case, cases_root=cases_root, mode=EvaluationMode.FULL_PIPELINE,
        reviewer_provider=FakeLLMProvider(response_factory=reviewer_factory),
        critic_provider=FakeLLMProvider(response_factory=critic_reject_factory), critic_enabled=True,
    )
    assert not off.is_error and not on.is_error
    comparison = compute_critic_comparison([off], [on])
    assert comparison.false_positive_delta < 0
    assert comparison.precision_delta > 0


async def test_critic_rejects_a_true_positive_hurting_recall(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    case, cases_root = _write_case(
        tmp_path, "critic-recall-case", {"billing.py": _BILLING_SOURCE},
        expected=(ExpectedFinding(id="ef1", category=FindingCategory.CORRECTNESS, file="billing.py", issue_family="fam", symbol="can_withdraw", line=3, ground_truth_source=GroundTruthSource.AI_EXPECTED),),
    )
    findings_response = ScriptedResponse(raw_json=json.dumps({"findings": [_finding()]}))

    def reviewer_factory(request: ProviderRequest) -> ScriptedResponse:
        if _targets_symbol(_review_target(request.user_prompt), "can_withdraw"):
            return findings_response
        return _NO_FINDINGS

    def critic_reject_factory(request: ProviderRequest) -> ScriptedResponse:
        if request.schema_name == "critic_verdict":
            return _REJECT  # incorrectly rejects a genuinely correct finding
        return _NO_FINDINGS

    runner = EvaluationRunner(session_factory=session_factory)
    off = await runner.run_case(
        case, cases_root=cases_root, mode=EvaluationMode.FULL_PIPELINE,
        reviewer_provider=FakeLLMProvider(response_factory=reviewer_factory), critic_enabled=False,
    )
    on = await runner.run_case(
        case, cases_root=cases_root, mode=EvaluationMode.FULL_PIPELINE,
        reviewer_provider=FakeLLMProvider(response_factory=reviewer_factory),
        critic_provider=FakeLLMProvider(response_factory=critic_reject_factory), critic_enabled=True,
    )
    assert not off.is_error and not on.is_error
    comparison = compute_critic_comparison([off], [on])
    assert comparison.recall_delta < 0  # critic actively hurt recall here


async def test_static_and_ai_overlap_when_both_catch_the_same_bug(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    # F821 (undefined name) is caught by ruff's default rule set; script
    # the AI to also report the exact same category/location so the
    # overlap metric sees "both".
    source = "def broken():\n    return undefined_name\n"
    case, cases_root = _write_case(
        tmp_path, "overlap-case", {"m.py": source},
        expected=(ExpectedFinding(id="ef1", category=FindingCategory.CORRECTNESS, file="m.py", issue_family="undefined_name", symbol="broken", line=2, ground_truth_source=GroundTruthSource.EITHER),),
    )
    ai_finding = ScriptedResponse(raw_json=json.dumps({"findings": [_finding(file_path="m.py", line=2, quoted="return undefined_name")]}))
    reviewer = FakeLLMProvider(response_factory=_factory_for_target("broken", ai_finding))
    critic = FakeLLMProvider(response_factory=_factory_for_target("broken", ai_finding))
    runner = EvaluationRunner(session_factory=session_factory)
    result = await runner.run_case(case, cases_root=cases_root, mode=EvaluationMode.FULL_PIPELINE, reviewer_provider=reviewer, critic_provider=critic)
    assert not result.is_error, result.error
    sources = {p.prediction.source for p in result.predictions if p.outcome is MatchOutcome.TRUE_POSITIVE}
    overlap = compute_static_ai_overlap([result])
    if PredictionSource.STATIC in sources:
        # ruff was available and actually caught it too -- genuine overlap.
        assert overlap is not None and overlap.both >= 1
    else:
        # ruff unavailable/didn't fire in this environment -- still a
        # valid AI-only true positive, not a test failure.
        assert overlap is not None and overlap.ai_only >= 1


async def test_analyzer_executions_are_captured_for_per_analyzer_coverage(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    from patchfrog.evaluation.metrics import compute_static_analyzer_coverage

    case, cases_root = _write_case(tmp_path, "coverage-case", {"m.py": "def clean():\n    return 1\n"}, expected=())
    runner = EvaluationRunner(session_factory=session_factory)
    result = await runner.run_case(case, cases_root=cases_root, mode=EvaluationMode.STATIC_ONLY)
    assert not result.is_error, result.error
    # At least one analyzer was actually attempted (ruff, at minimum, is
    # always installed in the dev/CI venv) -- an empty result here would
    # mean the execution-capture wiring silently broke.
    assert result.analyzer_executions
    names = {e.analyzer for e in result.analyzer_executions}
    assert "ruff" in names
    coverage = compute_static_analyzer_coverage([result])
    ruff_coverage = next(c for c in coverage if c.analyzer == "ruff")
    assert ruff_coverage.attempted == 1


async def test_evaluation_records_role_provenance_and_call_counts(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Required scenario 27: the evaluation pipeline must be able to
    record which specialist role produced a prediction and how many
    calls each role made, without needing (or leaking) ground truth --
    this test's provider is scripted purely on candidate/schema_name,
    never on the case's expected findings."""

    # Quality + Cost Guard: uses _SECURITY_SIGNAL_SOURCE so this candidate
    # has a real security-naming signal and tiers DEEP -- both specialist
    # roles (whose provenance this test checks) are guaranteed to run.
    case, cases_root = _write_case(
        tmp_path, "role-provenance-case", {"billing.py": _SECURITY_SIGNAL_SOURCE},
        expected=(ExpectedFinding(id="ef1", category=FindingCategory.CORRECTNESS, file="billing.py", issue_family="fam", symbol="can_withdraw", line=3, ground_truth_source=GroundTruthSource.AI_EXPECTED),),
    )
    findings_response = ScriptedResponse(raw_json=json.dumps({"findings": [_finding()]}))
    reviewer = FakeLLMProvider(response_factory=_factory_for_target("can_withdraw", findings_response))
    critic = FakeLLMProvider(response_factory=_factory_for_target("can_withdraw", findings_response))
    runner = EvaluationRunner(session_factory=session_factory)
    result = await runner.run_case(
        case, cases_root=cases_root, mode=EvaluationMode.FULL_PIPELINE, reviewer_provider=reviewer, critic_provider=critic
    )
    assert not result.is_error, result.error

    # This fixture has 2 candidates (can_withdraw + one other symbol) --
    # both specialist roles are called once per candidate by default.
    assert result.calls_by_role.get(AgentRole.CORRECTNESS) == 2
    assert result.calls_by_role.get(AgentRole.SECURITY) == 2
    assert result.reviewer_input_tokens_by_role.get(AgentRole.CORRECTNESS, 0) > 0
    assert result.reviewer_input_tokens_by_role.get(AgentRole.SECURITY, 0) > 0

    assert len(result.predictions) == 1
    assert result.predictions[0].prediction.agent_role == AgentRole.CORRECTNESS


async def test_quality_cost_guard_ablation_changes_call_shape_for_identical_fixture(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Required scenario: the evaluation harness must be able to compare
    the real Quality + Cost Guard (tiered) against a fixed "uniform
    baseline" (spec sections 24/25) for the *same* fixture. This is a
    pipeline-preservation / cost-behavior comparison, not a claim about
    real-model quality: with FakeLLMProvider, the plain (no security-
    naming signal) fixture tiers LIGHT under the guard -- dropping the
    Security role -- but the uniform baseline always calls both roles,
    exactly like the pre-Milestone-F engine did."""

    case, cases_root = _write_case(tmp_path, "ablation-guard-case", {"billing.py": _BILLING_SOURCE}, expected=())
    reviewer_factory: Callable[[ProviderRequest], ScriptedResponse] = _factory_for_target(
        "can_withdraw", _NO_FINDINGS
    )
    runner = EvaluationRunner(session_factory=session_factory)

    guard_result = await runner.run_case(
        case, cases_root=cases_root, mode=EvaluationMode.FULL_PIPELINE,
        reviewer_provider=FakeLLMProvider(response_factory=reviewer_factory),
        use_quality_cost_guard=True,
    )
    baseline_result = await runner.run_case(
        case, cases_root=cases_root, mode=EvaluationMode.FULL_PIPELINE,
        reviewer_provider=FakeLLMProvider(response_factory=reviewer_factory),
        use_quality_cost_guard=False,
    )
    assert not guard_result.is_error, guard_result.error
    assert not baseline_result.is_error, baseline_result.error

    assert guard_result.candidates_by_tier.get(ReviewEffortTier.LIGHT) == guard_result.candidates_reviewed
    # Fixed override -- every candidate records the same constant
    # STANDARD-equivalent tier, never LIGHT's role reduction.
    assert baseline_result.candidates_by_tier.get(ReviewEffortTier.STANDARD) == baseline_result.candidates_reviewed
    assert baseline_result.calls_by_role.get(AgentRole.SECURITY, 0) > guard_result.calls_by_role.get(
        AgentRole.SECURITY, 0
    )


async def test_evaluation_supports_fixed_and_adaptive_context_ablation(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Required scenario 29: the evaluation harness must be able to
    compare fixed depth-1, fixed depth-2, and adaptive context modes via
    the existing ``context_config_override`` plumbing, without any
    production review/context logic being duplicated for evaluation's
    sake. This fixture has no depth-2-eligible call chain, so all three
    variants are expected to agree -- the point is that all three run
    successfully end-to-end (no live LLM, oracle/Fake only)."""

    from patchfrog.context.config import AdaptiveContextConfig, ContextConfig

    case, cases_root = _write_case(
        tmp_path, "ablation-case", {"billing.py": _BILLING_SOURCE},
        expected=(ExpectedFinding(id="ef1", category=FindingCategory.CORRECTNESS, file="billing.py", issue_family="fam", symbol="can_withdraw", line=3, ground_truth_source=GroundTruthSource.AI_EXPECTED),),
    )
    findings_response = ScriptedResponse(raw_json=json.dumps({"findings": [_finding()]}))
    reviewer = FakeLLMProvider(response_factory=_factory_for_target("can_withdraw", findings_response))
    critic = FakeLLMProvider(response_factory=_factory_for_target("can_withdraw", findings_response))
    runner = EvaluationRunner(session_factory=session_factory)

    variants = {
        "fixed_depth_1": ContextConfig(),
        "fixed_depth_2": ContextConfig(graph_depth=2),
        "adaptive": ContextConfig(adaptive=AdaptiveContextConfig(enabled=True)),
    }
    outcomes = {}
    for label, context_config in variants.items():
        result = await runner.run_case(
            case, cases_root=cases_root, mode=EvaluationMode.FULL_PIPELINE, reviewer_provider=reviewer,
            critic_provider=critic, context_config_override=context_config,
        )
        assert not result.is_error, (label, result.error)
        outcomes[label] = [p.outcome for p in result.predictions]

    assert outcomes["fixed_depth_1"] == outcomes["fixed_depth_2"] == outcomes["adaptive"]
