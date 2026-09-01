"""JSON export (and a light Markdown summary) for telemetry snapshots/
aggregates.

Mirrors :mod:`patchfrog.evaluation.reporting`'s file-artifact-first
approach: telemetry results are typed dataclasses in memory and plain
JSON dicts on disk, round-tripped once through ``json.dumps``/
``json.loads`` so every :class:`~enum.StrEnum` member and
:class:`uuid.UUID` normalizes to a plain string at report-build time,
never at write time, and any non-JSON-native value is caught early. Every
export carries :data:`~patchfrog.telemetry.domain.TELEMETRY_SCHEMA_VERSION`
so a consumer (CI, a future dashboard, an ad-hoc script) can key off a
stable version number, never off parsing prose or guessing shape.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from patchfrog.telemetry.domain import ReviewTelemetrySnapshot, TelemetryAggregate


def snapshot_to_dict(snapshot: ReviewTelemetrySnapshot) -> dict[str, Any]:
    payload = asdict(snapshot)
    return cast("dict[str, Any]", json.loads(json.dumps(payload, default=str)))


def aggregate_to_dict(aggregate: TelemetryAggregate) -> dict[str, Any]:
    payload = asdict(aggregate)
    return cast("dict[str, Any]", json.loads(json.dumps(payload, default=str)))


def write_json(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def read_json(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(path.read_text()))


def render_markdown_snapshot(payload: dict[str, Any]) -> str:
    """A short, human-readable rendering of one snapshot's JSON export --
    for PR artifacts / local operator review, never the canonical
    machine-readable form (that's always the JSON)."""

    provider = payload["provider"]
    lines: list[str] = []
    lines.append(f"# PatchFrog Review Telemetry -- run `{payload['review_run_id']}`")
    lines.append("")
    lines.append(f"- schema_version: {payload['schema_version']}")
    lines.append(f"- status: `{payload['status']}`  commit: `{payload['commit_sha'][:12]}`")
    lines.append(
        f"- candidates: {payload['candidate_count']} (reviewed {payload['candidates_reviewed']}, "
        f"failed {payload['candidates_failed']}, skipped_budget {payload['candidates_skipped_budget']}, "
        f"escalated {payload['candidates_escalated']})"
    )
    lines.append(f"- proposals: {len(payload['finding_lifecycle'])}")
    lines.append(
        f"- reviewer: `{provider['reviewer_provider']}`/`{provider['reviewer_model']}` -- "
        f"{provider['reviewer_calls_total']} calls, "
        f"{provider['reviewer_input_tokens_total']} in / {provider['reviewer_output_tokens_total']} out / "
        f"{provider['reviewer_thinking_tokens_total']} thinking tokens, "
        f"{provider['reviewer_latency_ms_aggregate']:.0f}ms provider-work latency aggregate "
        f"(NOT wall clock -- see duration_ms below)"
    )
    lines.append(
        f"- critic: `{provider['critic_provider']}`/`{provider['critic_model']}` -- "
        f"{provider['critic_calls_total']} calls, "
        f"{provider['critic_input_tokens_total']} in / {provider['critic_output_tokens_total']} out / "
        f"{provider['critic_thinking_tokens_total']} thinking tokens, "
        f"{provider['critic_latency_ms_aggregate']:.0f}ms provider-work latency aggregate"
    )
    lines.append(f"- retries consumed: {provider['retries_consumed']}")
    duration = payload["duration_ms"]
    lines.append(f"- wall-clock duration_ms: {duration if duration is not None else 'unknown'}")
    lines.append(f"- context bundles: {len(payload['context'])}")
    lines.append(f"- feedback-bearing findings: {sum(1 for f in payload['feedback'] if f['has_feedback'])} / {len(payload['feedback'])}")
    lines.append("")
    return "\n".join(lines)
