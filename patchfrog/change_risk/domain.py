"""Typed vocabulary for the deterministic PR-level change/risk classifier.

A :class:`ChangeRiskClassification` answers "how much provider work is
this change worth, and why?" *before* any provider call, from the diff
alone. It never decides that anything is a defect -- it only bounds and
explains review effort. The same vocabulary is meant to be reused by
future dependency-migration workflows (M6+) to decide how much
verification a generated migration deserves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

#: Bumped only when *this package's own* classification semantics change
#: (a new signal, a changed threshold meaning, a changed tier rule).
CHANGE_RISK_POLICY_VERSION = 1


class ChangeRiskTier(StrEnum):
    """Ordered from cheapest to most scrutinised. ``NO_AI`` means no
    provider call is justified at all (docs/comment/excluded-generated
    only); the review lifecycle still runs and reports deterministically."""

    NO_AI = "no_ai"
    TINY = "tiny"
    NORMAL = "normal"
    ELEVATED = "elevated"
    HIGH_RISK = "high_risk"


TIER_ORDER: tuple[ChangeRiskTier, ...] = (
    ChangeRiskTier.NO_AI,
    ChangeRiskTier.TINY,
    ChangeRiskTier.NORMAL,
    ChangeRiskTier.ELEVATED,
    ChangeRiskTier.HIGH_RISK,
)


class FileChangeClass(StrEnum):
    """What kind of file a changed path is -- purely path-derived."""

    CODE = "code"
    TEST = "test"
    DOCS = "docs"
    GENERATED = "generated"
    VENDOR = "vendor"
    LOCKFILE = "lockfile"
    MANIFEST = "manifest"
    CI = "ci"
    CONFIG = "config"
    MIGRATION = "migration"
    SCHEMA = "schema"


class ChangeRiskSignal(StrEnum):
    """One deterministic, explainable fact about the diff."""

    EMPTY_DIFF = "empty_diff"
    DOCS_ONLY = "docs_only"
    COMMENT_ONLY = "comment_only"
    GENERATED_OR_VENDOR_ONLY = "generated_or_vendor_only"
    LOCKFILE_ONLY = "lockfile_only"
    NON_CODE_ONLY = "non_code_only"
    TEST_ONLY = "test_only"
    SMALL_CHANGE = "small_change"
    LARGE_CHANGE = "large_change"
    DELETION_HEAVY = "deletion_heavy"
    CROSS_MODULE = "cross_module"
    DEPENDENCY_MANIFEST_CHANGE = "dependency_manifest_change"
    LOCKFILE_CHANGE = "lockfile_change"
    CI_CONFIG_CHANGE = "ci_config_change"
    CONFIG_CHANGE = "config_change"
    PUBLIC_INTERFACE_CHANGE = "public_interface_change"
    SCHEMA_MODEL_CHANGE = "schema_model_change"
    DATABASE_MIGRATION = "database_migration"
    SECURITY_SENSITIVE_PATH = "security_sensitive_path"
    STATIC_HIGH_RISK_FINDING = "static_high_risk_finding"


#: Signals that, on their own, justify more than routine effort.
ELEVATING_SIGNALS: frozenset[ChangeRiskSignal] = frozenset(
    {
        ChangeRiskSignal.LARGE_CHANGE,
        ChangeRiskSignal.DELETION_HEAVY,
        ChangeRiskSignal.CROSS_MODULE,
        ChangeRiskSignal.DEPENDENCY_MANIFEST_CHANGE,
        ChangeRiskSignal.CI_CONFIG_CHANGE,
        ChangeRiskSignal.PUBLIC_INTERFACE_CHANGE,
        ChangeRiskSignal.SCHEMA_MODEL_CHANGE,
        ChangeRiskSignal.DATABASE_MIGRATION,
        ChangeRiskSignal.SECURITY_SENSITIVE_PATH,
        ChangeRiskSignal.STATIC_HIGH_RISK_FINDING,
    }
)


@dataclass(frozen=True, slots=True)
class FileChangeProfile:
    """Per-file facts. Carries counts and classes only -- never line
    content -- so it is always safe to persist/log."""

    path: str
    classes: frozenset[FileChangeClass]
    added_lines: int
    deleted_lines: int
    #: Non-blank, non-comment changed lines (0 for a comment-only edit).
    semantic_changed_lines: int
    comment_only: bool
    security_sensitive: bool
    public_interface_touched: bool

    @property
    def changed_lines(self) -> int:
        return self.added_lines + self.deleted_lines


@dataclass(frozen=True, slots=True)
class ChangeRiskClassification:
    tier: ChangeRiskTier
    signals: tuple[ChangeRiskSignal, ...]
    reasons: tuple[str, ...]
    changed_files: int
    added_lines: int
    deleted_lines: int
    #: Changed lines that could change behavior: code/test/config files,
    #: excluding blank and comment-only lines and excluded paths.
    semantic_changed_lines: int
    files: tuple[FileChangeProfile, ...] = field(default=())
    #: Set only for ``NO_AI`` -- the single signal that justified it.
    no_ai_reason: ChangeRiskSignal | None = None
    policy_version: int = CHANGE_RISK_POLICY_VERSION

    def has(self, signal: ChangeRiskSignal) -> bool:
        return signal in self.signals

    def paths_with(self, cls: FileChangeClass) -> tuple[str, ...]:
        return tuple(f.path for f in self.files if cls in f.classes)

    @property
    def public_interface_paths(self) -> tuple[str, ...]:
        return tuple(f.path for f in self.files if f.public_interface_touched)

    @property
    def security_sensitive_paths(self) -> tuple[str, ...]:
        return tuple(f.path for f in self.files if f.security_sensitive)

    def to_dict(self) -> dict[str, object]:
        """Counts/classes/signals only -- safe for logs and persistence."""

        return {
            "tier": self.tier.value,
            "signals": [s.value for s in self.signals],
            "reasons": list(self.reasons),
            "changed_files": self.changed_files,
            "added_lines": self.added_lines,
            "deleted_lines": self.deleted_lines,
            "semantic_changed_lines": self.semantic_changed_lines,
            "no_ai_reason": self.no_ai_reason.value if self.no_ai_reason else None,
            "policy_version": self.policy_version,
        }


__all__ = [
    "CHANGE_RISK_POLICY_VERSION",
    "ELEVATING_SIGNALS",
    "TIER_ORDER",
    "ChangeRiskClassification",
    "ChangeRiskSignal",
    "ChangeRiskTier",
    "FileChangeClass",
    "FileChangeProfile",
]
