"""Deterministic patch generation (M7.3).

Reads the checkout, **never writes it**: every planned automatic step is
turned into character-span edits against that file's *original* content
(all steps for one file are located against the same pristine text, so
one step's rewrite never has to re-find a call another step already
edited), the edits for a file are merged into its new content, and a
unified diff is produced. The result also carries ``new_contents`` in
memory -- a caller decides whether/how to materialize it; this module
never touches the working tree.

Idempotent by construction: on an already-migrated repository, every
rewriter raises :class:`~patchfrog.migration.edits.NotNeeded` (the code
already has the target shape) and the file is left unchanged -- re-running
``generate_patch`` on its own output is a no-op patch.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from patchfrog.dependencies.files import MAX_DISCOVERY_FILE_BYTES, is_secret_store_path
from patchfrog.migration.domain import (
    GeneratedPatch,
    MigrationPlan,
    MigrationStep,
    PatchOrigin,
    StepOutcome,
    StepResult,
)
from patchfrog.migration.edits import (
    NotNeeded,
    RewriteError,
    TextEdit,
    apply_edits,
    overlaps,
    unified_diff,
)
from patchfrog.migration.js_rewrite import js_edits
from patchfrog.migration.manifest_rewrite import manifest_edits
from patchfrog.migration.python_rewrite import python_edits
from patchfrog.migration.safety import run_safety_gates


def _rewrite(step: MigrationStep, text: str) -> list[TextEdit]:
    language = step.target.language
    if language == "python":
        return python_edits(step, text)
    if language == "javascript":
        return js_edits(step, text)
    if language == "manifest" and step.operation is not None and step.operation.kind == "bump_manifest_version":
        return manifest_edits(step, text)
    raise RewriteError(f"no deterministic rewriter for language {language!r}")


def read_target_file(root: Path, relative_path: str) -> str | None:
    if is_secret_store_path(relative_path):
        return None
    candidate = (root / relative_path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file() or candidate.is_symlink():
        return None
    try:
        if candidate.stat().st_size > MAX_DISCOVERY_FILE_BYTES:
            return None
        return candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def generate_patch(plan: MigrationPlan, root: Path) -> GeneratedPatch:
    resolved_root = root.resolve()
    automatic = [s for s in plan.steps if s.auto_fix]
    unresolved = [s for s in plan.steps if not s.auto_fix]

    by_file: dict[str, list[MigrationStep]] = defaultdict(list)
    for step in automatic:
        by_file[step.target.file_path].append(step)

    before: dict[str, str] = {}
    after: dict[str, str] = {}
    edit_scopes: dict[str, list[tuple[str, tuple[int, int]]]] = defaultdict(list)
    step_results: list[StepResult] = []

    for file_path, steps in sorted(by_file.items()):
        original = read_target_file(resolved_root, file_path)
        if original is None:
            for step in steps:
                step_results.append(
                    StepResult(step.step_id, StepOutcome.FAILED, f"{file_path} cannot be read for editing")
                )
            continue
        before[file_path] = original
        accepted: list[TextEdit] = []
        for step in steps:
            try:
                new_edits = _rewrite(step, original)
            except NotNeeded as exc:
                step_results.append(StepResult(step.step_id, StepOutcome.NOT_NEEDED, str(exc)))
                continue
            except RewriteError as exc:
                step_results.append(StepResult(step.step_id, StepOutcome.FAILED, str(exc)))
                continue
            if any(overlaps(edit, accepted) for edit in new_edits):
                step_results.append(
                    StepResult(step.step_id, StepOutcome.FAILED, "edit conflicts with another step at the same site")
                )
                continue
            accepted.extend(new_edits)
            edit_scopes[file_path].extend((step.step_id, edit.scope) for edit in new_edits)
            step_results.append(StepResult(step.step_id, StepOutcome.APPLIED, f"{len(new_edits)} edit(s)",
                                           edits=len(new_edits)))
        try:
            after[file_path] = apply_edits(original, accepted) if accepted else original
        except RewriteError as exc:
            after[file_path] = original
            applied_here = {s.step_id for s in steps} & {r.step_id for r in step_results
                                                          if r.outcome is StepOutcome.APPLIED}
            step_results = [
                r if r.step_id not in applied_here
                else StepResult(r.step_id, StepOutcome.FAILED, f"file-level conflict: {exc}")
                for r in step_results
            ]

    for step in unresolved:
        reason = "not automatic" if step.operation is None else "eligibility requires human review"
        step_results.append(StepResult(step.step_id, StepOutcome.SKIPPED, reason))

    modified_files = sorted(p for p in after if after[p] != before.get(p, ""))
    diff_text = "".join(unified_diff(p, before[p], after[p]) for p in modified_files)

    safety = run_safety_gates(steps=plan.steps, results=step_results, before=before, after=after,
                              edit_scopes=edit_scopes)

    return GeneratedPatch(
        origin=PatchOrigin.DETERMINISTIC,
        unified_diff=diff_text,
        modified_files=tuple(modified_files),
        new_contents={p: after[p] for p in modified_files},
        step_results=tuple(step_results),
        safety=safety,
    )


__all__ = ["generate_patch", "read_target_file"]
