"""Phase 8 spec section 36: the production reviewer prompt must NEVER
receive benchmark ground truth -- no expected findings, no benchmark
labels, no forbidden rules. This is the one test that inspects the
actual provider payload and proves that boundary holds, against a real
:class:`~patchfrog.evaluation.runner.EvaluationRunner` run through real
indexing/static-analysis/context/review, with only
:class:`FakeLLMProvider` standing in for the network call.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.analysis.domain import FindingCategory
from patchfrog.evaluation.domain import (
    Difficulty,
    EvaluationCase,
    EvaluationMode,
    ExpectedFinding,
    ForbiddenFinding,
    GroundTruthSource,
    Language,
)
from patchfrog.evaluation.runner import EvaluationRunner
from patchfrog.review.provider import ProviderRequest
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse

_LEAKY_ISSUE_FAMILY = "unmistakably_secret_issue_family_xyz123"
_LEAKY_NOTE = "unmistakably_secret_ground_truth_note_xyz123"
_LEAKY_FORBIDDEN_REASON = "unmistakably_secret_forbidden_reason_xyz123"


def _make_case(tmp_path: Path) -> tuple[EvaluationCase, Path]:
    cases_root = tmp_path / "cases"
    repo_root = cases_root / "leak-check" / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / "billing.py").write_text(
        "class Account:\n"
        "    def can_withdraw(self, balance, amount):\n"
        "        return amount >= balance\n"
    )
    case = EvaluationCase(
        id="leak-check", title="t", description="d", language=Language.PYTHON, fixture="leak-check",
        difficulty=Difficulty.EASY,
        expected=(
            ExpectedFinding(
                id="ef1", category=FindingCategory.CORRECTNESS, file="billing.py", issue_family=_LEAKY_ISSUE_FAMILY,
                symbol="can_withdraw", line=3, ground_truth_source=GroundTruthSource.AI_EXPECTED, notes=_LEAKY_NOTE,
            ),
        ),
        forbidden=(ForbiddenFinding(reason=_LEAKY_FORBIDDEN_REASON, category=FindingCategory.MAINTAINABILITY),),
    )
    return case, cases_root


async def test_no_ground_truth_leaks_into_the_reviewer_or_critic_prompt(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    case, cases_root = _make_case(tmp_path)
    no_findings = ScriptedResponse(raw_json=json.dumps({"findings": []}))

    def factory(request: ProviderRequest) -> ScriptedResponse:
        del request
        return no_findings

    reviewer = FakeLLMProvider(response_factory=factory)
    critic = FakeLLMProvider(response_factory=factory)

    runner = EvaluationRunner(session_factory=session_factory)
    result = await runner.run_case(
        case, cases_root=cases_root, mode=EvaluationMode.FULL_PIPELINE, reviewer_provider=reviewer,
        critic_provider=critic, critic_enabled=True,
    )
    assert not result.is_error, result.error

    all_requests: list[ProviderRequest] = [*reviewer.calls, *critic.calls]
    assert all_requests, "expected at least one provider call to actually inspect"

    for request in all_requests:
        combined = request.system_prompt + "\n" + request.user_prompt + "\n" + json.dumps(request.json_schema)
        assert _LEAKY_ISSUE_FAMILY not in combined
        assert _LEAKY_NOTE not in combined
        assert _LEAKY_FORBIDDEN_REASON not in combined
        assert "ef1" not in combined  # the expected-finding id itself must never leak either
