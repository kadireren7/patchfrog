"""PatchFrog's machine-readable review marker.

Embedded once, at the very end of a published review's top-level summary
body, as an HTML comment (invisible in GitHub's rendered markdown):

    <!-- patchfrog:review:<publication-id> -->

``publication-id`` is the persisted ``review_publications.id`` row this
review corresponds to -- generated *before* the GitHub write is attempted
(see :mod:`patchfrog.publishing.service`), so it is stable across
retries and lets a crashed-mid-publish process recognize its own
in-flight review on GitHub during reconciliation (see
:func:`find_marker` and :mod:`patchfrog.publishing.reconciliation`).

Security: the AI-authored parts of a review body (finding messages,
reasoning, suggested fixes) must never be able to forge or corrupt this
marker -- see :func:`sanitize_untrusted_text`, applied to every piece of
LLM-originated text before it is interpolated into any body PatchFrog
constructs.
"""

from __future__ import annotations

import re
from uuid import UUID

_MARKER_PREFIX = "patchfrog:review:"
_MARKER_RE = re.compile(r"<!--\s*patchfrog:review:([0-9a-fA-F-]{36})\s*-->")

# Matches the marker's own delimiters/prefix in isolation, so untrusted
# text can never contribute a lookalike even without the exact UUID --
# defense in depth beyond just checking the fully-formed pattern above.
_MARKER_LOOKALIKE_RE = re.compile(r"<!--\s*patchfrog:review:.*?-->", re.DOTALL)


def render_marker(publication_id: UUID) -> str:
    return f"<!-- {_MARKER_PREFIX}{publication_id} -->"


def find_marker(body: str | None) -> UUID | None:
    """Extract the publication id from a review body, if present and
    well-formed. Returns ``None`` for a body with no marker or a
    malformed one -- never raises, since reconciliation must be able to
    safely scan arbitrary reviews (including ones PatchFrog never wrote)."""

    if not body:
        return None
    match = _MARKER_RE.search(body)
    if match is None:
        return None
    try:
        return UUID(match.group(1))
    except ValueError:
        return None


def sanitize_untrusted_text(text: str) -> str:
    """Strip any marker-shaped sequence from AI-originated text before it
    is included in a comment/summary body PatchFrog constructs.

    Without this, a prompt-injected or hallucinated finding message
    containing ``<!-- patchfrog:review:... -->`` could be mistaken for
    PatchFrog's own marker by a later reconciliation scan -- this
    function is what guarantees the only marker that can ever appear in
    a body PatchFrog writes is the one :func:`render_marker` adds itself,
    once, at the end of the top-level summary.
    """

    return _MARKER_LOOKALIKE_RE.sub("[redacted-marker-like-sequence]", text)
