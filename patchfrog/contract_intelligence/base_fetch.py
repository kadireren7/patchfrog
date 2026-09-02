"""Bounded, read-only fetch + in-memory parse of a PR's *base-commit*
file content -- the one genuinely new I/O primitive this milestone
needs (see ``validation/contract_intelligence/latest-summary.md``
section 1, "Can base/head symbols be compared without a second index?").

Never writes to the database, never builds a second
:class:`~patchfrog.persistence.models.repository_index.RepositoryIndexModel`
row, never re-resolves calls/imports -- this is a pure, ephemeral,
per-review-run computation over exactly the files a contract-eligible
changed candidate lives in (bounded by
:data:`patchfrog.contract_intelligence.domain.MAX_BASE_FILES_FETCHED`),
using the *same* parser (:mod:`patchfrog.parsing.python`) already used
for indexing -- never a second parsing engine.

Two fetch strategies, matching the two shapes
:meth:`patchfrog.review.service.PullRequestReviewService.review_pull_request`/
``review_local`` already use elsewhere in this codebase:

- production (a real GitHub PR): :func:`patchfrog.repository.file_contents.read_files_at_commit`
  (a targeted, shallow ``git fetch`` + ``git show`` -- no working-tree
  checkout).
- local/CLI (:mod:`patchfrog.review.local_diff`): a direct ``git show``
  against the checkout already on disk -- cheaper, no fetch needed,
  since the base commit is already local history.

Both fail closed to ``None`` per path on any error (network, missing
ref, binary/non-UTF-8 content) -- a contract check for that file is
then simply skipped, never guessed.
"""

from __future__ import annotations

from pathlib import Path

from patchfrog.contract_intelligence.domain import MAX_BASE_FILES_FETCHED
from patchfrog.domain.code import Language, ParsedSymbol
from patchfrog.parsing.registry import default_registry
from patchfrog.repository.file_contents import read_files_at_commit
from patchfrog.repository.git import GitError, run_git


def fetch_base_file_contents(
    *,
    local: bool,
    base_sha: str,
    paths: frozenset[str],
    root_path: Path | None = None,
    clone_url: str | None = None,
    token: str | None = None,
) -> dict[str, str | None]:
    """Fetch each of ``paths``'s exact blob content at ``base_sha``.
    Bounded to :data:`MAX_BASE_FILES_FETCHED` files -- a caller asking
    for more than that is itself a bug (this milestone's candidate
    filtering should never produce that many), so the excess is simply
    never fetched rather than silently truncated without a trace."""

    bounded = frozenset(sorted(paths)[:MAX_BASE_FILES_FETCHED])
    if not bounded:
        return {}

    if local:
        if root_path is None:
            return dict.fromkeys(bounded)
        return _read_local(root_path=root_path, base_sha=base_sha, paths=bounded)

    if clone_url is None:
        return dict.fromkeys(bounded)
    return read_files_at_commit(clone_url=clone_url, commit_sha=base_sha, paths=bounded, token=token)


def _read_local(*, root_path: Path, base_sha: str, paths: frozenset[str]) -> dict[str, str | None]:
    result: dict[str, str | None] = dict.fromkeys(paths)
    for path in paths:
        try:
            result[path] = run_git(["-C", str(root_path), "show", f"{base_sha}:{path}"])
        except (GitError, UnicodeDecodeError):
            result[path] = None
    return result


def parse_base_symbols(
    *, file_contents: dict[str, str | None], language: Language = Language.PYTHON
) -> dict[str, dict[str, ParsedSymbol]]:
    """For each fetched file, parse its base content with the same
    parser used at indexing time and index the resulting symbols by
    ``qualified_name`` -- ``{file_path: {qualified_name: ParsedSymbol}}``.
    A file whose content is ``None`` (fetch failed, deleted, binary) is
    simply absent from the result, never a fabricated empty parse."""

    parser = default_registry().get(language)
    if parser is None:
        return {}

    out: dict[str, dict[str, ParsedSymbol]] = {}
    for path, content in file_contents.items():
        if content is None:
            continue
        try:
            parsed = parser.parse_file(relative_path=path, content=content.encode("utf-8"))
        except Exception:  # a parse failure on base content must never crash the review
            continue
        out[path] = {s.qualified_name: s for s in parsed.symbols}
    return out
