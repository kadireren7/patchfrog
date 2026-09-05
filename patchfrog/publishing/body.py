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

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.publishing.domain import PublishableFinding
from patchfrog.publishing.marker import render_marker, sanitize_untrusted_text

#: PatchFrog's subtle visual marker -- see docs/brand.md. Appears once in
#: the summary heading and once per inline comment header, never repeated
#: in body prose (GitHub already shows `patchfrog[bot]` as the comment
#: author). Configurable via `PublicationConfig.frog_marker` (default
#: on); every function here takes it as an explicit parameter rather than
#: reading config directly, keeping this module free of any config
#: dependency.
FROG_MARKER = "🐸"

#: A deterministic, minimal wording qualifier for anything below HIGH
#: confidence (Phase 8 spec sections 6/15: never present a non-HIGH
#: finding as a confirmed vulnerability, but never numeric confidence to
#: a GitHub user either -- one short parenthetical, not hedging scattered
#: through the prose).
_CONFIDENCE_QUALIFIER: dict[Confidence, str] = {
    Confidence.MEDIUM: "medium confidence, verify before treating as confirmed",
    Confidence.LOW: "low confidence, needs verification",
}

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


def _header_line(finding: PublishableFinding, *, frog_marker: bool) -> str:
    # UNKNOWN means the category taxonomy genuinely doesn't fit (see
    # patchfrog.analysis.domain.FindingCategory's own docstring: "use
    # UNKNOWN rather than forcing a fake fit") -- showing it to a reader
    # ("· unknown") would be noise, not signal, so it's the one category
    # value this header omits rather than prints.
    if finding.category is FindingCategory.UNKNOWN:
        header = f"**{finding.severity.value.upper()}**"
    else:
        header = f"**{finding.severity.value.upper()} · {finding.category.value}**"
    if frog_marker:
        header = f"{FROG_MARKER} {header}"
    qualifier = _CONFIDENCE_QUALIFIER.get(finding.confidence)
    if qualifier is not None:
        header += f" ({qualifier})"
    # finding.title is model-generated structured output -- the schema
    # doesn't forbid an embedded newline/tab, and .split()/" ".join()
    # (not just .strip()) is what actually guarantees the header stays
    # exactly one line no matter what whitespace the model emits.
    single_line_title = " ".join(finding.title.split())
    return f"{header} — {sanitize_untrusted_text(single_line_title)}"


def format_inline_comment_body(finding: PublishableFinding, *, frog_marker: bool = True) -> tuple[str, bool]:
    """Render one finding as the shortest useful, technically justified
    comment. Returns ``(body, truncated)``.

    Deliberately never prints a mechanical Identification:/Reason:/
    Impact:/Solution: section list (Phase 8 spec section 13) -- message
    (identification), reasoning_summary (root cause), impact, and
    suggested_fix (solution) are folded into one flowing paragraph,
    skipping any field that's empty or that merely repeats a sentence
    already included. A finding with only ``message`` (the common case
    for a simple, obvious bug) renders as a single short sentence; nothing
    here artificially pads a comment to "look complete."

    ``frog_marker`` (default on, see :data:`FROG_MARKER`) is the only
    branding this function ever adds -- never "PatchFrog", never "AI
    finding"/"LLM finding" (see docs/brand.md), since GitHub already
    shows `patchfrog[bot]` as the comment's author.
    """

    message = sanitize_untrusted_text(finding.message.strip())
    reasoning = sanitize_untrusted_text(finding.reasoning_summary.strip()) if finding.reasoning_summary else ""
    impact = sanitize_untrusted_text(finding.impact.strip()) if finding.impact else ""
    fix = sanitize_untrusted_text(finding.suggested_fix.strip()) if finding.suggested_fix else ""

    sentences: list[str] = [message]
    for extra in (reasoning, impact, fix):
        if extra and extra not in sentences:
            sentences.append(extra)
    paragraph = " ".join(sentences)

    lines = [_header_line(finding, frog_marker=frog_marker), "", paragraph]
    body = "\n".join(lines)
    return _truncate(body, limit=MAX_COMMENT_BODY_CHARS)


def _finding_summary_line(finding: PublishableFinding) -> str:
    location = f"`{finding.file_path}:{finding.start_line}`"
    title = sanitize_untrusted_text(" ".join(finding.title.split()))
    return f"- **{finding.severity.value.upper()}** {location} — {title}"


