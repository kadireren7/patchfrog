"""Unit coverage for :mod:`patchfrog.review_memory.config`."""

from __future__ import annotations

from pathlib import Path

from patchfrog.review_memory.config import (
    NO_MEMORY_CONTEXT_FINGERPRINT,
    IncrementalConfig,
    compute_incremental_context_fingerprint,
    compute_memory_compatibility_fingerprint,
    load_incremental_config,
)
from patchfrog.review_memory.domain import IncrementalModePolicy


def test_missing_config_file_falls_back_to_defaults(tmp_path: Path) -> None:
    config = load_incremental_config(tmp_path)
    assert config == IncrementalConfig()
    assert config.incremental is IncrementalModePolicy.AUTO
    assert config.memory_enabled is True


def test_malformed_yaml_falls_back_to_defaults(tmp_path: Path) -> None:
    (tmp_path / ".patchfrog.yml").write_text("review:\n  incremental: [unterminated\n")
    config = load_incremental_config(tmp_path)
    assert config == IncrementalConfig()


def test_valid_config_parses_incremental_and_memory_settings(tmp_path: Path) -> None:
    (tmp_path / ".patchfrog.yml").write_text(
        "review:\n"
        "  incremental: force_incremental\n"
        "  suppress_already_reported: false\n"
        "memory:\n"
        "  enabled: false\n"
    )
    config = load_incremental_config(tmp_path)
    assert config.incremental is IncrementalModePolicy.FORCE_INCREMENTAL
    assert config.suppress_already_reported is False
    assert config.memory_enabled is False


def test_non_mapping_top_level_falls_back_to_defaults(tmp_path: Path) -> None:
    (tmp_path / ".patchfrog.yml").write_text("- just\n- a\n- list\n")
    config = load_incremental_config(tmp_path)
    assert config == IncrementalConfig()


def test_compatibility_fingerprint_deterministic() -> None:
    fp1 = compute_memory_compatibility_fingerprint(
        review_engine_version=1, review_prompt_version=1, review_policy_version=1,
        reviewer_provider="anthropic", reviewer_model="claude-opus-5", toolchain_fingerprint=None,
        context_engine_version=1,
    )
    fp2 = compute_memory_compatibility_fingerprint(
        review_engine_version=1, review_prompt_version=1, review_policy_version=1,
        reviewer_provider="anthropic", reviewer_model="claude-opus-5", toolchain_fingerprint=None,
        context_engine_version=1,
    )
    assert fp1 == fp2


def test_compatibility_fingerprint_changes_with_model() -> None:
    fp1 = compute_memory_compatibility_fingerprint(
        review_engine_version=1, review_prompt_version=1, review_policy_version=1,
        reviewer_provider="anthropic", reviewer_model="claude-opus-5", toolchain_fingerprint=None,
        context_engine_version=1,
    )
    fp2 = compute_memory_compatibility_fingerprint(
        review_engine_version=1, review_prompt_version=1, review_policy_version=1,
        reviewer_provider="anthropic", reviewer_model="claude-sonnet-5", toolchain_fingerprint=None,
        context_engine_version=1,
    )
    assert fp1 != fp2


def test_compatibility_fingerprint_changes_with_context_engine_version() -> None:
    """Milestone E audit fix: a context-engine change (e.g. adaptive
    multi-hop expansion) must invalidate incremental-review candidate
    skipping -- a skipped candidate's memory could have been built from
    an entirely different context under the new engine."""

    fp1 = compute_memory_compatibility_fingerprint(
        review_engine_version=1, review_prompt_version=1, review_policy_version=1,
        reviewer_provider="anthropic", reviewer_model="claude-opus-5", toolchain_fingerprint=None,
        context_engine_version=1,
    )
    fp2 = compute_memory_compatibility_fingerprint(
        review_engine_version=1, review_prompt_version=1, review_policy_version=1,
        reviewer_provider="anthropic", reviewer_model="claude-opus-5", toolchain_fingerprint=None,
        context_engine_version=2,
    )
    assert fp1 != fp2


def test_full_no_previous_generation_uses_stable_sentinel() -> None:
    assert compute_incremental_context_fingerprint(mode="full", previous_generation_id=None) == (
        NO_MEMORY_CONTEXT_FINGERPRINT
    )


def test_incremental_fingerprint_differs_by_previous_generation() -> None:
    gen_a = "11111111-1111-1111-1111-111111111111"
    gen_b = "22222222-2222-2222-2222-222222222222"
    fp_a = compute_incremental_context_fingerprint(mode="incremental", previous_generation_id=gen_a)
    fp_b = compute_incremental_context_fingerprint(mode="incremental", previous_generation_id=gen_b)
    assert fp_a != fp_b
    assert fp_a != NO_MEMORY_CONTEXT_FINGERPRINT


def test_full_mode_tied_to_a_previous_generation_differs_from_no_memory() -> None:
    """Drift-mode FULL runs (still tied to a previous generation for
    finding-memory reconciliation, per PreviousReviewSelection.usable_for_finding_memory)
    must never collide with the ordinary no-memory-at-all FULL identity."""

    gen = "33333333-3333-3333-3333-333333333333"
    fp = compute_incremental_context_fingerprint(mode="full", previous_generation_id=gen)
    assert fp != NO_MEMORY_CONTEXT_FINGERPRINT
