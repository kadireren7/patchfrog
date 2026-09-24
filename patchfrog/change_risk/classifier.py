"""Deterministic PR-level change/risk classification (M4.1).

``classify_change(diff_files)`` is a pure function of the diff (plus an
optional boolean for already-computed static-analysis evidence). No LLM,
no network, no repository reads, no randomness: the same diff always
yields the same tier, signals and reasons.

Tier rules, in order:

1. ``NO_AI`` -- no changed file needs a model: every file is docs,
   excluded generated/vendor output, a lockfile, or a code file whose
   every changed line is blank or provably a comment (see
   :mod:`patchfrog.change_risk.comments`). An empty diff is also
   ``NO_AI``.
2. ``HIGH_RISK`` -- a *corroborated* high-risk combination:
   a security-sensitive path or a static HIGH/security finding **plus**
   at least one other elevating signal; or a database migration plus a
   schema/public-interface/large change; or three or more elevating
   signals at once. A security-sensitive path token alone never reaches
   this tier (a path keyword is never the sole decisive signal).
3. ``ELEVATED`` -- any single elevating signal
   (:data:`~patchfrog.change_risk.domain.ELEVATING_SIGNALS`).
4. ``TINY`` -- small: at most ``tiny_max_semantic_lines`` semantic
   changed lines across at most ``tiny_max_files`` reviewable files
   (test-only changes get a larger allowance).
5. ``NORMAL`` -- everything else.

Signals only ever bound *effort*; none of them is a finding.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from patchfrog.change_risk.comments import count_semantic_changed_lines, is_python_path
from patchfrog.change_risk.domain import (
    ELEVATING_SIGNALS,
    ChangeRiskClassification,
    ChangeRiskSignal,
    ChangeRiskTier,
    FileChangeClass,
    FileChangeProfile,
)
from patchfrog.change_risk.paths import NON_CODE_CLASSES, classify_path, is_security_sensitive_path
from patchfrog.diff.models import DiffFile

_S = ChangeRiskSignal


@dataclass(frozen=True, slots=True)
class ChangeRiskPolicy:
    """Operator-tunable thresholds. Defaults are engine policy; every
    field participates in :meth:`fingerprint` so a changed threshold can
    never silently reuse an exact-head review made under another."""

    tiny_max_semantic_lines: int = 20
    tiny_max_files: int = 2
    test_only_tiny_max_semantic_lines: int = 80
    large_semantic_lines: int = 400
    large_files: int = 15
    deletion_heavy_min_deleted: int = 50
    deletion_heavy_ratio: float = 2.0
    cross_module_min_modules: int = 3
    #: When False, generated/vendor paths are reviewed like code.
    exclude_generated_and_vendor: bool = True

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "tiny_max_semantic_lines": self.tiny_max_semantic_lines,
            "tiny_max_files": self.tiny_max_files,
            "test_only_tiny_max_semantic_lines": self.test_only_tiny_max_semantic_lines,
            "large_semantic_lines": self.large_semantic_lines,
            "large_files": self.large_files,
            "deletion_heavy_min_deleted": self.deletion_heavy_min_deleted,
            "deletion_heavy_ratio": self.deletion_heavy_ratio,
            "cross_module_min_modules": self.cross_module_min_modules,
            "exclude_generated_and_vendor": self.exclude_generated_and_vendor,
        }


_PUBLIC_DEF_PATTERNS: dict[str, re.Pattern[str]] = {
    "python": re.compile(r"^\s*(async\s+def|def|class)\s+(?!_)\w+"),
    "js": re.compile(r"^\s*export\s"),
    "go": re.compile(r"^(func\s+(\([^)]*\)\s*)?[A-Z]\w*|type\s+[A-Z]\w*)"),
    "jvm": re.compile(r"^\s*public\s"),
    "rust": re.compile(r"^\s*pub(\([^)]*\))?\s"),
}
_SUFFIX_FAMILY = {
    ".py": "python", ".pyi": "python",
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js", ".ts": "js", ".tsx": "js",
    ".go": "go",
    ".java": "jvm", ".kt": "jvm", ".cs": "jvm", ".scala": "jvm",
    ".rs": "rust",
}
_HEADER_SUFFIXES = frozenset({".h", ".hh", ".hpp", ".hxx"})


def _public_interface_touched(diff_file: DiffFile) -> bool:
    """A *removed or modified* public definition -- consumers may break.
    Only deleted lines are inspected: adding a new public function never
    breaks an existing consumer."""

    suffix = PurePosixPath(diff_file.path).suffix.lower()
    deleted = [line.content for line in diff_file.deleted_lines if line.content.strip()]
    if not deleted:
        return False
    if suffix in _HEADER_SUFFIXES:
        return any(not c.strip().startswith(("//", "/*", "*")) for c in deleted)
    family = _SUFFIX_FAMILY.get(suffix)
    if family is None:
        return False
    pattern = _PUBLIC_DEF_PATTERNS[family]
    return any(pattern.match(c) for c in deleted)


def _module_of(path: str) -> str:
    parts = PurePosixPath(path).parts
    if len(parts) <= 1:
        return "."
    if parts[0] in {"src", "lib", "app", "apps", "packages", "pkg", "internal"} and len(parts) > 2:
        return "/".join(parts[:2])
    return "/".join(parts[:2]) if len(parts) > 2 else parts[0]


#: ``path -> (head comment lines, base comment lines)`` -- exact
#: tokenizer-derived evidence (see
#: :func:`patchfrog.change_risk.comments.python_comment_lines`).
CommentLineEvidence = Mapping[str, tuple[frozenset[int] | None, frozenset[int] | None]]


def _profile(
    diff_file: DiffFile, *, policy: ChangeRiskPolicy, comment_lines: CommentLineEvidence
) -> FileChangeProfile:
    classes = set(classify_path(diff_file.path))
    if not policy.exclude_generated_and_vendor and classes & {FileChangeClass.GENERATED, FileChangeClass.VENDOR}:
        classes = {FileChangeClass.CODE}
    added = len(diff_file.added_lines)
    deleted = len(diff_file.deleted_lines)
    if classes & NON_CODE_CLASSES:
        semantic, comment_only = 0, False
    else:
        head_lines, base_lines = comment_lines.get(diff_file.path, (None, None))
        semantic, comment_only = count_semantic_changed_lines(
            diff_file, head_comment_lines=head_lines, base_comment_lines=base_lines
        )
        if not diff_file.hunks:
            # Binary or too-large-to-diff: unknown content, never
            # comment-only, and never "small".
            semantic, comment_only = max(semantic, policy.large_semantic_lines), False
    return FileChangeProfile(
        path=diff_file.path,
        classes=frozenset(classes),
        added_lines=added,
        deleted_lines=deleted,
        semantic_changed_lines=semantic,
        comment_only=comment_only,
        security_sensitive=is_security_sensitive_path(diff_file.path) and not classes & NON_CODE_CLASSES,
        public_interface_touched=not comment_only and not classes & NON_CODE_CLASSES
        and _public_interface_touched(diff_file),
    )


def classify_change(
    diff_files: Sequence[DiffFile],
    *,
    policy: ChangeRiskPolicy | None = None,
    static_high_risk_finding: bool = False,
    comment_lines: CommentLineEvidence | None = None,
) -> ChangeRiskClassification:
    """Classify one PR/diff. ``static_high_risk_finding`` is an
    already-known fact from static analysis on the changed spans
    (HIGH/CRITICAL severity or security category) -- evidence the caller
    already has, never computed here. ``comment_lines`` is optional exact
    comment-line evidence for Python files (the caller reads file content;
    this function never does I/O)."""

    policy = policy or ChangeRiskPolicy()
    evidence = comment_lines or {}
    profiles = tuple(
        sorted((_profile(f, policy=policy, comment_lines=evidence) for f in diff_files), key=lambda p: p.path)
    )
    added = sum(p.added_lines for p in profiles)
    deleted = sum(p.deleted_lines for p in profiles)
    semantic = sum(p.semantic_changed_lines for p in profiles)

    def make(
        tier: ChangeRiskTier,
        signals: tuple[ChangeRiskSignal, ...],
        reasons: tuple[str, ...],
        no_ai_reason: ChangeRiskSignal | None = None,
    ) -> ChangeRiskClassification:
        return ChangeRiskClassification(
            tier=tier,
            signals=signals,
            reasons=reasons,
            changed_files=len(profiles),
            added_lines=added,
            deleted_lines=deleted,
            semantic_changed_lines=semantic,
            files=profiles,
            no_ai_reason=no_ai_reason,
        )

    if not profiles or (added + deleted == 0 and all(f.hunks for f in diff_files)):
        return make(ChangeRiskTier.NO_AI, (_S.EMPTY_DIFF,), ("the diff changes no lines",), _S.EMPTY_DIFF)

    reviewable = [p for p in profiles if not p.classes & NON_CODE_CLASSES and not p.comment_only]
    if not reviewable:
        no_ai = _no_ai_reason(profiles)
        return make(ChangeRiskTier.NO_AI, (no_ai,), (_NO_AI_TEXT[no_ai],), no_ai)

    signals: set[ChangeRiskSignal] = set()
    reasons: list[str] = []

    def add(signal: ChangeRiskSignal, reason: str) -> None:
        if signal not in signals:
            signals.add(signal)
            reasons.append(reason)

    test_only = all(FileChangeClass.TEST in p.classes for p in reviewable)
    if test_only:
        add(_S.TEST_ONLY, "only test files change")
    if any(p.classes & {FileChangeClass.LOCKFILE} for p in profiles):
        add(_S.LOCKFILE_CHANGE, "a dependency lockfile changes")
    manifests = [p.path for p in reviewable if FileChangeClass.MANIFEST in p.classes]
    if manifests:
        add(_S.DEPENDENCY_MANIFEST_CHANGE, f"dependency manifest changes: {', '.join(manifests[:3])}")
    ci = [p.path for p in reviewable if FileChangeClass.CI in p.classes]
    if ci:
        add(_S.CI_CONFIG_CHANGE, f"CI/workflow configuration changes: {', '.join(ci[:3])}")
    if any(FileChangeClass.CONFIG in p.classes for p in reviewable):
        add(_S.CONFIG_CHANGE, "deployment/tool configuration changes")
    migrations = [p.path for p in reviewable if FileChangeClass.MIGRATION in p.classes]
    if migrations:
        add(_S.DATABASE_MIGRATION, f"database migration changes: {', '.join(migrations[:3])}")
    schemas = [p.path for p in reviewable if FileChangeClass.SCHEMA in p.classes and p.path not in migrations]
    if schemas:
        add(_S.SCHEMA_MODEL_CHANGE, f"schema/model definitions change: {', '.join(schemas[:3])}")
    public = [p.path for p in reviewable if p.public_interface_touched and FileChangeClass.TEST not in p.classes]
    if public:
        add(_S.PUBLIC_INTERFACE_CHANGE, f"a public definition is removed or modified in {', '.join(public[:3])}")
    security = [p.path for p in reviewable if p.security_sensitive and FileChangeClass.TEST not in p.classes]
    if security:
        add(_S.SECURITY_SENSITIVE_PATH, f"security-sensitive paths change: {', '.join(security[:3])}")
    if static_high_risk_finding:
        add(_S.STATIC_HIGH_RISK_FINDING, "static analysis reports a HIGH/security finding on changed code")

    review_semantic = sum(p.semantic_changed_lines for p in reviewable)
    review_deleted = sum(p.deleted_lines for p in reviewable)
    review_added = sum(p.added_lines for p in reviewable)
    if review_semantic >= policy.large_semantic_lines or len(reviewable) >= policy.large_files:
        add(_S.LARGE_CHANGE, f"large change: {review_semantic} semantic lines across {len(reviewable)} files")
    if review_deleted >= policy.deletion_heavy_min_deleted and review_deleted >= policy.deletion_heavy_ratio * max(
        1, review_added
    ):
        add(_S.DELETION_HEAVY, f"deletion-heavy change: {review_deleted} deleted vs {review_added} added lines")
    modules = {_module_of(p.path) for p in reviewable if FileChangeClass.TEST not in p.classes}
    if len(modules) >= policy.cross_module_min_modules:
        add(_S.CROSS_MODULE, f"change spans {len(modules)} modules")

    elevating = signals & ELEVATING_SIGNALS
    tier: ChangeRiskTier
    risky_anchor = signals & {_S.SECURITY_SENSITIVE_PATH, _S.STATIC_HIGH_RISK_FINDING}
    if (
        (risky_anchor and len(elevating - risky_anchor) >= 1)
        or (_S.STATIC_HIGH_RISK_FINDING in signals and _S.SECURITY_SENSITIVE_PATH in signals)
        or (
            _S.DATABASE_MIGRATION in signals
            and signals & {_S.SCHEMA_MODEL_CHANGE, _S.PUBLIC_INTERFACE_CHANGE, _S.LARGE_CHANGE}
        )
        or len(elevating) >= 3
    ):
        tier = ChangeRiskTier.HIGH_RISK
    elif elevating:
        tier = ChangeRiskTier.ELEVATED
    else:
        tiny_limit = policy.test_only_tiny_max_semantic_lines if test_only else policy.tiny_max_semantic_lines
        if review_semantic <= tiny_limit and len(reviewable) <= policy.tiny_max_files:
            add(_S.SMALL_CHANGE, f"small change: {review_semantic} semantic lines in {len(reviewable)} file(s)")
            tier = ChangeRiskTier.TINY
        else:
            tier = ChangeRiskTier.NORMAL

    ordered = tuple(s for s in ChangeRiskSignal if s in signals)
    return make(tier, ordered, tuple(reasons))


_NO_AI_TEXT = {
    _S.DOCS_ONLY: "only documentation changes",
    _S.COMMENT_ONLY: "only comments/blank lines change in code files",
    _S.GENERATED_OR_VENDOR_ONLY: "only policy-excluded generated/vendored files change",
    _S.LOCKFILE_ONLY: "only dependency lockfiles change (deterministic dependency tooling owns these)",
    _S.NON_CODE_ONLY: "only documentation, comments, lockfiles or excluded generated/vendored files change",
}


def _no_ai_reason(profiles: Sequence[FileChangeProfile]) -> ChangeRiskSignal:
    kinds: set[ChangeRiskSignal] = set()
    for p in profiles:
        if FileChangeClass.DOCS in p.classes:
            kinds.add(_S.DOCS_ONLY)
        elif p.classes & {FileChangeClass.GENERATED, FileChangeClass.VENDOR}:
            kinds.add(_S.GENERATED_OR_VENDOR_ONLY)
        elif FileChangeClass.LOCKFILE in p.classes:
            kinds.add(_S.LOCKFILE_ONLY)
        else:
            kinds.add(_S.COMMENT_ONLY)
    return kinds.pop() if len(kinds) == 1 else _S.NON_CODE_ONLY


def python_files_needing_comment_evidence(diff_files: Sequence[DiffFile]) -> tuple[str, ...]:
    """Python files whose every changed non-blank line *looks* like a
    ``#`` comment -- the only files where exact tokenizer evidence can
    change the outcome (to comment-only). Callers fetch content for
    these and nothing else."""

    paths: list[str] = []
    for diff_file in diff_files:
        if not is_python_path(diff_file.path) or not diff_file.hunks:
            continue
        changed = [
            line.content.strip()
            for line in (*diff_file.added_lines, *diff_file.deleted_lines)
            if line.content.strip()
        ]
        if changed and all(c.startswith("#") for c in changed):
            paths.append(diff_file.path)
    return tuple(sorted(paths))


__all__ = ["ChangeRiskPolicy", "CommentLineEvidence", "classify_change", "python_files_needing_comment_evidence"]
