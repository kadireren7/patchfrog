"""Context Engine configuration and identity.

Deliberately small and explicit -- every knob here is a documented,
deterministic number, never a learned or inferred weight. See
:mod:`patchfrog.context.scoring` for the (also explicit) ranking weights,
which are code constants rather than configuration -- changing *how*
scoring works is an engine-version change (see
:data:`CONTEXT_ENGINE_VERSION`), not a per-run config choice.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from patchfrog.context.domain import ContextItemKind

#: Bumped whenever candidate generation, scoring, budgeting, or dedup
#: logic changes materially enough that a prior bundle's selection can no
#: longer be trusted as equivalent to what regenerating now would
#: produce -- folded into :meth:`ContextConfig.fingerprint` so a version
#: bump alone is enough to invalidate stale canonical bundles, exactly
#: like :data:`patchfrog.analysis.toolchain.ANALYSIS_ENGINE_VERSION`.
CONTEXT_ENGINE_VERSION = 1

DEFAULT_ENABLED_RELATIONSHIP_KINDS: tuple[ContextItemKind, ...] = (
    ContextItemKind.TARGET_SYMBOL,
    ContextItemKind.TARGET_FILE_REGION,
    ContextItemKind.CALLER,
    ContextItemKind.CALLEE,
    ContextItemKind.IMPORTED_DEPENDENCY,
    ContextItemKind.INCLUDED_HEADER,
    ContextItemKind.RELATED_TEST,
    ContextItemKind.PARENT_SYMBOL,
    ContextItemKind.SIBLING_SYMBOL,
)


@dataclass(frozen=True, slots=True)
class ContextConfig:
    """Effective configuration for one context-generation run."""

    max_tokens: int = 4000
    max_lines: int = 400
    max_tokens_per_item: int = 800
    max_lines_per_item: int = 120
    #: Fraction of the budget reserved for the target itself before any
    #: other candidate competes for space -- see
    #: :mod:`patchfrog.context.budgeting`.
    target_reservation_fraction: float = 0.35
    #: Maximum graph hops from the target considered at all. 1 = direct
    #: callers/callees/tests/imports only; 2 additionally considers
    #: callers-of-callers / callees-of-callees, subject to budget.
    graph_depth: int = 1
    #: Diversity cap: at most this many selected items per relationship
    #: kind, so budget isn't consumed entirely by e.g. five callers.
    max_items_per_relationship: int = 3
    enabled_kinds: tuple[ContextItemKind, ...] = field(
        default_factory=lambda: DEFAULT_ENABLED_RELATIONSHIP_KINDS
    )

    def wants(self, kind: ContextItemKind) -> bool:
        return kind in self.enabled_kinds

    def fingerprint(self) -> str:
        """Deterministic identity for this configuration *and* the engine
        version producing output from it -- two runs share this
        fingerprint only if both the requested configuration and the
        engine's behavior are identical (see module docstring)."""

        payload = {
            "engine_version": CONTEXT_ENGINE_VERSION,
            "max_tokens": self.max_tokens,
            "max_lines": self.max_lines,
            "max_tokens_per_item": self.max_tokens_per_item,
            "max_lines_per_item": self.max_lines_per_item,
            "target_reservation_fraction": self.target_reservation_fraction,
            "graph_depth": self.graph_depth,
            "max_items_per_relationship": self.max_items_per_relationship,
            "enabled_kinds": sorted(k.value for k in self.enabled_kinds),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()
