"""Cross-Repo Intelligence Foundation.

Detects proven dependency/contract evidence across repository
boundaries: does the current PR change a contract that another
*explicitly-linked* repository depends on? PatchFrog never discovers
cross-repository relationships from similarity -- Cross-Repo
Intelligence runs only across repositories whose technical relationship
has been explicitly established through a trusted (operator-only)
source. See ``validation/cross_repo_intelligence/latest-summary.md``
for the full audit and ``docs/cross-repo-intelligence.md`` for the
user-facing behavior.
"""

from __future__ import annotations
