"""Ground-truth loading and validation for the evaluation benchmark
corpus.

Every case is a directory under ``tests/fixtures/evaluation/cases/<id>/``:

    <id>/case.yaml   -- human-authored, committed ground truth (see the
                        Phase 8 spec's YAML schema)
    <id>/repo/        -- a plain file tree (no ``.git``, same convention
                        as :mod:`tests.support.git_repo`'s existing
                        fixture repos) -- materialized as a real git
                        repo by :mod:`patchfrog.evaluation.runner`.

Validation (see the module docstring of :mod:`patchfrog.evaluation.matcher`
and Phase 8 spec section 50) fails fast on broken benchmark metadata --
a case with a nonexistent expected file, an invalid line range, or a
duplicate expected-finding id is never silently run.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from patchfrog.analysis.domain import FindingCategory, Severity
from patchfrog.evaluation.domain import (
    Difficulty,
    EvaluationCase,
    ExpectedFinding,
    FixtureInfo,
    ForbiddenFinding,
    GroundTruthSource,
    Language,
)
from patchfrog.repository.git import run_git

DEFAULT_CASES_ROOT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "evaluation" / "cases"


class BenchmarkValidationError(ValueError):
    """Raised when a case's committed ground truth is internally
    inconsistent or references fixture content that doesn't exist --
    always raised before any evaluation run starts, never discovered
    mid-run."""


def load_case(case_dir: Path) -> EvaluationCase:
    case_file = case_dir / "case.yaml"
    if not case_file.is_file():
        raise BenchmarkValidationError(f"{case_dir}: missing case.yaml")

    raw = yaml.safe_load(case_file.read_text())
    if not isinstance(raw, dict) or "case" not in raw:
        raise BenchmarkValidationError(f"{case_file}: must be a mapping with a top-level 'case' key")

    meta = raw["case"]
    case_id = str(meta["id"])
    if case_id != case_dir.name:
        raise BenchmarkValidationError(
            f"{case_file}: case.id {case_id!r} must match directory name {case_dir.name!r}"
        )

    expected = tuple(_parse_expected(e) for e in raw.get("expected", []))
    forbidden = tuple(_parse_forbidden(f) for f in raw.get("forbidden", []))

    return EvaluationCase(
        id=case_id,
        title=str(meta.get("title", case_id)),
        description=str(meta.get("description", "")),
        language=Language(meta["language"]),
        fixture=case_id,
        difficulty=Difficulty(meta.get("difficulty", "medium")),
        tags=tuple(str(t) for t in meta.get("tags", [])),
        expected=expected,
        forbidden=forbidden,
        notes=str(meta.get("notes", "")),
    )


def _parse_expected(raw: dict[str, Any]) -> ExpectedFinding:
    severity = raw.get("severity")
    return ExpectedFinding(
        id=str(raw["id"]),
        category=FindingCategory(raw["category"]),
        file=str(raw["file"]),
        issue_family=str(raw.get("issue_family", "")),
        symbol=str(raw["symbol"]) if raw.get("symbol") is not None else None,
        severity=Severity(severity) if severity is not None else None,
        severity_min=Severity(raw["severity_min"]) if raw.get("severity_min") is not None else None,
        severity_max=Severity(raw["severity_max"]) if raw.get("severity_max") is not None else None,
        line=int(raw["line"]) if raw.get("line") is not None else None,
        line_end=int(raw["line_end"]) if raw.get("line_end") is not None else None,
        line_tolerance=int(raw.get("line_tolerance", 3)),
        evidence_contains=str(raw["evidence_contains"]) if raw.get("evidence_contains") is not None else None,
        ground_truth_source=GroundTruthSource(raw.get("ground_truth_source", "either")),
        notes=str(raw.get("notes", "")),
    )


def _parse_forbidden(raw: dict[str, Any]) -> ForbiddenFinding:
    category = raw.get("category")
    return ForbiddenFinding(
        reason=str(raw["reason"]),
        category=FindingCategory(category) if category is not None else None,
        issue_family=str(raw["issue_family"]) if raw.get("issue_family") is not None else None,
    )


def load_all_cases(cases_root: Path = DEFAULT_CASES_ROOT) -> list[EvaluationCase]:
    if not cases_root.is_dir():
        return []
    cases = [
        load_case(child)
        for child in sorted(cases_root.iterdir())
        if child.is_dir() and (child / "case.yaml").is_file()
    ]
    _validate_no_duplicate_case_ids(cases)
    return cases


def _validate_no_duplicate_case_ids(cases: Iterable[EvaluationCase]) -> None:
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise BenchmarkValidationError(f"duplicate case id: {case.id!r}")
        seen.add(case.id)


def validate_case(case: EvaluationCase, *, cases_root: Path = DEFAULT_CASES_ROOT) -> list[str]:
    """Returns a list of validation error strings (empty == valid).
    Never raises -- callers decide whether to fail fast (see
    :func:`validate_and_raise`) or collect every case's errors first."""

    errors: list[str] = []
    repo_root = cases_root / case.id / "repo"
    if not repo_root.is_dir():
        errors.append(f"fixture repo directory missing: {repo_root}")
        return errors  # nothing else is checkable without the repo

    seen_expected_ids: set[str] = set()
    seen_dedup_keys: set[tuple[str, str | None, str, int | None]] = set()
    for e in case.expected:
        if e.id in seen_expected_ids:
            errors.append(f"duplicate expected finding id: {e.id!r}")
        seen_expected_ids.add(e.id)

        dedup_key = (e.file, e.symbol, e.category.value, e.line)
        if dedup_key in seen_dedup_keys:
            errors.append(f"expected finding {e.id!r} duplicates another (file/symbol/category/line)")
        seen_dedup_keys.add(dedup_key)

        file_path = repo_root / e.file
        if not file_path.is_file():
            errors.append(f"expected finding {e.id!r}: file does not exist: {e.file}")
            continue

        content = file_path.read_text(errors="replace")
        line_count = len(content.splitlines())
        if e.line is not None and not (1 <= e.line <= line_count):
            errors.append(f"expected finding {e.id!r}: line {e.line} out of range (file has {line_count} lines)")
        if e.line_end is not None and e.line is not None and e.line_end < e.line:
            errors.append(f"expected finding {e.id!r}: line_end {e.line_end} < line {e.line}")
        if e.line_tolerance < 0:
            errors.append(f"expected finding {e.id!r}: line_tolerance must be >= 0")
        if e.symbol is not None and e.symbol not in content:
            errors.append(f"expected finding {e.id!r}: symbol {e.symbol!r} not found (as text) in {e.file}")

    for f in case.forbidden:
        if f.category is None and f.issue_family is None:
            errors.append("forbidden rule must specify at least category or issue_family")
        if not f.reason.strip():
            errors.append("forbidden rule missing a reason")

    return errors


