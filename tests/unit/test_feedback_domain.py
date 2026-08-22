"""Unit coverage for :mod:`patchfrog.feedback.domain` -- reaction
normalization is the only non-trivial pure logic here; everything else is
enums/dataclasses."""

from __future__ import annotations

from patchfrog.domain.github_feedback import GitHubReactionContent
from patchfrog.feedback.domain import NormalizedReactionHint, normalize_reaction


def test_plus_one_and_heart_and_hooray_are_positive_hints() -> None:
    for content in (GitHubReactionContent.PLUS_ONE, GitHubReactionContent.HEART, GitHubReactionContent.HOORAY):
        assert normalize_reaction(content) is NormalizedReactionHint.POSITIVE_HINT


def test_minus_one_and_confused_are_negative_hints() -> None:
    for content in (GitHubReactionContent.MINUS_ONE, GitHubReactionContent.CONFUSED):
        assert normalize_reaction(content) is NormalizedReactionHint.NEGATIVE_HINT


def test_eyes_is_neutral_attention() -> None:
    assert normalize_reaction(GitHubReactionContent.EYES) is NormalizedReactionHint.NEUTRAL_ATTENTION


def test_laugh_and_rocket_are_never_interpreted_as_positive_or_negative() -> None:
    for content in (GitHubReactionContent.LAUGH, GitHubReactionContent.ROCKET):
        hint = normalize_reaction(content)
        assert hint is NormalizedReactionHint.UNINTERPRETED
        assert hint not in (NormalizedReactionHint.POSITIVE_HINT, NormalizedReactionHint.NEGATIVE_HINT)


def test_every_reaction_content_has_a_mapped_hint() -> None:
    for content in GitHubReactionContent:
        assert normalize_reaction(content) is not None
