"""patchfrog.review.config_resolution.apply_operator_hard_caps: the
Quality + Cost Guard trust boundary closing a gap Milestone C (operator-
controlled provider runtime) didn't cover -- a repository's own
``.patchfrog.yml`` may request *less* review cost/candidate volume than
the operator allows, but never more. Mirrors the trust-boundary test
style already established in tests/unit/test_review_config.py's
`test_malicious_repo_config_cannot_influence_operator_runtime_selection`.
"""

from __future__ import annotations

import pytest

from patchfrog.config.settings import Settings
from patchfrog.review.config import ReviewConfig
from patchfrog.review.config_resolution import apply_operator_hard_caps


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "REDIS_URL": "redis://localhost:6379/0",
        "GITHUB_APP_ID": "1",
        "GITHUB_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
        "GITHUB_WEBHOOK_SECRET": "x",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_repo_requesting_more_than_operator_cap_is_capped_down() -> None:
    repo_config = ReviewConfig(
        max_candidates=10_000, max_total_input_tokens=50_000_000, max_output_tokens_per_candidate=1_000_000,
        max_concurrent_requests=1_000, max_retries=1_000,
    )
    settings = _settings(
        PATCHFROG_MAX_REVIEW_CANDIDATES=50,
        PATCHFROG_MAX_TOTAL_INPUT_TOKENS=200_000,
        PATCHFROG_MAX_OUTPUT_TOKENS_PER_CANDIDATE=8_000,
        PATCHFROG_MAX_CONCURRENT_REVIEW_REQUESTS=8,
        PATCHFROG_MAX_REVIEW_RETRIES=3,
    )
    effective = apply_operator_hard_caps(repo_config, settings=settings)

    assert effective.max_candidates == 50
    assert effective.max_total_input_tokens == 200_000
    assert effective.max_output_tokens_per_candidate == 8_000
    assert effective.max_concurrent_requests == 8
    assert effective.max_retries == 3


def test_repo_requesting_less_than_operator_cap_is_honored_unchanged() -> None:
    """A repository may voluntarily ask for *less* than the operator
    allows -- that request is never overridden upward."""

    repo_config = ReviewConfig(
        max_candidates=5, max_total_input_tokens=1_000, max_output_tokens_per_candidate=512,
        max_concurrent_requests=1, max_retries=0,
    )
    settings = _settings(
        PATCHFROG_MAX_REVIEW_CANDIDATES=100,
        PATCHFROG_MAX_TOTAL_INPUT_TOKENS=1_000_000,
        PATCHFROG_MAX_OUTPUT_TOKENS_PER_CANDIDATE=16_000,
        PATCHFROG_MAX_CONCURRENT_REVIEW_REQUESTS=16,
        PATCHFROG_MAX_REVIEW_RETRIES=5,
    )
    effective = apply_operator_hard_caps(repo_config, settings=settings)

    assert effective.max_candidates == 5
    assert effective.max_total_input_tokens == 1_000
    assert effective.max_output_tokens_per_candidate == 512
    assert effective.max_concurrent_requests == 1
    assert effective.max_retries == 0


def test_default_operator_caps_preserve_default_repo_config_behavior() -> None:
    """An unconfigured self-hosted install (no PATCHFROG_MAX_* env vars
    set) must behave exactly as before this milestone for a repository
    using ReviewConfig's own (smaller) defaults."""

    effective = apply_operator_hard_caps(ReviewConfig(), settings=_settings())
    assert effective == ReviewConfig()


def test_operator_caps_are_never_read_from_repo_config_fields() -> None:
    """ReviewConfig has no operator-cap fields at all -- the trust
    boundary is structural, not just a runtime check."""

    assert not hasattr(ReviewConfig(), "operator_max_candidates")
    assert set(ReviewConfig.model_fields) == {
        "critic_enabled", "max_candidates", "max_input_tokens_per_candidate",
        "max_output_tokens_per_candidate", "max_total_input_tokens", "max_concurrent_requests",
        "min_final_confidence", "max_retries",
    }


def test_effective_config_fingerprint_differs_when_operator_cap_changes_behavior() -> None:
    """Spec section 29: canonical run identity must reflect *effective*
    behavior, not raw repository intent -- a repository asking for more
    than one operator allows must never be canonicalized as if it got
    what it asked for."""

    repo_config = ReviewConfig(max_candidates=10_000)
    lenient = apply_operator_hard_caps(repo_config, settings=_settings(PATCHFROG_MAX_REVIEW_CANDIDATES=10_000))
    strict = apply_operator_hard_caps(repo_config, settings=_settings(PATCHFROG_MAX_REVIEW_CANDIDATES=10))

    assert lenient.fingerprint() != strict.fingerprint()
    assert strict.max_candidates == 10


def test_positive_operator_cap_validation() -> None:
    for field in (
        "PATCHFROG_MAX_REVIEW_CANDIDATES",
        "PATCHFROG_MAX_TOTAL_INPUT_TOKENS",
        "PATCHFROG_MAX_OUTPUT_TOKENS_PER_CANDIDATE",
        "PATCHFROG_MAX_CONCURRENT_REVIEW_REQUESTS",
        "PATCHFROG_MAX_REVIEW_RETRIES",
    ):
        with pytest.raises(Exception):  # noqa: B017 -- pydantic ValidationError, any non-positive value
            _settings(**{field: 0})
