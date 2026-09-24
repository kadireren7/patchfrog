"""Deterministic PR-level change/risk classification (M4.1).

Pure functions of a diff: no LLM, no network, no repository reads. Used
by the review engine to decide how much provider work a change is worth
(including none at all), and designed to be reused by future
dependency-migration workflows to size verification effort.
"""

from patchfrog.change_risk.classifier import (
    ChangeRiskPolicy,
    CommentLineEvidence,
    classify_change,
    python_files_needing_comment_evidence,
)
from patchfrog.change_risk.comments import python_comment_lines
from patchfrog.change_risk.domain import (
    CHANGE_RISK_POLICY_VERSION,
    ChangeRiskClassification,
    ChangeRiskSignal,
    ChangeRiskTier,
    FileChangeClass,
    FileChangeProfile,
)

__all__ = [
    "CHANGE_RISK_POLICY_VERSION",
    "ChangeRiskClassification",
    "ChangeRiskPolicy",
    "ChangeRiskSignal",
    "ChangeRiskTier",
    "CommentLineEvidence",
    "FileChangeClass",
    "FileChangeProfile",
    "classify_change",
    "python_comment_lines",
    "python_files_needing_comment_evidence",
]
