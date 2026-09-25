"""Deterministic patch safety gates (M7.5).

A generated patch is a *migration candidate* only if every gate passes:

``allowed_files``         only files of automatic plan steps are touched
``edits_match_steps``     every changed line lies inside the scope of an
                          applied step (its usage-site call / calling
                          function / manifest line)
``no_secret_files``       no secret-store-like file touched; no
                          credential-shaped literal introduced
``no_dependency_removed`` manifests keep every declared package; only the
                          targeted package's constraint changes
``bounded_diff``          no mass rewrite: changed lines bounded per step
``line_stability``        line count of every file unchanged
``syntax``                Python parses (``ast``), JSON/TOML parse, JS/TS
                          brackets balance lexically
``obsolete_shape_gone``   the usage no longer matches the obsolete
                          contract shape at the migrated site

Passing the gates is **not** runtime verification -- that is M8.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath

from patchfrog.dependencies.files import is_secret_store_path
from patchfrog.dependencies.manifests import is_manifest, parse_manifest
from patchfrog.migration.domain import MigrationStep, SafetyGateResult, StepOutcome, StepResult
from patchfrog.migration.edits import line_of
from patchfrog.migration.path_rewrite import contains_path
from patchfrog.upstream.calls import (
    js_code_mask,
    language_for,
    locate_calls,
    locate_calls_at_line,
    matching_close,
)
from patchfrog.upstream.domain import normalize_package_name

MAX_CHANGED_LINES_PER_STEP = 12
MAX_CHANGED_LINES_TOTAL = 2000

_CREDENTIAL_RE = re.compile(
    r"""(['"])(?:sk|pk|rk)[-_](?:live|test|proj)?[-_]?[A-Za-z0-9]{12,}\1|(['"])gh[pousr]_[A-Za-z0-9]{16,}\2"""
    r"""|AKIA[0-9A-Z]{12,}|-----BEGIN [A-Z ]*PRIVATE KEY|xox[abpors]-[A-Za-z0-9-]{8,}"""
)


def _changed_lines(before: str, after: str) -> list[int]:
    b, a = before.splitlines(), after.splitlines()
    if len(b) != len(a):
        return list(range(1, max(len(a), len(b)) + 1))
    return [i + 1 for i, (x, y) in enumerate(zip(b, a, strict=True)) if x != y]


def _gate(name: str, problems: Sequence[str], ok: str) -> SafetyGateResult:
    return SafetyGateResult(name, not problems, "; ".join(problems[:10]) if problems else ok)


def _syntax_problem(path: str, text: str) -> str | None:
    suffix = PurePosixPath(path).suffix
    if language_for(path) == "python":
        try:
            ast.parse(text)
        except SyntaxError as exc:
            return f"{path}: Python syntax error at line {exc.lineno}"
    elif suffix == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            return f"{path}: invalid JSON ({exc.msg})"
    elif suffix == ".toml":
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return f"{path}: invalid TOML"
    elif language_for(path) == "javascript":
        mask = js_code_mask(text)
        stack: list[str] = []
        for i, ch in enumerate(text):
            if not mask[i]:
                continue
            if ch in "([{":
                closing = matching_close(text, mask, i)
                if closing is None:
                    return f"{path}: unbalanced '{ch}' at line {line_of(text, i)}"
                stack.append(ch)
            elif ch in ")]}" and not stack:
                return f"{path}: unexpected '{ch}' at line {line_of(text, i)}"
            elif ch in ")]}":
                stack.pop()
    return None


def _obsolete_shape_problem(step: MigrationStep, after: str) -> str | None:
    op = step.operation
    if op is None:
        return None
    path, line = step.target.file_path, step.target.line or 0
    kind = op.kind
    if kind == "rename_call_chain":
        if locate_calls(after, path, line, op.get("old")):
            return f"{path}:{line} still calls `{op.get('old')}`"
        if not locate_calls(after, path, line, op.get("new")):
            return f"{path}:{line} no longer has a call of `{op.get('new')}`"
    elif kind in ("rename_keyword", "add_keyword", "replace_keyword_literal"):
        # The call's own chain may already have been renamed by another
        # step at the same site (e.g. a symbol rename plus a keyword
        # rename): fall back to "any call at this line" rather than
        # searching by a token that is no longer there.
        calls = locate_calls(after, path, line, step.target.usage_token) or locate_calls_at_line(after, path, line)
        if not calls:
            return f"{path}:{line} call no longer found"
        for call in calls:
            if kind == "rename_keyword" and call.keyword(op.get("old")) is not None:
                return f"{path}:{line} still passes `{op.get('old')}`"
            if kind == "add_keyword" and call.keyword(op.get("name")) is None:
                return f"{path}:{line} does not pass `{op.get('name')}`"
            if kind == "replace_keyword_literal":
                keyword = call.keyword(op.get("name"))
                if keyword is not None and keyword.literal == op.get("old"):
                    return f"{path}:{line} still passes `{op.get('name')}={op.get('old')!r}`"
    elif kind == "replace_path_literal":
        line_text = after.splitlines()[line - 1] if 0 < line <= len(after.splitlines()) else ""
        if contains_path(line_text, op.get("old")):
            return f"{path}:{line} still requests `{op.get('old')}`"
    elif kind == "rename_import_module":
        line_text = after.splitlines()[line - 1] if 0 < line <= len(after.splitlines()) else ""
        if re.search(r"(?<![\w.])" + re.escape(op.get("old")) + r"(?![\w])", line_text) and \
                op.get("new") not in line_text:
            return f"{path}:{line} still imports `{op.get('old')}`"
    elif kind == "bump_manifest_version":
        declared = {
            normalize_package_name(d.name): d.spec or "" for d in parse_manifest(path, after)
        }
        spec = declared.get(normalize_package_name(op.get("package")), "")
        if op.get("version") not in spec:
            return f"{path}: {op.get('package')} does not require {op.get('version')}"
    return None


def run_safety_gates(
    *,
    steps: Sequence[MigrationStep],
    results: Sequence[StepResult],
    before: Mapping[str, str],
    after: Mapping[str, str],
    edit_scopes: Mapping[str, Sequence[tuple[str, tuple[int, int]]]],
) -> tuple[SafetyGateResult, ...]:
    """``edit_scopes``: file -> [(step_id, allowed line range)] of the
    edits that were applied."""

    applied = {r.step_id for r in results if r.outcome is StepOutcome.APPLIED}
    by_id = {s.step_id: s for s in steps}
    modified = sorted(p for p in after if after[p] != before.get(p))
    allowed = {by_id[i].target.file_path for i in applied}

    gates = [
        _gate("allowed_files", [f"{p} is not a target of an applied step" for p in modified if p not in allowed],
              f"{len(modified)} file(s), all planned targets"),
    ]

    outside: list[str] = []
    changed_total = 0
    for path in modified:
        scopes = edit_scopes.get(path, ())
        lines = _changed_lines(before[path], after[path])
        changed_total += len(lines)
        for number in lines:
            if not any(step_id in applied and lo <= number <= hi for step_id, (lo, hi) in scopes):
                outside.append(f"{path}:{number}")
    gates.append(_gate("edits_match_steps", [f"{loc} changed outside any step scope" for loc in outside],
                       "every changed line is inside an applied step's scope"))

    secret: list[str] = [f"{p} is a secret-store-like file" for p in modified if is_secret_store_path(p)]
    for path in modified:
        before_lines = set(before[path].splitlines())
        for number, text in enumerate(after[path].splitlines(), start=1):
            if text not in before_lines and _CREDENTIAL_RE.search(text):
                secret.append(f"{path}:{number} introduces a credential-shaped literal")
    gates.append(_gate("no_secret_files", secret, "no secret files, no credential-shaped literals"))

    removed: list[str] = []
    for path in modified:
        if not is_manifest(path):
            continue
        old_decl = {normalize_package_name(d.name): d.spec for d in parse_manifest(path, before[path])}
        new_decl = {normalize_package_name(d.name): d.spec for d in parse_manifest(path, after[path])}
        targets = {
            normalize_package_name(by_id[i].operation.get("package"))  # type: ignore[union-attr]
            for i in applied
            if by_id[i].target.file_path == path and by_id[i].operation is not None
            and by_id[i].operation.kind == "bump_manifest_version"  # type: ignore[union-attr]
        }
        removed += [f"{path}: {name} removed" for name in sorted(set(old_decl) - set(new_decl))]
        removed += [
            f"{path}: {name} constraint changed without a plan step"
            for name in sorted(set(old_decl) & set(new_decl))
            if old_decl[name] != new_decl[name] and name not in targets
        ]
    gates.append(_gate("no_dependency_removed", removed, "declared dependencies preserved"))

    bound = min(MAX_CHANGED_LINES_TOTAL, MAX_CHANGED_LINES_PER_STEP * max(1, len(applied)))
    gates.append(_gate("bounded_diff", [f"{changed_total} changed lines > {bound}"] if changed_total > bound else [],
                       f"{changed_total} changed line(s)"))

    unstable = [p for p in modified if before[p].count("\n") != after[p].count("\n")]
    gates.append(_gate("line_stability", [f"{p} line count changed" for p in unstable], "line numbers preserved"))

    syntax = [problem for p in modified if (problem := _syntax_problem(p, after[p])) is not None]
    gates.append(_gate("syntax", syntax, "all modified files parse"))

    obsolete = [
        problem for i in sorted(applied)
        if (problem := _obsolete_shape_problem(by_id[i], after.get(by_id[i].target.file_path, ""))) is not None
    ]
    gates.append(_gate("obsolete_shape_gone", obsolete, "migrated usages no longer match the obsolete shape"))
    return tuple(gates)


__all__ = ["MAX_CHANGED_LINES_PER_STEP", "run_safety_gates"]
