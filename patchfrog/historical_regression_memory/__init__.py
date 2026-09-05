"""Historical Regression Memory Foundation -- deterministic detection of
when a current PR re-enters a repository's own, trusted historical
review risk.

See ``docs/historical-regression-memory.md`` and
``validation/historical_regression_memory/latest-summary.md`` for the
full design narrative. Extends :mod:`patchfrog.change_intelligence`,
:mod:`patchfrog.contract_intelligence`, :mod:`patchfrog.intent_verification`,
and :mod:`patchfrog.test_intelligence` -- and reuses Phase 9's own
:mod:`patchfrog.feedback` trust signals -- rather than building a
parallel history database.
"""

from __future__ import annotations
