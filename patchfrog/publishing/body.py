"""Deterministic comment/summary body formatting.

Every body PatchFrog writes to GitHub is assembled here from Phase 5's
already-final finding content -- never a fresh LLM call (see the module
docstring of :mod:`patchfrog.publishing.service`: "publishing is a
deterministic side effect, not an AI operation"). Also owns GitHub's
comment-body size limit: truncation is deterministic and always recorded,
never allowed to fail an entire publication over one oversized message.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from patchfrog.analysis.domain import Severity
from patchfrog.publishing.domain import PublishableFinding
from patchfrog.publishing.marker import render_marker, sanitize_untrusted_text

# GitHub's actual limit is 65536 bytes for both review and comment bodies;
# staying comfortably under it (in characters, a conservative proxy for
# bytes) leaves room for the fixed scaffolding PatchFrog adds around
# AI-authored text.
MAX_COMMENT_BODY_CHARS = 60_000
MAX_SUMMARY_BODY_CHARS = 60_000

_TRUNCATION_SUFFIX = "\n\n*(truncated -- message exceeded PatchFrog's size limit)*"

_SEVERITY_ORDER = (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW, Severity.INFO)


def _truncate(text: str, *, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    keep = max(limit - len(_TRUNCATION_SUFFIX), 0)
    return text[:keep] + _TRUNCATION_SUFFIX, True


def format_inline_comment_body(finding: PublishableFinding) -> tuple[str, bool]:
    """Render one finding as a concise, actionable inline comment body.
    Returns ``(body, truncated)``."""

    message = sanitize_untrusted_text(finding.message.strip())
    reasoning = sanitize_untrusted_text(finding.reasoning_summary.strip())

    lines = [
        f"**{finding.severity.value.upper()} · {finding.category.value}** — {sanitize_untrusted_text(finding.title.strip())}",
        "",
        message,
    ]
    if reasoning and reasoning != message:
        lines += ["", "<details><summary>Why</summary>", "", reasoning, "", "</details>"]
    if finding.suggested_fix:
        lines += ["", f"**Suggested direction:** {sanitize_untrusted_text(finding.suggested_fix.strip())}"]

    body = "\n".join(lines)
    return _truncate(body, limit=MAX_COMMENT_BODY_CHARS)


def _finding_summary_line(finding: PublishableFinding) -> str:
    location = f"`{finding.file_path}:{finding.start_line}`"
    title = sanitize_untrusted_text(finding.title.strip())
    return f"- **{finding.severity.value.upper()}** {location} — {title}"


def format_summary_body(
    *,
    publication_id: UUID,
    counts_by_severity: Mapping[Severity, int],
    inline_findings: Sequence[PublishableFinding],
    summary_only_findings: Sequence[PublishableFinding],
    omitted_count: int,
) -> tuple[str, bool]:
    """Render the deterministic top-level review summary. Returns
    ``(body, truncated)``."""

    lines = ["## PatchFrog review summary", ""]

    severity_line = ", ".join(
        f"{counts_by_severity[s]} {s.value}" for s in _SEVERITY_ORDER if counts_by_severity.get(s, 0) > 0
    )
    lines.append(f"**Findings:** {severity_line or 'none'}")
    lines.append(
        f"**Published inline:** {len(inline_findings)} · "
        f"**Summary-only:** {len(summary_only_findings)} · "
        f"**Omitted:** {omitted_count}"
    )
    lines.append("")

    if inline_findings:
        lines.append("### Inline comments")
        for finding in inline_findings:
            lines.append(_finding_summary_line(finding))
        lines.append("")

    if summary_only_findings:
        lines.append("### Additional findings (not mappable to a diff line)")
        for finding in summary_only_findings:
            lines.append(_finding_summary_line(finding))
        lines.append("")

    if omitted_count:
        lines.append(f"*{omitted_count} additional finding(s) omitted -- see PatchFrog for the full report.*")
        lines.append("")

    marker = render_marker(publication_id)
    content = "\n".join(lines)
    # The marker must always survive truncation (see the module docstring
    # and patchfrog.publishing.marker) -- reserve room for it up front
    # rather than truncating the assembled body wholesale, which could
    # otherwise cut the marker off along with everything else.
    truncated_content, truncated = _truncate(content, limit=MAX_SUMMARY_BODY_CHARS - len(marker) - 2)
    body = f"{truncated_content}\n\n{marker}"
    return body, truncated