def format_summary_body(
    *,
    publication_id: UUID,
    counts_by_severity: Mapping[Severity, int],
    inline_findings: Sequence[PublishableFinding],
    summary_only_findings: Sequence[PublishableFinding],
    omitted_count: int,
    frog_marker: bool = True,
    change_story: str | None = None,
    change_map_text: str | None = None,
    intent_coverage_summary: str | None = None,
    test_coverage_summary: str | None = None,
    historical_context_summary: str | None = None,
) -> tuple[str, bool]:
    """Render the deterministic top-level review summary. Returns
    ``(body, truncated)``.

    ``frog_marker`` (default on, see :data:`FROG_MARKER`) controls only
    the heading's emoji -- "PatchFrog" itself always appears exactly
    once here (the one place this module names the product), never
    repeated elsewhere in the body, and never with marketing copy
    alongside it (see docs/brand.md).

    ``change_story``/``change_map_text`` (Change Intelligence Foundation,
    :mod:`patchfrog.change_intelligence`) are already-bounded, already-
    deterministic text -- this function never generates or alters them,
    only places them per the suggested order (spec section 16: Change
    story, Change map, then findings). Both are ``None`` for a run that
    predates this milestone or produced neither; this whole block is
    then simply omitted, so the body is byte-identical to before.
    ``intent_coverage_summary`` (Intent Verification Foundation,
    :mod:`patchfrog.intent_verification`) is the same kind of already-
    bounded, already-deterministic text, placed after the Change Map
    (spec section 22) -- ``None`` unless
    :func:`patchfrog.intent_verification.summary.should_render_intent_coverage_summary`
    determined it was eligible. ``test_coverage_summary`` (Test
    Intelligence Foundation, :mod:`patchfrog.test_intelligence`) is the
    same kind of already-bounded, already-deterministic text, placed
    after the Intent Coverage block -- ``None`` unless
    :func:`patchfrog.test_intelligence.summary.should_render_test_gap_summary`
    determined it was eligible. ``historical_context_summary``
    (Historical Regression Memory Foundation,
    :mod:`patchfrog.historical_regression_memory`) is the same kind of
    already-bounded, already-deterministic text, placed after the Test
    Impact block -- ``None`` unless
    :func:`patchfrog.historical_regression_memory.summary.should_render_historical_summary`
    determined it was eligible. Repository Learnings Foundation
    (:mod:`patchfrog.repository_learnings`) has no separate summary
    block of its own in v1 -- it only ever enriches an existing
    Historical Regression Memory candidate, so a second, standalone
    section here would duplicate the block above for the exact same
    surface (see that package's own ``__init__.py`` docstring); its
    only user-facing footprint is a bounded addendum already folded
    into ``change_story`` above. Never used on the *clean*-review path
    (:func:`format_clean_review_body`) -- see that function's own
    docstring for why."""

    heading = f"## {FROG_MARKER} PatchFrog review" if frog_marker else "## PatchFrog review"
    lines = [heading, ""]

    if change_story:
        lines.append(sanitize_untrusted_text(change_story.strip()))
        lines.append("")

    if change_map_text:
        lines.append(sanitize_untrusted_text(change_map_text.strip()))
        lines.append("")

    if intent_coverage_summary:
        lines.append(sanitize_untrusted_text(intent_coverage_summary.strip()))
        lines.append("")

    if test_coverage_summary:
        lines.append(sanitize_untrusted_text(test_coverage_summary.strip()))
        lines.append("")

    if historical_context_summary:
        lines.append(sanitize_untrusted_text(historical_context_summary.strip()))
        lines.append("")

    severity_line = " · ".join(
        f"{counts_by_severity[s]} {s.value}" for s in _SEVERITY_ORDER if counts_by_severity.get(s, 0) > 0
    )
    lines.append(f"**Findings:** {severity_line or 'none'}")
    counts_line = f"**Published inline:** {len(inline_findings)} · **Summary-only:** {len(summary_only_findings)}"
    if omitted_count:
        counts_line += f" · **Omitted:** {omitted_count}"
    lines.append(counts_line)
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


def format_clean_review_body(*, publication_id: UUID, frog_marker: bool = True) -> str:
    """The one-line review posted when Phase 5 genuinely produced zero
    findings (``PublicationConfig.post_clean_summary``, off by default --
    see that field's docstring). Deliberately never "No issues exist." --
    PatchFrog only ever reports what its own review process did, never a
    correctness guarantee about the code (external beta readiness: a
    clean PR must not look like PatchFrog silently failed, but it also
    must never overclaim)."""

    heading = f"## {FROG_MARKER} PatchFrog review" if frog_marker else "## PatchFrog review"
    lines = [
        heading,
        "",
        "PatchFrog found no publishable findings in this review.",
        "",
        render_marker(publication_id),
    ]
    return "\n".join(lines)
