"""Cross-PR Intelligence Foundation.

Detects meaningful *structural* overlap between the current PR and
other concurrently-relevant PRs in the **same repository** -- e.g. both
PRs directly changing the exact same `(file_path, qualified_name)`
symbol. See ``validation/cross_pr_intelligence/latest-summary.md`` for
the full audit and ``docs/cross-pr-intelligence.md`` for the
user-facing behavior.
"""

from __future__ import annotations
