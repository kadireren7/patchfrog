from __future__ import annotations

from pathlib import Path

from patchfrog.analysis.domain import Confidence
from patchfrog.review.config import ReviewConfig, ReviewModelIdentity, load_review_config


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
