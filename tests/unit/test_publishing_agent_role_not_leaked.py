"""Required scenario 23 / spec section 16: publishing format must stay
unchanged by Agent Orchestration v1 -- PatchFrog presents one coherent
review, never internal agent chatter like "Security Agent #2 says...".
:class:`PublishableFinding` (and therefore every rendered comment body)
must never carry or mention which specialist role produced a finding."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.publishing.body import format_inline_comment_body, format_summary_body
from patchfrog.publishing.domain import PublishableFinding
from patchfrog.publishing.queries import publishable_finding_from_model
from patchfrog.review.agents.roles import AgentRole


@dataclass(slots=True)
class _FakeAIFindingModel:
    """A minimal stand-in for AIFindingModel exposing only the
    attributes publishable_finding_from_model actually reads -- proves
    the conversion function structurally cannot read agent_role even
    though the real model has that column."""

    id: uuid.UUID
    title: str
    message: str
    category: FindingCategory
    severity: Severity
    confidence: Confidence
    file_path: str
    start_line: int
    end_line: int
    reasoning_summary: str
    suggested_fix: str | None
    impact: str | None
    agent_role: AgentRole | None  # present on the real model; must never be read here


def test_publishable_finding_has_no_agent_role_field() -> None:
    assert not hasattr(PublishableFinding, "agent_role")
    assert "agent_role" not in PublishableFinding.__dataclass_fields__


def test_publishable_finding_from_model_ignores_agent_role() -> None:
    model = _FakeAIFindingModel(
        id=uuid.uuid4(), title="Untrusted value reaches shell invocation",
        message="user_id reaches subprocess.run without validation", category=FindingCategory.SECURITY,
        severity=Severity.HIGH, confidence=Confidence.HIGH, file_path="src/billing.py",
        start_line=14, end_line=14, reasoning_summary="untrusted input flows to a shell sink",
        suggested_fix=None, impact=None, agent_role=AgentRole.SECURITY,
    )
    finding = publishable_finding_from_model(model)  # type: ignore[arg-type]
    assert not hasattr(finding, "agent_role")


def test_rendered_comment_never_mentions_agent_role_names() -> None:
    finding = PublishableFinding(
        finding_id=uuid.uuid4(), title="Untrusted value reaches shell invocation",
        message="user_id reaches subprocess.run without validation", category=FindingCategory.SECURITY,
        severity=Severity.HIGH, confidence=Confidence.HIGH, file_path="src/billing.py",
        start_line=14, end_line=14, reasoning_summary="untrusted input flows to a shell sink",
        suggested_fix=None, impact=None,
    )
    inline_body, _ = format_inline_comment_body(finding)
    summary_body, _ = format_summary_body(
        publication_id=uuid.uuid4(),
        counts_by_severity={Severity.HIGH: 1},
        inline_findings=[finding],
        summary_only_findings=[],
        omitted_count=0,
    )

    for body in (inline_body, summary_body):
        lowered = body.lower()
        assert "agent" not in lowered
        assert "correctness specialist" not in lowered
        assert "security specialist" not in lowered
