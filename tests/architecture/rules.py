"""Architecture rules R1-R4 (docs/architecture/BOUNDARIES.md section 2).

Each check takes a :class:`~tests.architecture.import_graph.ModuleGraph`
(and the baseline where relevant) and returns a sorted list of
human-actionable violation messages; an empty list means compliant. The
production tests and the mutation tests call the very same functions.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable

from tests.architecture.import_graph import (
    ALL_KINDS,
    Baseline,
    ImportKind,
    ModuleGraph,
    edge_key,
    import_closure,
    path_within,
    strongly_connected_components,
    unit_edges,
)

# --- R1: core closure purity -------------------------------------------------

#: Exactly the approved core roots (BOUNDARIES.md section 1/R1). A package
#: root covers every module inside that package.
CORE_ROOTS: tuple[str, ...] = (
    "patchfrog.analysis.subprocess_sandbox",
    "patchfrog.app",  # Milestones 2.1-2.3: local application layer (revision, store, jobs, discovery)
    "patchfrog.change_risk",
    "patchfrog.codegraph",  # Milestone 1.3 (M4): neutral home of the local code graph
    "patchfrog.config.core",
    "patchfrog.dependencies.files",
    "patchfrog.dependencies.manifests",
    "patchfrog.diff",
    "patchfrog.intelligence.graph",
    "patchfrog.intelligence.resolution",
    "patchfrog.intelligence.tests",
    "patchfrog.parsing.registry",
    "patchfrog.repository.git",
    "patchfrog.scalability",  # Milestone 2.3: scalability engine (application discovery)
    "patchfrog.upstream.code_graph",
)

FORBIDDEN_INTERNAL_PREFIXES: tuple[str, ...] = (
    "apps",
    "patchfrog.cli",  # also matches patchfrog.cli_changes / cli_campaigns
    "patchfrog.config.settings",
    "patchfrog.feedback",
    "patchfrog.github",
    "patchfrog.mcp",
    "patchfrog.ops",
    "patchfrog.persistence",
    "patchfrog.publishing",
    "patchfrog.review",  # also matches review_memory: not a core dependency either
    "patchfrog.services",
)

FORBIDDEN_EXTERNAL: frozenset[str] = frozenset(
    {
        "alembic", "anthropic", "asyncpg", "celery", "fastapi", "google", "openai",
        "prometheus_client", "redis", "semgrep", "sqlalchemy", "uvicorn",
    }
)

#: Third-party libraries a core closure may use (anything else needs an ADR).
ALLOWED_EXTERNAL: frozenset[str] = frozenset({"pydantic", "pydantic_settings", "structlog", "yaml"})
ALLOWED_EXTERNAL_PREFIXES: tuple[str, ...] = ("tree_sitter",)


def _is_forbidden_internal(module: str) -> bool:
    return any(module == p or module.startswith(p + ".") or module.startswith(p + "_") for p in FORBIDDEN_INTERNAL_PREFIXES)


def _expand_roots(graph: ModuleGraph, root: str) -> list[str]:
    if root in graph.packages:
        return sorted(m for m in graph.paths if m == root or m.startswith(root + "."))
    return [root] if root in graph.paths else []


def check_core_purity(graph: ModuleGraph, roots: Iterable[str] = CORE_ROOTS) -> list[str]:
    violations: list[str] = []
    for root in sorted(set(roots)):
        expanded = _expand_roots(graph, root)
        if not expanded:
            violations.append(f"R1: core root {root} does not exist (update CORE_ROOTS and BOUNDARIES.md together)")
            continue
        internal, external = import_closure(graph, expanded, ALL_KINDS)
        # Report only the frontier -- the first step into forbidden
        # territory -- not everything reachable beyond it.
        for module in sorted(internal):
            via = internal[module].via
            if _is_forbidden_internal(module) and not (via is not None and _is_forbidden_internal(via)):
                chain = "\n      -> ".join(internal[module].chain)
                violations.append(f"R1: core root {root} reaches forbidden module {module}\n      {chain}")
        for library in sorted(external):
            via = external[library].via
            if via is not None and _is_forbidden_internal(via):
                continue  # already reported at the forbidden module that pulls it in
            if library in sys.stdlib_module_names or library in ALLOWED_EXTERNAL:
                continue
            if library.startswith(ALLOWED_EXTERNAL_PREFIXES):
                continue
            label = "forbidden" if library in FORBIDDEN_EXTERNAL else "non-allowlisted (needs an ADR)"
            chain = "\n      -> ".join(external[library].chain)
            violations.append(f"R1: core root {root} reaches {label} library {library}\n      {chain}")
    return violations


# --- R2 + R3: cycles against the baseline ----------------------------------------


def check_cycles(graph: ModuleGraph, baseline: Baseline) -> list[str]:
    """R2 (no growth) and R3 (shrink-only baseline), on the normalized
    unit graph: the set of non-trivial SCCs and their intra-SCC edges."""

    edges = unit_edges(graph, ALL_KINDS)
    current = strongly_connected_components(edges)
    historical = [frozenset(c["members"]) for c in baseline["historical_sccs"]]
    allowed_edges: dict[str, str] = {}
    for recorded in baseline["historical_sccs"]:
        allowed_edges.update(recorded["edges"])
    historical_members = frozenset().union(*historical) if historical else frozenset()

    violations: list[str] = []
    current_intra: dict[str, tuple[str, str]] = {}
    for component in current:
        for (source, target) in edges:
            if source in component and target in component:
                current_intra[edge_key(source, target)] = (source, target)

        if not any(component <= old for old in historical):
            joined = sorted(component - historical_members)
            # Which edge is "the new one" is unknowable from a single
            # snapshot, so list every cycle edge touching the new units,
            # each with its import location (one of them is the culprit).
            involved = [
                e for e in sorted(edges)
                if e[0] in component and e[1] in component and (not joined or e[0] in joined or e[1] in joined)
            ]
            lines = [f"\n      edge: {edges[e].where(graph)}" for e in involved[:20]]
            if len(involved) > 20:
                lines.append(f"\n      ... and {len(involved) - 20} more")
            example = path_within(edges, involved[0][1], involved[0][0], component) if involved else []
            detail = "".join(lines) + (f"\n      example cycle: {involved[0][0]} -> {' -> '.join(example)}" if example else "")
            if not joined:
                violations.append(f"R2: separate historical SCCs merged into one: {sorted(component)}{detail}")
            elif component & historical_members:
                violations.append(f"R2: unit(s) {joined} joined the historical SCC{detail}")
            else:
                violations.append(f"R2: new dependency cycle among {sorted(component)}{detail}")

    for key, (source, target) in sorted(current_intra.items()):
        if key not in allowed_edges and any(source in old and target in old for old in historical):
            violations.append(
                f"R2: new edge inside the historical SCC: {key}\n      {edges[(source, target)].where(graph)}"
                "\n      (add it to the baseline only with a reviewed justification, or remove the import)"
            )

    for key, reason in sorted(allowed_edges.items()):
        if not reason.strip() or reason.strip().upper().startswith("TODO"):
            violations.append(f"R2: baseline edge {key} has no reviewed justification ({reason!r})")
        if key not in current_intra:
            violations.append(
                f"R3: baseline edge {key} is no longer part of a dependency cycle -- debt was removed; "
                "delete it from tests/architecture/dependency_baseline.json (shrink-only baseline)"
            )
    current_members = frozenset().union(*current) if current else frozenset()
    for member in sorted(historical_members - current_members):
        violations.append(
            f"R3: {member} is no longer in any dependency cycle -- remove it from the baseline members"
        )
    return sorted(set(violations))


# --- R4: adapter direction ----------------------------------------------------------


def check_apps_imports(graph: ModuleGraph, baseline: Baseline) -> list[str]:
    allowed = {
        (e["importer"], e["function"], e["target"]): e["reason"] for e in baseline["apps_lazy_import_exceptions"]
    }
    seen: set[tuple[str, str, str]] = set()
    violations: list[str] = []
    for edge in graph.edges:
        if not (edge.internal and edge.importer.startswith("patchfrog") and edge.target.startswith("apps.")):
            continue
        if edge.kind is not ImportKind.LAZY:
            violations.append(
                f"R4: library module imports {edge.target} at {edge.kind.value} level -- "
                f"{edge.where(graph)}\n      (move it into the function that needs it, or into apps/)"
            )
            continue
        key = (edge.importer, edge.function or "", edge.target)
        seen.add(key)
        if key not in allowed:
            violations.append(
                f"R4: new function-local apps.* import needs architectural review -- {edge.where(graph)}"
            )
    for key, reason in sorted(allowed.items()):
        if not reason.strip() or reason.strip().upper().startswith("TODO"):
            violations.append(f"R4: exception {key} has no reviewed justification")
        if key not in seen:
            violations.append(
                f"R4: baseline exception {key[0]}:{key[1]}() -> {key[2]} no longer exists -- remove it from "
                "tests/architecture/dependency_baseline.json"
            )
    return sorted(set(violations))
