"""Safe, deterministic repository file enumeration for dependency
discovery.

- In a git checkout, only ``git ls-files`` paths are considered (respects
  ``.gitignore`` for free -- local ``.env`` files, virtualenvs and build
  output never appear). Otherwise the tree is walked.
- The same vendor/build/virtualenv denylist as indexing applies.
- Symlinks are never followed; oversized and binary files are skipped.
- **Secret-store-like files are never opened** -- matched by name alone
  (``.env*``, private keys, credential/secret files, ``.npmrc``,
  ``.pypirc``, ``.netrc``). They are counted, never read.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from patchfrog.indexing.inventory import is_denylisted_path
from patchfrog.repository.git import GitError, run_git

MAX_DISCOVERY_FILE_BYTES = 1024 * 1024
MAX_OPENAPI_FILE_BYTES = 5 * 1024 * 1024

_SECRET_NAME_RE = re.compile(
    r"^(\.env(\..*)?|\.npmrc|\.pypirc|\.netrc|\.git-credentials|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?"
    r"|.*\.(pem|key|p12|pfx|jks|keystore|kdbx)|(.*[._-])?(secret|secrets|credential|credentials)"
    r"(\.[a-z0-9]+)?|service[-_]?account.*\.json)$",
    re.IGNORECASE,
)


def is_secret_store_path(relative_path: str) -> bool:
    return bool(_SECRET_NAME_RE.match(PurePosixPath(relative_path).name))


@dataclass(frozen=True, slots=True)
class RepositoryFile:
    relative_path: str
    absolute_path: Path


@dataclass(slots=True)
class WalkStats:
    files_scanned: int = 0
    secret_store_files_skipped: int = 0


def _candidate_paths(root: Path) -> list[str]:
    if (root / ".git").exists():
        try:
            output = run_git(["-C", str(root), "ls-files", "-z"])
            return sorted(p for p in output.split("\0") if p)
        except GitError:
            pass
    paths: list[str] = []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative.startswith(".git/"):
            continue
        paths.append(relative)
    return sorted(paths)


def iter_repository_files(root: Path, stats: WalkStats | None = None) -> Iterator[RepositoryFile]:
    """Yield readable, non-secret, non-vendored files under ``root`` in a
    deterministic order."""

    stats = stats if stats is not None else WalkStats()
    resolved_root = root.resolve()
    for relative in _candidate_paths(root):
        if is_denylisted_path(relative):
            continue
        if is_secret_store_path(relative):
            stats.secret_store_files_skipped += 1
            continue
        absolute = root / relative
        if absolute.is_symlink() or not absolute.is_file():
            continue
        try:
            absolute.resolve().relative_to(resolved_root)
        except ValueError:
            continue
        stats.files_scanned += 1
        yield RepositoryFile(relative_path=relative, absolute_path=absolute)


def read_text(file: RepositoryFile, *, max_bytes: int = MAX_DISCOVERY_FILE_BYTES) -> str | None:
    """UTF-8 text, or ``None`` for oversized/binary/undecodable files."""

    try:
        if file.absolute_path.stat().st_size > max_bytes:
            return None
        data = file.absolute_path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


__all__ = [
    "MAX_DISCOVERY_FILE_BYTES",
    "MAX_OPENAPI_FILE_BYTES",
    "RepositoryFile",
    "WalkStats",
    "is_secret_store_path",
    "iter_repository_files",
    "read_text",
]
