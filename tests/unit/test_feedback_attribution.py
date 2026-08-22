"""Unit coverage for :mod:`patchfrog.feedback.attribution` -- the
deterministic ``github_comment_id`` enrichment matcher. Every ambiguous
case must fail closed (no attribution), never guess."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from patchfrog.domain.github_feedback import GitHubActor, GitHubActorType, GitHubReviewComment
from patchfrog.feedback.attribution import PublicationCommentKey, match_comments_to_publication

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_USER = GitHubActor(login="patchfrog[bot]", actor_type=GitHubActorType.BOT)


def _comment(id_: int, *, path: str, line: int, side: str, body: str, in_reply_to_id: int | None = None) -> GitHubReviewComment:
    return GitHubReviewComment(
        id=id_,
        path=path,
        line=line,
        original_line=line,
        side=side,
        body=body,
        actor=_USER,
        in_reply_to_id=in_reply_to_id,
        pull_request_review_id=999,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _key(path: str, line: int, side: str, body: str) -> PublicationCommentKey:
    import hashlib

    return PublicationCommentKey(id=uuid.uuid4(), path=path, line=line, side=side, body_hash=hashlib.sha256(body.encode()).hexdigest())


def test_internal_diff_side_vocabulary_is_translated_to_github_vocabulary() -> None:
    """ReviewPublicationCommentModel.side stores "old"/"new" (PatchFrog's
    internal DiffSide), never GitHub's "LEFT"/"RIGHT" -- a real comment
    fetched back from the API always carries the GitHub vocabulary, so
    the match must translate before comparing."""

    gh = [_comment(101, path="a.py", line=10, side="RIGHT", body="finding body")]
    key = _key("a.py", 10, "new", "finding body")
    result = match_comments_to_publication(github_comments=gh, candidates=[key])
    assert result == {key.id: 101}


def test_unique_match_is_attributed() -> None:
    gh = [_comment(101, path="a.py", line=10, side="RIGHT", body="finding body")]
    key = _key("a.py", 10, "RIGHT", "finding body")
    result = match_comments_to_publication(github_comments=gh, candidates=[key])
    assert result == {key.id: 101}


def test_reply_comments_are_never_matched_as_top_level() -> None:
    gh = [_comment(101, path="a.py", line=10, side="RIGHT", body="finding body", in_reply_to_id=999)]
    key = _key("a.py", 10, "RIGHT", "finding body")
    result = match_comments_to_publication(github_comments=gh, candidates=[key])
    assert result == {}


def test_two_publication_comments_sharing_the_same_key_are_both_excluded() -> None:
    gh = [_comment(101, path="a.py", line=10, side="RIGHT", body="same body")]
    key_a = _key("a.py", 10, "RIGHT", "same body")
    key_b = _key("a.py", 10, "RIGHT", "same body")
    result = match_comments_to_publication(github_comments=gh, candidates=[key_a, key_b])
    assert result == {}


def test_two_github_comments_sharing_the_same_key_leave_it_unmatched() -> None:
    gh = [
        _comment(101, path="a.py", line=10, side="RIGHT", body="same body"),
        _comment(102, path="a.py", line=10, side="RIGHT", body="same body"),
    ]
    key = _key("a.py", 10, "RIGHT", "same body")
    result = match_comments_to_publication(github_comments=gh, candidates=[key])
    assert result == {}


def test_different_findings_on_different_lines_all_match_independently() -> None:
    gh = [
        _comment(101, path="a.py", line=10, side="RIGHT", body="finding one"),
        _comment(102, path="a.py", line=20, side="RIGHT", body="finding two"),
    ]
    key1 = _key("a.py", 10, "RIGHT", "finding one")
    key2 = _key("a.py", 20, "RIGHT", "finding two")
    result = match_comments_to_publication(github_comments=gh, candidates=[key1, key2])
    assert result == {key1.id: 101, key2.id: 102}


def test_candidate_with_no_body_hash_is_never_matched() -> None:
    gh = [_comment(101, path="a.py", line=10, side="RIGHT", body="finding body")]
    key = PublicationCommentKey(id=uuid.uuid4(), path="a.py", line=10, side="RIGHT", body_hash=None)
    result = match_comments_to_publication(github_comments=gh, candidates=[key])
    assert result == {}


def test_no_matching_github_comment_leaves_candidate_unattributed() -> None:
    key = _key("a.py", 10, "RIGHT", "finding body")
    result = match_comments_to_publication(github_comments=[], candidates=[key])
    assert result == {}
