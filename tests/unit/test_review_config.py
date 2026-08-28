from __future__ import annotations

from pathlib import Path

import pytest

from patchfrog.analysis.domain import Confidence
from patchfrog.review.config import (
    MalformedReviewConfigError,
    ReviewConfig,
    ReviewModelIdentity,
    load_review_config,
)


def test_identical_config_has_identical_fingerprint() -> None:
    assert ReviewConfig().fingerprint() == ReviewConfig().fingerprint()


def test_different_model_changes_config_fingerprint() -> None:
    a = ReviewConfig(model="claude-opus-5")
    b = ReviewConfig(model="claude-sonnet-5")
    assert a.fingerprint() != b.fingerprint()


def test_different_min_confidence_changes_config_fingerprint() -> None:
    a = ReviewConfig(min_final_confidence=Confidence.LOW)
    b = ReviewConfig(min_final_confidence=Confidence.HIGH)
    assert a.fingerprint() != b.fingerprint()


def test_identical_model_identity_has_identical_fingerprint() -> None:
    a = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider="anthropic", critic_model="claude-opus-5",
    )
    b = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider="anthropic", critic_model="claude-opus-5",
    )
    assert a.fingerprint() == b.fingerprint()


def test_model_swap_changes_model_fingerprint() -> None:
    """The core toolchain-awareness invariant established for the static
    analysis engine and the context engine: swapping the reviewer model
    must produce a distinct effective identity, never silently reused."""

    a = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider=None, critic_model=None,
    )
    b = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-sonnet-5",
        critic_provider=None, critic_model=None,
    )
    assert a.fingerprint() != b.fingerprint()


def test_provider_swap_changes_model_fingerprint() -> None:
    a = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider=None, critic_model=None,
    )
    b = ReviewModelIdentity(
        reviewer_provider="fake", reviewer_model="claude-opus-5",
        critic_provider=None, critic_model=None,
    )
    assert a.fingerprint() != b.fingerprint()


def test_prompt_version_bump_changes_model_fingerprint() -> None:
    a = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider=None, critic_model=None, prompt_version=1,
    )
    b = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider=None, critic_model=None, prompt_version=2,
    )
    assert a.fingerprint() != b.fingerprint()


def test_policy_version_bump_changes_model_fingerprint() -> None:
    a = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider=None, critic_model=None, policy_version=1,
    )
    b = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider=None, critic_model=None, policy_version=2,
    )
    assert a.fingerprint() != b.fingerprint()


def test_critic_disabled_vs_enabled_changes_model_fingerprint() -> None:
    with_critic = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider="anthropic", critic_model="claude-opus-5",
    )
    without_critic = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider=None, critic_model=None,
    )
    assert with_critic.fingerprint() != without_critic.fingerprint()


def test_load_review_config_absent_file_returns_defaults(tmp_path: Path) -> None:
    config = load_review_config(tmp_path)
    assert config == ReviewConfig()


def test_load_review_config_malformed_yaml_falls_back_to_defaults(tmp_path: Path) -> None:
    (tmp_path / ".patchfrog.yml").write_text("review: [this is not a mapping\n")
    config = load_review_config(tmp_path)
    assert config == ReviewConfig()


def test_load_review_config_reads_review_section(tmp_path: Path) -> None:
    (tmp_path / ".patchfrog.yml").write_text(
        "review:\n  max_candidates: 7\n  min_final_confidence: high\n"
    )
    config = load_review_config(tmp_path)
    assert config.max_candidates == 7
    assert config.min_final_confidence == Confidence.HIGH


def test_load_review_config_ignores_credential_shaped_fields(tmp_path: Path) -> None:
    """Credentials are environment-only (see patchfrog.config.settings) --
    a repository-controlled .patchfrog.yml must never be able to inject
    or override one, even by accident."""

    (tmp_path / ".patchfrog.yml").write_text("review:\n  api_key: sk-ant-should-be-ignored\n")
    config = load_review_config(tmp_path)
    assert not hasattr(config, "api_key")
    assert config == ReviewConfig()


def test_missing_file_defaults_even_under_raise_mode(tmp_path: Path) -> None:
    """A genuinely *missing* file is never "malformed" -- on_malformed
    only governs what happens to a file that exists but can't be parsed."""

    config = load_review_config(tmp_path, on_malformed="raise")
    assert config == ReviewConfig()


def test_malformed_yaml_raises_under_raise_mode(tmp_path: Path) -> None:
    (tmp_path / ".patchfrog.yml").write_text("review: [this is not a mapping\n")
    with pytest.raises(MalformedReviewConfigError) as excinfo:
        load_review_config(tmp_path, on_malformed="raise")
    assert "invalid YAML" in str(excinfo.value)
    assert excinfo.value.raw_text == "review: [this is not a mapping\n"


def test_non_mapping_top_level_raises_under_raise_mode(tmp_path: Path) -> None:
    (tmp_path / ".patchfrog.yml").write_text("- just\n- a\n- list\n")
    with pytest.raises(MalformedReviewConfigError, match="not a mapping"):
        load_review_config(tmp_path, on_malformed="raise")


def test_non_mapping_review_section_raises_under_raise_mode(tmp_path: Path) -> None:
    (tmp_path / ".patchfrog.yml").write_text("review: not-a-mapping\n")
    with pytest.raises(MalformedReviewConfigError, match="'review' section is not a mapping"):
        load_review_config(tmp_path, on_malformed="raise")


