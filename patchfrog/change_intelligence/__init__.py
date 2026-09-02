"""Change Intelligence Foundation.

Groups a review run's already-generated, diff-driven
:class:`~patchfrog.review.domain.ReviewCandidate` list into deterministic
logical :class:`~patchfrog.change_intelligence.domain.ChangeUnit`\\ s,
derives a bounded affected surface and expected/missing companion-change
candidates from the *existing* repository graph
(:mod:`patchfrog.intelligence.queries`), and produces a bounded, optional
Change Story/Change Map for the review summary.

Zero LLM calls anywhere in this package. See ``docs/change-intelligence.md``
for the full design and ``validation/change_intelligence/`` for the audit
and corpus results this milestone produced.
"""

from __future__ import annotations
