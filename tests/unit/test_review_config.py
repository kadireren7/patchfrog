from __future__ import annotations

from pathlib import Path

import pytest

from patchfrog.analysis.domain import Confidence
from patchfrog.config.settings import Settings
from patchfrog.review.config import (
    CONFIG_SCHEMA_VERSION,
    MalformedReviewConfigError,
    ReviewConfig,
    ReviewModelIdentity,
    load_review_config,
)
from patchfrog.review.runtime_config import resolve_review_runtime_config


def test_identical_config_has_identical_fingerprint() -> None:
    assert ReviewConfig().fingerprint() == ReviewConfig().fingerprint()


def test_different_max_candidates_changes_config_fingerprint() -> None:
    a = ReviewConfig(max_candidates=10)
    b = ReviewConfig(max_candidates=20)
    assert a.fingerprint() != b.fingerprint()


def test_different_min_confidence_changes_config_fingerprint() -> None:
    a = ReviewConfig(min_final_confidence=Confidence.LOW)
    b = ReviewConfig(min_final_confidence=Confidence.HIGH)
    assert a.fingerprint() != b.fingerprint()


def test_critic_enabled_toggle_changes_config_fingerprint() -> None:
    a = ReviewConfig(critic_enabled=True)
    b = ReviewConfig(critic_enabled=False)
    assert a.fingerprint() != b.fingerprint()


def test_review_config_has_no_provider_identity_fields() -> None:
    """Trust boundary (Milestone C): provider/model/critic_model/
    request_timeout_seconds are operator-controlled runtime concerns
    (see patchfrog.review.runtime_config), never repository-controlled
    review behavior. ReviewConfig must not even have these attributes,
    so nothing downstream can accidentally read a repo-supplied value."""

    config = ReviewConfig()
    for field in ("provider", "model", "critic_model", "request_timeout_seconds"):
        assert not hasattr(config, field)


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


def test_config_schema_version_bumped_for_operator_boundary_change() -> None:
    # Milestone C bumped 2 -> 3 (provider/model fields removed from
    # repository config); Milestone F (Quality + Cost Guard) bumped
    # 3 -> 4 (max_output_tokens_per_candidate's effective repo-facing
    # meaning changed -- see patchfrog.review.config's module comment).
    assert CONFIG_SCHEMA_VERSION == 4


# -- Trust boundary: provider/model/critic_model/request_timeout_seconds
# are operator-controlled, never repository-controlled (Milestone C).
#
# A repository's `.patchfrog.yml` setting any of these fields must have
# zero effect on which AI provider/model actually runs -- in
# "defaults"/preview mode the fields are stripped with a warning (never
# silently accepted via extra="ignore" with no trace); in "raise" mode
# (the mode both the CLI's real run and the production Celery task use)
# a repository trying to set one of these fields is a hard, actionable
# failure, not a quiet no-op.


@pytest.mark.parametrize("field", ["provider", "model", "critic_model", "request_timeout_seconds"])
def test_operator_only_field_rejected_under_raise_mode(tmp_path: Path, field: str) -> None:
    (tmp_path / ".patchfrog.yml").write_text(f"review:\n  {field}: some-value\n")
    with pytest.raises(MalformedReviewConfigError) as excinfo:
        load_review_config(tmp_path, on_malformed="raise")
    message = str(excinfo.value)
    assert field in message
    assert "operator" in message.lower() or "runtime" in message.lower()


def test_operator_only_fields_rejected_together_under_raise_mode(tmp_path: Path) -> None:
    (tmp_path / ".patchfrog.yml").write_text(
        "review:\n"
        "  provider: gemini\n"
        "  model: gemini-3.6-flash\n"
        "  critic_model: gemini-3.6-flash\n"
        "  request_timeout_seconds: 999\n"
    )
    with pytest.raises(MalformedReviewConfigError) as excinfo:
        load_review_config(tmp_path, on_malformed="raise")
    message = str(excinfo.value)
    for field in ("provider", "model", "critic_model", "request_timeout_seconds"):
        assert field in message


@pytest.mark.parametrize("field", ["provider", "model", "critic_model", "request_timeout_seconds"])
def test_operator_only_field_stripped_under_defaults_mode(tmp_path: Path, field: str) -> None:
    (tmp_path / ".patchfrog.yml").write_text(f"review:\n  {field}: some-value\n  max_candidates: 9\n")
    config = load_review_config(tmp_path)
    assert not hasattr(config, field)
    assert config.max_candidates == 9


def test_malicious_repo_config_cannot_influence_operator_runtime_selection(tmp_path: Path) -> None:
    """The concrete end-to-end trust-boundary guarantee: no matter what a
    repository's `.patchfrog.yml` claims, the operator's actual runtime
    provider/model/critic/timeout -- resolved independently from trusted
    Settings -- is completely unaffected."""

    (tmp_path / ".patchfrog.yml").write_text(
        "review:\n"
        "  provider: gemini\n"
        "  model: some-much-more-expensive-model\n"
        "  critic_model: some-other-critic-model\n"
        "  request_timeout_seconds: 1\n"
    )
    # Preview/default resolution (as a dry-run would use) -- fields
    # stripped, not applied.
    repo_config = load_review_config(tmp_path)
    assert not hasattr(repo_config, "provider")
    assert not hasattr(repo_config, "model")

    operator_settings = Settings(
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        GITHUB_APP_ID="1",
        GITHUB_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
        GITHUB_WEBHOOK_SECRET="x",
    )
    runtime_config = resolve_review_runtime_config(operator_settings)
    assert runtime_config.provider == "anthropic"
    assert runtime_config.model == "claude-opus-5"
    assert runtime_config.critic_model == "claude-opus-5"
    assert runtime_config.request_timeout_seconds == 30.0
