# Brand

Concise reference for PatchFrog's identity in a published GitHub review.
Not a style guide for marketing copy -- PatchFrog does not write
marketing copy into a review.

## Product name

**PatchFrog**

## Bot identity

`patchfrog[bot]` -- the real GitHub App's own login. Every published
comment/review already shows this as the author; PatchFrog's own body
text never repeats the name to compensate (see
[`patchfrog/publishing/body.py`](../patchfrog/publishing/body.py)).

## Inline marker

🐸 -- prefixed to each inline comment's severity/category header, and to
the review summary's heading. Nowhere else. Configurable per repository
(`.patchfrog.yml`'s `publish.frog_marker: false`), on by default for
hosted PatchFrog.

## Summary heading

`## 🐸 PatchFrog review`

The only place the product name appears in a published review body --
once, in the heading. Never repeated in the findings list, never
repeated per inline comment.

## Tone

- Technical
- Concise
- Specific
- Calm
- Non-marketing

## Explicitly avoided

- Large banners, multiple logos, repeated slogans
- "Powered by AI" / "AI-powered code review assistant" anywhere in a
  published body
- Labeling a finding "AI finding" / "LLM finding" -- a developer cares
  about the issue, not which internal component discovered it (static
  analyzer vs. AI reviewer)
- Raw numeric confidence ("Confidence: 82%") -- confidence is
  represented through wording only (see Security Review Quality)
- A verbose footer, or any text whose only purpose is brand reinforcement
