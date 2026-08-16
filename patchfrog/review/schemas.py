"""JSON Schemas for the two structured-output calls the AI Reviewer makes.

Both are passed verbatim as ``output_config.format.schema`` (see
:mod:`patchfrog.review.providers.anthropic_provider`) -- the provider is
contractually constrained to return only schema-conforming JSON, which is
the first validation layer before anything in
:mod:`patchfrog.review.validation` runs. Every enum here is generated
directly from :mod:`patchfrog.analysis.domain`, never hand-duplicated, so
the schema and the domain model can never drift apart.
"""

from __future__ import annotations

from typing import Any

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.review.domain import CriticDecision

_CATEGORY_VALUES = [c.value for c in FindingCategory]
_SEVERITY_VALUES = [s.value for s in Severity]
_CONFIDENCE_VALUES = [c.value for c in Confidence]
_CRITIC_DECISION_VALUES = [d.value for d in CriticDecision]

_NULLABLE_STRING: dict[str, Any] = {"anyOf": [{"type": "string"}, {"type": "null"}]}


def _nullable_enum(values: list[str]) -> dict[str, Any]:
    return {"anyOf": [{"type": "string", "enum": values}, {"type": "null"}]}


_EVIDENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string"},
        "start_line": {"type": "integer"},
        "end_line": {"type": "integer"},
        "quoted_text": {
            "type": "string",
            "description": "A verbatim excerpt copied exactly from the provided context that supports this finding.",
        },
    },
    "required": ["file_path", "start_line", "end_line", "quoted_text"],
    "additionalProperties": False,
}

_FINDING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "A short, specific title (not a category label)."},
        "message": {"type": "string", "description": "A precise description of the concrete bug or issue."},
        "category": {"type": "string", "enum": _CATEGORY_VALUES},
        "severity": {"type": "string", "enum": _SEVERITY_VALUES},
        "confidence": {
            "type": "string",
            "enum": _CONFIDENCE_VALUES,
            "description": "Your own confidence that this is a real, concrete bug.",
        },
        "file_path": {"type": "string"},
        "start_line": {"type": "integer"},
        "end_line": {"type": "integer"},
        "evidence": {
            "type": "array",
            "items": _EVIDENCE_SCHEMA,
            "description": "One or more verbatim excerpts from the provided context supporting this finding.",
        },
        "reasoning_summary": {
            "type": "string",
            "description": "A concise (1-3 sentence) explanation of why this is a bug. Not your full internal reasoning.",
        },
        "suggested_fix": _NULLABLE_STRING,
    },
    "required": [
        "title",
        "message",
        "category",
        "severity",
        "confidence",
        "file_path",
        "start_line",
        "end_line",
        "evidence",
        "reasoning_summary",
        "suggested_fix",
    ],
    "additionalProperties": False,
}

REVIEW_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": _FINDING_SCHEMA,
            "description": "Zero or more findings. An empty array is a valid, expected, and common response.",
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}

CRITIC_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": _CRITIC_DECISION_VALUES},
        "reasoning_summary": {"type": "string", "description": "1-3 sentences explaining the decision."},
        "downgraded_severity": _nullable_enum(_SEVERITY_VALUES),
        "downgraded_confidence": _nullable_enum(_CONFIDENCE_VALUES),
    },
    "required": ["decision", "reasoning_summary", "downgraded_severity", "downgraded_confidence"],
    "additionalProperties": False,
}
