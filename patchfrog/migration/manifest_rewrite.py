"""Package version bumps in manifests (``requirements*.txt``,
``pyproject.toml``, ``package.json``).

Only single-clause constraints are rewritten (``==X``, ``>=X``, ``~=X``,
``^X``, ``~X``, ``X``); anything else (ranges, exclusions, URLs) needs a
human decision and fails the step. Lockfiles are never edited -- they
belong to the package manager. Only the one declaration line changes.
"""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath

from patchfrog.migration.domain import MigrationStep
from patchfrog.migration.edits import NotNeeded, RewriteError, TextEdit, line_of
from patchfrog.upstream.domain import normalize_package_name
from patchfrog.upstream.package_version import parse_version

_REQ_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._\-]*)(?P<extras>\[[^\]]*\])?[ \t]*(?P<spec>[^;#\n]*)", re.MULTILINE
)
_SINGLE_PY_SPEC_RE = re.compile(r"^(?P<op>===|==|>=|~=)\s*(?P<version>[A-Za-z0-9.+!\-*]+)\s*$")
_NPM_SPEC_RE = re.compile(r"^(?P<op>\^|~|>=|=)?(?P<version>\d[\w.+\-]*)$")


def _same(a: str, b: str) -> bool:
    return normalize_package_name(a) == normalize_package_name(b)


def _bump_spec(op: str, version: str, target: str) -> str:
    current, new = parse_version(version), parse_version(target)
    if current is not None and new is not None and current == new:
        raise NotNeeded(f"already requires {target}")
    return f"{op}{target}"


def _edit(step: MigrationStep, text: str, start: int, end: int, replacement: str) -> TextEdit:
    line = line_of(text, start)
    return TextEdit(step.target.file_path, start, end, replacement, step.step_id, (line, line))


def _requirements(step: MigrationStep, text: str, package: str, target: str) -> list[TextEdit]:
    for match in _REQ_RE.finditer(text):
        if not _same(match.group("name"), package):
            continue
        spec = match.group("spec").strip()
        if not spec:
            raise NotNeeded("unpinned: the new version is already allowed")
        single = _SINGLE_PY_SPEC_RE.match(spec)
        if single is None:
            raise RewriteError(f"constraint `{spec}` needs a human decision")
        start = match.start("spec") + match.group("spec").index(spec)
        replacement = _bump_spec(single.group("op"), single.group("version"), target)
        return [_edit(step, text, start, start + len(spec), replacement)]
    raise RewriteError(f"{package} is not declared in {step.target.file_path}")


def _pyproject(step: MigrationStep, text: str, package: str, target: str) -> list[TextEdit]:
    pep621 = re.compile(
        r"""(["'])(?P<name>[A-Za-z0-9][A-Za-z0-9._\-]*)(\[[^\]]*\])?\s*(?P<spec>[^"';]*?)\s*(?:;[^"']*)?\1"""
    )
    for match in pep621.finditer(text):
        if not _same(match.group("name"), package):
            continue
        spec = match.group("spec").strip()
        single = _SINGLE_PY_SPEC_RE.match(spec)
        if not spec:
            raise NotNeeded("unpinned: the new version is already allowed")
        if single is None:
            raise RewriteError(f"constraint `{spec}` needs a human decision")
        start = match.start("spec") + match.group("spec").index(spec)
        return [_edit(step, text, start, start + len(spec), _bump_spec(single.group("op"), single.group("version"),
                                                                       target))]
    poetry = re.compile(r"""^(?P<name>[A-Za-z0-9][A-Za-z0-9._\-]*)\s*=\s*(["'])(?P<spec>[^"']*)\2""", re.MULTILINE)
    for match in poetry.finditer(text):
        if not _same(match.group("name"), package):
            continue
        npm_like = _NPM_SPEC_RE.match(match.group("spec").strip())
        if npm_like is None:
            raise RewriteError(f"constraint `{match.group('spec')}` needs a human decision")
        return [_edit(step, text, match.start("spec"), match.end("spec"),
                      _bump_spec(npm_like.group("op") or "", npm_like.group("version"), target))]
    raise RewriteError(f"{package} is not declared in {step.target.file_path}")


_NPM_SECTIONS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")


def _package_json(step: MigrationStep, text: str, package: str, target: str) -> list[TextEdit]:
    try:
        before = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RewriteError("package.json does not parse") from exc
    sections = [s for s in _NPM_SECTIONS if isinstance(before.get(s), dict) and package in before[s]]
    if not sections:
        raise RewriteError(f"{package} is not declared in {step.target.file_path}")
    pattern = re.compile(r'"' + re.escape(package) + r'"\s*:\s*"(?P<spec>[^"]*)"')
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise RewriteError(f"{package} is declared more than once; a person must choose")
    match = matches[0]
    npm = _NPM_SPEC_RE.match(match.group("spec"))
    if npm is None:
        raise RewriteError(f"constraint `{match.group('spec')}` needs a human decision")
    replacement = _bump_spec(npm.group("op") or "", npm.group("version"), target)
    edit = _edit(step, text, match.start("spec"), match.end("spec"), replacement)
    after = json.loads(text[: edit.start] + replacement + text[edit.end :])
    changed = [s for s in _NPM_SECTIONS if before.get(s) != after.get(s)]
    if changed != sections[:1] or {k: v for k, v in before.items() if k not in _NPM_SECTIONS} != {
        k: v for k, v in after.items() if k not in _NPM_SECTIONS
    }:
        raise RewriteError("edit would change more than the one dependency")
    return [edit]


def manifest_edits(step: MigrationStep, text: str) -> list[TextEdit]:
    assert step.operation is not None and step.operation.kind == "bump_manifest_version"
    package, target = step.operation.get("package"), step.operation.get("version")
    name = PurePosixPath(step.target.file_path).name
    if name == "package.json":
        return _package_json(step, text, package, target)
    if name == "pyproject.toml":
        return _pyproject(step, text, package, target)
    if name.endswith((".txt", ".in")):
        return _requirements(step, text, package, target)
    raise RewriteError(f"{name} manifests are not rewritten")


__all__ = ["manifest_edits"]
