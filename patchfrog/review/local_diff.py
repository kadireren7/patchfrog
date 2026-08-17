"""Minimal local-checkout diff support for the CLI ``review`` command.

Production PR review (:mod:`apps.worker.tasks.review_pull_request`) uses
the diff GitHub's API already returns per file (see
:mod:`patchfrog.diff.parser`) -- this module exists only so the developer
CLI can drive the same :class:`~patchfrog.review.service.PullRequestReviewService`
against a local checkout, without a real pull request. It is a thin
wrapper around ``git diff`` and the existing unified-diff hunk parser;
renames are not specially handled (a renamed file is treated as its new
path only) since this path is for local development/dogfooding, not
production review.
"""

from __future__ import annotations

import re
from pathlib import Path

from patchfrog.diff.models import DiffFile
from patchfrog.diff.parser import build_diff_file
from patchfrog.repository.git import run_git

_FILE_HEADER_RE = re.compile(r"^diff --git a/(?P<a>.*) b/(?P<b>.*)$")


def diff_against_base(root_path: Path, base_ref: str) -> list[DiffFile]:
    """The set of changed files between ``base_ref`` and the current
    checkout's ``HEAD``, as normalized :class:`DiffFile` objects."""

    raw = run_git(["diff", "--no-color", "--unified=3", f"{base_ref}...HEAD"], cwd=root_path)
    return _parse_git_diff_text(raw)


def _parse_git_diff_text(raw: str) -> list[DiffFile]:
    files: list[DiffFile] = []
    current_path: str | None = None
    current_patch_lines: list[str] = []
    in_hunk = False

    def flush() -> None:
        if current_path is not None:
            patch = "\n".join(current_patch_lines) if current_patch_lines else None
            files.append(build_diff_file(current_path, patch))

    for line in raw.split("\n"):
        match = _FILE_HEADER_RE.match(line)
        if match:
            flush()
            current_path = match.group("b")
            current_patch_lines = []
            in_hunk = False
            continue
        if current_path is None:
            continue
        if line.startswith("@@"):
            in_hunk = True
        if in_hunk:
            current_patch_lines.append(line)

    flush()
    return files
