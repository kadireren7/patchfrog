"""Deterministic attribution: mapping a raw GitHub review comment back to
the exact :class:`~patchfrog.persistence.models.publishing.ReviewPublicationCommentModel`
(and, through it, the exact finding) it belongs to.

Fallback order (Phase 9 spec section 9):

1. ``github_comment_id`` already persisted on the publication comment
   (the common case once :func:`match_comments_to_publication` has run
   at least once for a publication -- see :mod:`patchfrog.feedback.sync`).
2. Deterministic (path, line, side, body-hash) matching against
   PatchFrog's own top-level comments for that publication -- this *is*
   the ``github_comment_id`` enrichment path (Phase 9 spec section 10),
   run on demand during sync rather than eagerly at publish time, so
   historical rows missing ``github_comment_id`` self-heal the next time
   sync runs against their PR (section 40).
3. Ambiguous -> no attribution. Never guessed, never fuzzy-matched on
   comment prose (explicitly forbidden by section 9).
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass

from patchfrog.domain.github_feedback import GitHubReviewComment

#: ``ReviewPublicationCommentModel.side`` stores PatchFrog's own internal
#: :class:`patchfrog.publishing.domain.DiffSide` vocabulary (``"old"``/
#: ``"new"``), never GitHub's wire vocabulary (``"LEFT"``/``"RIGHT"``,
#: :class:`patchfrog.domain.github_review.GitHubDiffSide`) -- the
#: translation happens only at the transport boundary when publishing
#: (mirrors ``patchfrog.publishing.service._DIFF_SIDE_TO_GITHUB`` exactly).
#: A real :class:`GitHubReviewComment` fetched back from the API always
#: carries the GitHub vocabulary, so candidate keys must be translated
#: before comparison -- comparing "new" against "RIGHT" directly would
#: never match anything.
_INTERNAL_SIDE_TO_GITHUB: dict[str, str] = {"old": "LEFT", "new": "RIGHT"}


def _body_hash(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def _github_side(internal_side: str | None) -> str | None:
    if internal_side is None:
        return None
    return _INTERNAL_SIDE_TO_GITHUB.get(internal_side, internal_side)


@dataclass(frozen=True, slots=True)
class PublicationCommentKey:
    """The identity a real GitHub comment must match to be attributed to
    one :class:`~patchfrog.persistence.models.publishing.ReviewPublicationCommentModel`
    row -- (path, line, side, body_hash), exactly what was sent to
    GitHub at publish time."""

    id: object  # uuid.UUID, kept loosely typed to avoid a hard uuid import cycle
    path: str
    line: int | None
    #: PatchFrog's internal ``DiffSide`` vocabulary ("old"/"new") --
    #: exactly what ``ReviewPublicationCommentModel.side`` stores.
    #: Translated to GitHub's wire vocabulary internally before matching.
    side: str | None
    body_hash: str | None


def match_comments_to_publication(
    *, github_comments: list[GitHubReviewComment], candidates: list[PublicationCommentKey]
) -> dict[object, int]:
    """Returns ``{review_publication_comment_id: github_comment_id}`` for
    every candidate that matches exactly one top-level (non-reply)
    GitHub comment on ``(path, line, side, body_hash)``. A candidate or a
    GitHub comment that matches more than one counterpart on the other
    side is excluded entirely from the result -- fail closed on
    ambiguity, never guess (Phase 9 spec section 10)."""

    # Only PatchFrog's own comments are ever top-level (a reply always
    # has in_reply_to_id set) -- restrict candidates to those before
    # keying, so a developer reply whose body happens to collide with a
    # finding's body_hash can never be mismatched onto the wrong finding.
    top_level = [c for c in github_comments if c.in_reply_to_id is None]

    github_by_key: dict[tuple[str, int | None, str | None, str], list[int]] = defaultdict(list)
    for c in top_level:
        key = (c.path, c.line if c.line is not None else c.original_line, c.side, _body_hash(c.body))
        github_by_key[key].append(c.id)

    candidates_by_key: dict[tuple[str, int | None, str | None, str], list[object]] = defaultdict(list)
    for cand in candidates:
        if cand.body_hash is None:
            continue
        key = (cand.path, cand.line, _github_side(cand.side), cand.body_hash)
        candidates_by_key[key].append(cand.id)

    result: dict[object, int] = {}
    for key, candidate_ids in candidates_by_key.items():
        if len(candidate_ids) != 1:
            continue  # two publication comments share an identical key -- ambiguous, skip both
        github_ids = github_by_key.get(key, [])
        if len(github_ids) != 1:
            continue  # zero or multiple GitHub comments share this key -- ambiguous or not yet posted
        result[candidate_ids[0]] = github_ids[0]

    return result
