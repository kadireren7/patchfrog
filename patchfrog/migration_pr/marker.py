"""PatchFrog's machine-readable migration-PR marker.

Embedded once, at the end of a migration PR's body, as an HTML comment
(invisible in GitHub's rendered markdown):

    <!-- patchfrog:migration:<identity-key> -->

``identity-key`` is :meth:`patchfrog.migration_pr.domain.MigrationPRLinkage.identity_key`
-- deterministic (repository + upstream change fingerprint + engine
version), unlike :mod:`patchfrog.publishing.marker`'s random per-attempt
UUID, because migration-PR idempotency is itself about *reusing* the same
PR across regenerated patches for the same upstream change (M9.6), not
about disambiguating concurrent attempts at one already-known row.
"""

from __future__ import annotations

import re

_MARKER_PREFIX = "patchfrog:migration:"
_MARKER_RE = re.compile(r"<!--\s*patchfrog:migration:([0-9a-f]{64})\s*-->")
_MARKER_LOOKALIKE_RE = re.compile(r"<!--\s*patchfrog:migration:.*?-->", re.DOTALL)


def render_marker(identity_key: str) -> str:
    return f"<!-- {_MARKER_PREFIX}{identity_key} -->"


def find_marker(body: str | None) -> str | None:
    """Extract the identity key from a PR body, if present and
    well-formed. Never raises -- a body with no marker, or one PatchFrog
    never wrote, is simply not a match."""

    if not body:
        return None
    match = _MARKER_RE.search(body)
    return match.group(1) if match else None


def sanitize_untrusted_text(text: str) -> str:
    """Strip any marker-shaped sequence from evidence-derived text before
    interpolation into a PR body -- defense in depth, mirroring
    :func:`patchfrog.publishing.marker.sanitize_untrusted_text`, even
    though dossier content here is deterministic evidence, not free-form
    AI output."""

    return _MARKER_LOOKALIKE_RE.sub("[redacted-marker-like-sequence]", text)


__all__ = ["find_marker", "render_marker", "sanitize_untrusted_text"]
