"""Bounded evidence text for the critic's own prompt -- the
``<executable_verification>`` section. Never shown to the original
specialist (verification only ever runs after a specialist has already
proposed a finding -- see
``validation/executable_verification/latest-summary.md`` section 7).

**Never tells the LLM a conclusion**: neutral wording only ("treat
runtime execution as evidence, not as an automatic conclusion; verify
that the failure actually exercises the proposed issue"), regardless of
outcome. `PASSED` is never phrased as "this proves the finding is
wrong"; `CONFIRMED_FAILURE` is never phrased as "this proves the finding
is right." No raw unbounded logs -- only the already-bounded excerpt
carried on the evidence object itself.
"""

from __future__ import annotations

from patchfrog.executable_verification.domain import (
    ExecutableVerificationReport,
    VerificationOutcome,
)

_INSTRUCTION = (
    "Treat runtime execution as evidence, not as an automatic conclusion. "
    "Verify that the failure (or pass) actually exercises the proposed issue "
    "before weighing it."
)


#: Only these two outcomes carry anything actionable for the critic to
#: weigh -- TIMEOUT/SANDBOX_ERROR/UNSUPPORTED/INCONCLUSIVE are all
#: "we could not get a clear signal," never rendered as evidence at all
#: (spec: "INCONCLUSIVE: no conclusion").
_RENDERED_OUTCOMES = frozenset({VerificationOutcome.PASSED, VerificationOutcome.CONFIRMED_FAILURE})


def evidence_text_for_report(report: ExecutableVerificationReport) -> str:
    if not report.attempted or report.evidence is None:
        return ""
    evidence = report.evidence
    if evidence.outcome not in _RENDERED_OUTCOMES:
        return ""

    lines = [
        "verification: existing targeted test",
        f"target: {evidence.test_target_path}",
        f"result: {evidence.outcome.value}",
    ]
    if evidence.outcome is VerificationOutcome.CONFIRMED_FAILURE and evidence.stdout_excerpt.strip():
        lines.append(f"excerpt: {evidence.stdout_excerpt.strip()[:500]}")
    lines.append(f"instruction: {_INSTRUCTION}")
    return "\n".join(lines)
