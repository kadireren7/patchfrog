"""Local, database-free code graph (Milestone 1.3).

The neutral home of :func:`build_local_code_graph` -- the repository
index -> resolution -> graph pipeline without persistence -- shared by the
API-compatibility line (``patchfrog.upstream``) and the upcoming
local-first scalability engine. Public names are re-exported here; see
:mod:`patchfrog.codegraph.local_graph` for the implementation.
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
