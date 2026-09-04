"""Test Intelligence Foundation -- deterministic, evidence-based detection
of when changed behavior lacks the correct behavioral test surface.

See ``docs/test-intelligence.md`` and
``validation/test_intelligence/latest-summary.md`` for the full design
narrative. Extends :mod:`patchfrog.change_intelligence`,
:mod:`patchfrog.contract_intelligence`, and
:mod:`patchfrog.intent_verification` rather than building a fourth
parallel graph/candidate stack -- see this package's ``domain.py`` for
the exact reuse/dedup rules.
"""

from __future__ import annotations