def test_invalid_field_value_raises_under_raise_mode(tmp_path: Path) -> None:
    (tmp_path / ".patchfrog.yml").write_text("review:\n  min_final_confidence: not_a_real_level\n")
    with pytest.raises(MalformedReviewConfigError, match="invalid 'review' section"):
        load_review_config(tmp_path, on_malformed="raise")


def test_valid_config_returns_normally_under_raise_mode(tmp_path: Path) -> None:
    (tmp_path / ".patchfrog.yml").write_text("review:\n  max_candidates: 12\n")
    config = load_review_config(tmp_path, on_malformed="raise")
    assert config.max_candidates == 12


def test_credential_fields_still_ignored_under_raise_mode(tmp_path: Path) -> None:
    """Credential handling is orthogonal to on_malformed -- a
    credential-shaped field never becomes a hard failure by itself; the
    field is simply dropped (with a warning), same as in "defaults" mode."""

    (tmp_path / ".patchfrog.yml").write_text("review:\n  api_key: sk-ant-should-be-ignored\n  max_candidates: 4\n")
    config = load_review_config(tmp_path, on_malformed="raise")
    assert not hasattr(config, "api_key")
    assert config.max_candidates == 4


def test_two_different_malformed_contents_have_different_error_raw_text(tmp_path: Path) -> None:
    """The raw text carried on the exception is what downstream identity
    derivation (patchfrog.review.service._malformed_config_fingerprint)
    keys on -- two different bad contents must never be conflated."""

    (tmp_path / ".patchfrog.yml").write_text("review: [bad one\n")
    with pytest.raises(MalformedReviewConfigError) as first:
        load_review_config(tmp_path, on_malformed="raise")

    (tmp_path / ".patchfrog.yml").write_text("review: [bad two\n")
    with pytest.raises(MalformedReviewConfigError) as second:
        load_review_config(tmp_path, on_malformed="raise")

    assert first.value.raw_text != second.value.raw_text


# -- Effective-default normalization (critic_model / request_timeout_seconds) --
#
# Regression coverage for a real gap found live: a repository selecting
# `provider: gemini` without also setting `critic_model` previously kept
# the class-level default `claude-opus-5` (an Anthropic model), so the
# critic call asked Gemini's API for a model that doesn't exist there.
# ReviewConfig now fills in provider-coherent effective values for any
# field the caller genuinely omitted (via pydantic's model_fields_set),
# never for one explicitly supplied -- even when that explicit value
# happens to match the old default string.


def test_a_gemini_critic_model_omitted_defaults_to_reviewer_model() -> None:
    config = ReviewConfig(provider="gemini", model="gemini-3.6-flash")
    assert config.critic_model == "gemini-3.6-flash"


def test_b_gemini_explicit_critic_model_is_preserved() -> None:
    config = ReviewConfig(
        provider="gemini", model="gemini-3.6-flash", critic_model="some-other-valid-gemini-model"
    )
    assert config.critic_model == "some-other-valid-gemini-model"


def test_c_anthropic_defaults_remain_unchanged() -> None:
    config = ReviewConfig()
    assert config.provider == "anthropic"
    assert config.model == "claude-opus-5"
    assert config.critic_model == "claude-opus-5"
    assert config.request_timeout_seconds == 30.0


def test_d_gemini_timeout_omitted_uses_provider_appropriate_default() -> None:
    config = ReviewConfig(provider="gemini", model="gemini-3.6-flash")
    assert config.request_timeout_seconds == 120.0


def test_e_gemini_explicit_timeout_is_preserved() -> None:
    config = ReviewConfig(provider="gemini", model="gemini-3.6-flash", request_timeout_seconds=45.0)
    assert config.request_timeout_seconds == 45.0


def test_f_anthropic_timeout_omitted_preserves_existing_default() -> None:
    config = ReviewConfig(provider="anthropic", model="claude-opus-5")
    assert config.request_timeout_seconds == 30.0


def test_explicit_critic_model_equal_to_old_default_string_is_still_respected() -> None:
    """The distinction must be "was this field present in the input", not
    "does its value differ from the class default" -- a user may
    deliberately choose the exact string that also happens to be the
    default."""

    config = ReviewConfig.model_validate(
        {"provider": "gemini", "model": "gemini-3.6-flash", "critic_model": "claude-opus-5"}
    )
    assert config.critic_model == "claude-opus-5"


def test_minimal_gemini_yaml_config_normalizes_correctly(tmp_path: Path) -> None:
    """The documented minimal Gemini config (provider + model only) must
    be sufficient -- no separately-remembered critic_model or timeout."""

    (tmp_path / ".patchfrog.yml").write_text(
        "review:\n  provider: gemini\n  model: gemini-3.6-flash\n"
    )
    config = load_review_config(tmp_path)
    assert config.critic_model == "gemini-3.6-flash"
    assert config.request_timeout_seconds == 120.0


def test_config_schema_version_bumped_for_effective_default_semantics_change() -> None:
    from patchfrog.review.config import CONFIG_SCHEMA_VERSION

    assert CONFIG_SCHEMA_VERSION == 2


def test_omitted_vs_explicit_gemini_critic_model_produce_different_fingerprints() -> None:
    """A config that silently defaulted critic_model to claude-opus-5
    (the pre-fix behavior) must never be treated as canonically identical
    to one that correctly defaults it to the reviewer model."""

    omitted = ReviewConfig(provider="gemini", model="gemini-3.6-flash")
    explicit_old_default = ReviewConfig(
        provider="gemini", model="gemini-3.6-flash", critic_model="claude-opus-5"
    )
    assert omitted.fingerprint() != explicit_old_default.fingerprint()