def validate_and_raise(cases: Iterable[EvaluationCase], *, cases_root: Path = DEFAULT_CASES_ROOT) -> None:
    all_errors: list[str] = []
    for case in cases:
        for error in validate_case(case, cases_root=cases_root):
            all_errors.append(f"[{case.id}] {error}")
    if all_errors:
        raise BenchmarkValidationError("benchmark ground truth validation failed:\n" + "\n".join(all_errors))


def materialize_case_repo(
    case: EvaluationCase, *, cases_root: Path = DEFAULT_CASES_ROOT, workdir_root: Path | None = None
) -> tuple[Path, str]:
    """Copy a case's committed fixture tree into a fresh throwaway
    directory and git-init/commit it -- mirrors
    ``tests.support.git_repo.materialize_fixture_repo``'s convention
    exactly, reimplemented here (rather than imported) so production
    package :mod:`patchfrog.evaluation` never depends on test-only code.
    Caller owns cleanup (``shutil.rmtree`` the returned path)."""

    src = cases_root / case.id / "repo"
    if not src.is_dir():
        raise BenchmarkValidationError(f"fixture repo missing for case {case.id!r}: {src}")

    dest = Path(tempfile.mkdtemp(prefix=f"patchfrog-eval-{case.id}-", dir=workdir_root))
    shutil.copytree(src, dest, dirs_exist_ok=True)
    run_git(["-C", str(dest), "init", "--quiet"])
    run_git(["-C", str(dest), "config", "user.email", "eval@patchfrog.dev"])
    run_git(["-C", str(dest), "config", "user.name", "PatchFrog Eval"])
    run_git(["-C", str(dest), "add", "-A"])
    run_git(["-C", str(dest), "commit", "--quiet", "-m", f"eval case {case.id}"])
    commit_sha = run_git(["-C", str(dest), "rev-parse", "HEAD"]).strip()
    return dest, commit_sha


def fixture_info_for_case(case: EvaluationCase, *, cases_root: Path = DEFAULT_CASES_ROOT) -> FixtureInfo:
    """Built from the *committed* fixture tree (not a transient
    materialized copy) -- identical content either way, but avoids
    coupling hallucination-detection to whichever runner call happens
    to have a repo checked out at the time."""

    repo_root = cases_root / case.id / "repo"
    valid_paths: set[str] = set()
    line_counts: dict[str, int] = {}
    for path in repo_root.rglob("*"):
        if path.is_file():
            rel = str(path.relative_to(repo_root))
            valid_paths.add(rel)
            line_counts[rel] = len(path.read_text(errors="replace").splitlines())
    return FixtureInfo(valid_file_paths=frozenset(valid_paths), file_line_counts=line_counts)


def fixture_content_hash(case: EvaluationCase, *, cases_root: Path = DEFAULT_CASES_ROOT) -> str:
    """A stable hash over one case's committed ground truth plus fixture
    file contents -- part of :class:`~patchfrog.evaluation.domain.EvaluationIdentity`,
    so a baseline is never silently compared against a run of different
    fixture content."""

    case_dir = cases_root / case.id
    digest = hashlib.sha256()
    digest.update((case_dir / "case.yaml").read_bytes())
    repo_root = case_dir / "repo"
    if repo_root.is_dir():
        for path in sorted(repo_root.rglob("*")):
            if path.is_file():
                digest.update(str(path.relative_to(repo_root)).encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()
