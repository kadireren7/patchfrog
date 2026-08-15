"""Deterministic finding fingerprints.

A fingerprint identifies *one finding instance* (one analyzer, one rule,
one location) across repeated runs of the same code — not a cross-analyzer
identity (that's :mod:`patchfrog.analysis.dedupe`'s job; two different
analyzers reporting "the same" underlying issue get two different
fingerprints, then get grouped together, preserving both fingerprints as
provenance).

When a containing symbol is known (from Phase 2 enrichment), the
fingerprint anchors to the symbol's qualified name instead of the exact
line — a finding stays identifiable as "the same one" if unrelated code
shifts it a few lines within the same function, which a raw line number
alone could not survive. Without a containing symbol, the exact line is
the only anchor available.
"""

from __future__ import annotations

import hashlib

from patchfrog.analysis.domain import RawFinding


def compute_fingerprint(finding: RawFinding, *, symbol_qualified_name: str | None) -> str:
    """Compute a stable fingerprint for one raw finding.

    ``symbol_qualified_name`` is passed explicitly (rather than read off
    ``finding``, which has no such field) because it comes from Phase 2
    repository-intelligence enrichment, a step the finding itself doesn't
    know about.
    """

    location_anchor = symbol_qualified_name if symbol_qualified_name else f"line:{finding.span.start_line}"

    parts = (
        finding.category.value,
        finding.source_analyzer,
        finding.rule_id,
        finding.file_path,
        location_anchor,
    )
    canonical = "\x1f".join(parts)  # unit-separator: never appears in any of these fields
    return hashlib.sha256(canonical.encode()).hexdigest()
