"""Tests for :mod:`patchfrog.telemetry.reporting` -- JSON export shape
and structural privacy guarantees. See
``tests/integration/test_telemetry_collector.py`` for the end-to-end
redaction proof against real persisted secret-shaped content."""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

from patchfrog.telemetry.domain import (
    TELEMETRY_SCHEMA_VERSION,
    ProviderTelemetry,
    ReviewTelemetrySnapshot,
)
from patchfrog.telemetry.reporting import (
    read_json,
    render_markdown_snapshot,
    snapshot_to_dict,
    write_json,
)

_FORBIDDEN_FIELD_NAMES = {
    "context_text",
    "raw_prompt",
    "system_prompt",
    "user_prompt",
    "quoted_text",
    "diff_excerpt",
    "content",
    "message",
    "reasoning_summary",
    "impact",
    "suggested_fix",
    "raw_json",
}


def _snapshot() -> ReviewTelemetrySnapshot:
    return ReviewTelemetrySnapshot(
        schema_version=TELEMETRY_SCHEMA_VERSION, review_run_id=uuid.uuid4(), repository_id=uuid.uuid4(),
        pull_request_id=None, status="succeeded", commit_sha="a" * 40, started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:01:00+00:00", duration_ms=100.0, candidate_count=1, candidates_reviewed=1,
        candidates_failed=0, candidates_skipped_budget=0, candidates_escalated=0, candidates=(),
        finding_lifecycle=(),
        provider=ProviderTelemetry(
            reviewer_provider="fake", reviewer_model="fake-1", critic_provider=None, critic_model=None,
            reviewer_calls_total=1, reviewer_input_tokens_total=10, reviewer_output_tokens_total=5,
            reviewer_thinking_tokens_total=0, reviewer_by_role=(), reviewer_latency_ms_aggregate=50.0,
            critic_calls_total=0, critic_input_tokens_total=0, critic_output_tokens_total=0,
            critic_thinking_tokens_total=0, critic_latency_ms_aggregate=0.0, retries_consumed=0,
        ),
        context=(), feedback=(),
    )


def test_no_telemetry_dataclass_carries_a_forbidden_content_field() -> None:
    """Structural guarantee: none of the telemetry domain dataclasses
    even *have* a field that could hold code/prompt/evidence content --
    this is enforced by the type system, not by a runtime scrub."""

    import patchfrog.telemetry.domain as domain_module

    for name in dir(domain_module):
        obj = getattr(domain_module, name)
        if not dataclasses.is_dataclass(obj):
            continue
        field_names = {f.name for f in dataclasses.fields(obj)}
        overlap = field_names & _FORBIDDEN_FIELD_NAMES
        assert not overlap, f"{name} has forbidden field(s): {overlap}"


def test_json_export_contains_schema_version() -> None:
    payload = snapshot_to_dict(_snapshot())
    assert payload["schema_version"] == 1


def test_json_export_uses_plain_strings_not_python_reprs() -> None:
    payload = snapshot_to_dict(_snapshot())
    assert isinstance(payload["review_run_id"], str)
    assert "UUID(" not in payload["review_run_id"]
    assert isinstance(payload["status"], str)


def test_write_and_read_json_round_trips(tmp_path: Path) -> None:
    payload = snapshot_to_dict(_snapshot())
    path = tmp_path / "telemetry.json"
    write_json(payload, path)
    reloaded = read_json(path)
    assert reloaded == payload


def test_markdown_rendering_does_not_raise() -> None:
    rendered = render_markdown_snapshot(snapshot_to_dict(_snapshot()))
    assert "schema_version" in rendered
