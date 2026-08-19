"""Stable finding fingerprints for publication reconciliation.

Deliberately never the database auto-increment id -- a fingerprint must
stay the same across process restarts, across DB-row loss/recovery, and
be independently recomputable from a finding's own semantic content. Used
both for the DB-level uniqueness guard (one comment per fingerprint per
publication -- see :mod:`patchfrog.persistence.models.publishing`) and,
were a future phase to compare fingerprints across review runs, as the
one stable handle for "the same finding" (explicitly out of scope for
Phase 6 -- see :mod:`patchfrog.publishing.service`'s module docstring).
"""

from __future__ import annotations

import hashlib
from uuid import UUID

from patchfrog.publishing.domain import PublishableFinding


def compute_finding_fingerprint(
    *,
    repository_id: UUID,
    pull_request_number: int,
    head_sha: str,
    finding: PublishableFinding,
) -> str:
    """Derive a stable fingerprint from semantic identity only: repository,
    PR, head SHA, path, line range, category, and a whitespace-normalized
    message -- never the finding's own database id."""

    normalized_message = " ".join(finding.message.split()).strip().lower()
    parts = (
        str(repository_id),
        str(pull_request_number),
        head_sha,
        finding.file_path,
        str(finding.start_line),
        str(finding.end_line),
        finding.category.value,
        normalized_message,
    )
    canonical = "\x1f".join(parts)
    return hashlib.sha256(canonical.encode()).hexdigest()
