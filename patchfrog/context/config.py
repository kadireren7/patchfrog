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
#:
#: Bumped to 2 for Milestone E (adaptive multi-hop context): depth-2
#: candidate generation now excludes the original target/root symbol(s)
#: from both the depth-1 and depth-2 sets (a real cycle/self-call bug --
#: see :mod:`patchfrog.context.candidates`), and
#: :attr:`ContextConfig.max_expansion_roots` replaces a previously
#: hardcoded constant -- both change what a fixed-depth-2 bundle
#: actually contains, not just what adaptive mode adds.
CONTEXT_ENGINE_VERSION = 2

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

#: v1 hard cap -- see :class:`AdaptiveContextConfig`. A future milestone
#: may raise this; this milestone solves the proven 1-hop -> 2-hop gap
#: only.
MAX_SUPPORTED_ADAPTIVE_DEPTH = 2


@dataclass(frozen=True, slots=True)
class AdaptiveContextConfig:
    """Explicit, typed adaptive-context policy -- a first-class mode,
    never a hidden behavior overloaded onto ``graph_depth`` (see
    :mod:`patchfrog.context.adaptive` for the deterministic decision
    logic this configures).

    ``enabled=False`` (the default) means adaptive mode is off entirely
    -- :class:`ContextConfig`'s plain ``graph_depth`` (fixed 1 or fixed
    2) governs generation exactly as before this milestone existed.
    """

    enabled: bool = False
    #: Always 1 in v1 -- context always starts at depth 1; exposed
    #: (rather than hardcoded) purely for clarity/testability, not
    #: because v1 supports changing it.
    initial_depth: int = 1
    #: Hard-capped at :data:`MAX_SUPPORTED_ADAPTIVE_DEPTH` (2) -- see
    #: :meth:`__post_init__`.
    max_depth: int = 2
    #: Bounded fraction of :attr:`ContextConfig.max_tokens` /
    #: :attr:`ContextConfig.max_lines` that depth-2 (expansion) items may
    #: consume in total, on top of whatever depth-1 already used --
    #: expansion can never inflate the bundle beyond the existing
    #: ``max_tokens``/``max_lines`` ceiling, it only competes for a
    #: bounded slice of it (see :mod:`patchfrog.context.budgeting`).
    expansion_token_fraction: float = 0.3
    expansion_line_fraction: float = 0.3

    def __post_init__(self) -> None:
        if self.initial_depth != 1:
            raise ValueError("AdaptiveContextConfig.initial_depth must be 1 in v1")
        if not (1 <= self.max_depth <= MAX_SUPPORTED_ADAPTIVE_DEPTH):
            raise ValueError(
                f"AdaptiveContextConfig.max_depth must be between 1 and "
                f"{MAX_SUPPORTED_ADAPTIVE_DEPTH} in v1, got {self.max_depth}"
            )
        if not (0.0 < self.expansion_token_fraction <= 1.0):
            raise ValueError("AdaptiveContextConfig.expansion_token_fraction must be in (0, 1]")
        if not (0.0 < self.expansion_line_fraction <= 1.0):
            raise ValueError("AdaptiveContextConfig.expansion_line_fraction must be in (0, 1]")

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "initial_depth": self.initial_depth,
            "max_depth": self.max_depth,
            "expansion_token_fraction": self.expansion_token_fraction,
            "expansion_line_fraction": self.expansion_line_fraction,
        }


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
    #: Maximum graph hops from the target considered at all when
    #: ``adaptive`` is not set. 1 = direct callers/callees/tests/imports
    #: only; 2 additionally considers callers-of-callers /
    #: callees-of-callees, subject to budget. Ignored (superseded by
    #: ``adaptive.max_depth``) whenever ``adaptive is not None and
    #: adaptive.enabled``.
    graph_depth: int = 1
    #: Diversity cap: at most this many selected items per relationship
    #: kind, so budget isn't consumed entirely by e.g. five callers.
    max_items_per_relationship: int = 3
    #: Bound on how many depth-1 caller/callee nodes are expanded to
    #: depth 2 -- without this, a highly-connected symbol could trigger
    #: an expansion proportional to the whole call graph. Applies to
    #: both fixed ``graph_depth=2`` and adaptive expansion (the same
    #: underlying traversal, see :mod:`patchfrog.context.candidates`).
    max_expansion_roots: int = 5
    enabled_kinds: tuple[ContextItemKind, ...] = field(
        default_factory=lambda: DEFAULT_ENABLED_RELATIONSHIP_KINDS
    )
    #: ``None`` (default): adaptive mode off, ``graph_depth`` governs
    #: generation exactly as before this milestone. Set to enable
    #: deterministic 1-hop-first, expand-to-2-when-justified behavior
    #: (see :mod:`patchfrog.context.adaptive`).
    adaptive: AdaptiveContextConfig | None = None

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
            "max_expansion_roots": self.max_expansion_roots,
            "enabled_kinds": sorted(k.value for k in self.enabled_kinds),
            "adaptive": self.adaptive.fingerprint_payload() if self.adaptive is not None else None,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()
