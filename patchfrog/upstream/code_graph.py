"""Backward-compatible re-export of :mod:`patchfrog.codegraph`.

The local code graph moved to the neutral :mod:`patchfrog.codegraph`
package in Milestone 1.3 (MIGRATION_PLAN M4). This module keeps the old
import path working for existing callers and downstream integrations; the
objects are the same objects (no copies, no wrappers).

Deprecated: import from :mod:`patchfrog.codegraph` in new code. Kept for at
least one minor release (MIGRATION_PLAN guarantee 4); removal requires the
ADR-015 compatibility check.
"""

from __future__ import annotations

from patchfrog.codegraph.local_graph import (
    EMPTY_GRAPH,
    MAX_GRAPH_FILE_BYTES,
    LocalCodeGraph,
    SymbolRef,
    build_local_code_graph,
)

__all__ = ["EMPTY_GRAPH", "MAX_GRAPH_FILE_BYTES", "LocalCodeGraph", "SymbolRef", "build_local_code_graph"]
