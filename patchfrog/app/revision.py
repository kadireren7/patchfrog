"""RepositoryRevision: a deterministic identity for the analysed repository
state (Milestone 2.1, docs/architecture/LOCAL_STORE.md).

What is identified is the **analysed working-tree snapshot**, not a Git
commit: the exact bytes of every in-scope file at capture time. A commit
SHA, when present, is recorded as *provenance* only. Two snapshots with the
same in-scope bytes and the same scope have the same ``revision_id``,
whether they come from a Git checkout, a plain directory, another clone
location, or another machine. Any change to an in-scope file's bytes, any
added or removed in-scope file, or any change of scope gives a different id.

Scope
-----
* Git work tree: tracked files plus (by default) untracked files that are
  not ignored (``git ls-files --cached --others --exclude-standard``).
  ``.gitignore`` is honoured. Tracked files deleted from the work tree are
  simply absent.
* Plain directory: every regular file under the root (an ``os.walk`` that
  never follows symlinks), with ``.git`` directories pruned.
* Both: the shared vendor/build/virtualenv denylist
  (:func:`patchfrog.indexing.inventory.is_denylisted_path`) is pruned, and
  secret-store-like files (:func:`patchfrog.dependencies.files.is_secret_store_path`)
  are **never opened** and are not part of the identity. ``exclude_prefixes``
  removes further subtrees (use it for a store directory placed inside the
  root, so an identity never depends on its own artifacts).

Safety
------
Repository content is untrusted. Nothing is executed or imported. Git is
asked only to *list* (``rev-parse``, ``ls-files``, ``ls-tree``) with hooks,
fsmonitor and the untracked cache disabled; ``git status``/``hash-object``
are never used because they can run repository-configured filters or
fsmonitor commands. Files are opened component by component relative to
the root with ``O_NOFOLLOW``, so a symlink anywhere in a path (including
one swapped in during capture) is skipped and nothing outside the root is
read. Non-regular files (FIFOs, devices, sockets, submodule directories)
are skipped without blocking.

Consistency
-----------
Each file is hashed from one open descriptor, with ``fstat`` before and
after the read. After all files are hashed, the file list and every
file's ``(size, mtime_ns, ctime_ns, inode)`` are re-checked (and ``HEAD``
re-read). Any difference means the tree changed during capture: the
capture is retried once, then :class:`RevisionUnstableError` is raised. A
wrong identity is never returned. Granularity is that of the filesystem's
timestamps; a same-size rewrite within one timestamp tick between the two
stat calls cannot be observed (documented limitation).

Bounded cost
------------
One streaming read per in-scope file (1 MiB chunks), with memory
independent of file sizes. ``max_files`` and ``max_total_bytes`` are
checked *before* and *during* hashing and fail fast with
:class:`RevisionBudgetExceeded`. Budgets are not part of the identity (they
can only abort a capture, never change its result).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath

from patchfrog.dependencies.files import is_secret_store_path
from patchfrog.indexing.inventory import is_denylisted_path
from patchfrog.repository.git import GitError, run_git

REVISION_SCHEMA_VERSION = 1
#: Bump whenever the scope selection rules change (it is part of the identity).
SCOPE_ALGORITHM_VERSION = 1

_CHUNK_BYTES = 1 << 20
_GIT_LIST_ONLY = ["-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false"]
_REGULAR_GIT_MODES = frozenset({"100644", "100755"})


class RevisionKind(StrEnum):
    GIT = "git"
    DIRECTORY = "directory"


class RevisionCaptureError(RuntimeError):
    """A consistent revision could not be captured."""


class RevisionBudgetExceeded(RevisionCaptureError):
    """The in-scope tree exceeds ``max_files`` or ``max_total_bytes``."""


class RevisionUnstableError(RevisionCaptureError):
    """The tree changed while it was being captured (after the retry)."""


def _normalize_prefix(prefix: str) -> str:
    cleaned = prefix.strip().rstrip("/")
    parts = PurePosixPath(cleaned).parts
    if not cleaned or cleaned.startswith("/") or "\\" in cleaned or any(p in ("..", ".") for p in parts):
        raise ValueError(f"exclude prefix must be a relative POSIX path without '..': {prefix!r}")
    return "/".join(parts)


@dataclass(frozen=True)
class RevisionScope:
    """Which files are part of the snapshot, plus capture budgets."""

    include_untracked: bool = True
    exclude_prefixes: tuple[str, ...] = ()
    max_files: int = 200_000
    max_total_bytes: int = 2 * 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        normalized = tuple(sorted({_normalize_prefix(p) for p in self.exclude_prefixes}))
        object.__setattr__(self, "exclude_prefixes", normalized)
        if self.max_files < 1 or self.max_total_bytes < 0:
            raise ValueError("revision budgets must be positive")

    def fingerprint(self) -> str:
        """Identity-relevant part of the scope (budgets are excluded)."""

        canonical = json.dumps(
            {
                "algorithm": SCOPE_ALGORITHM_VERSION,
                "include_untracked": self.include_untracked,
                "exclude_prefixes": list(self.exclude_prefixes),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def excludes(self, relative_path: str) -> bool:
        return any(relative_path == p or relative_path.startswith(p + "/") for p in self.exclude_prefixes)


@dataclass(frozen=True, order=True)
class RevisionEntry:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class RevisionChanges:
    """In-scope differences between the snapshot and ``HEAD`` (raw bytes).

    Files whose Git attributes transform content (EOL conversion, LFS,
    clean/smudge filters) can appear as ``modified`` although ``git status``
    calls them clean: the comparison is deliberately on raw working-tree
    bytes, without running any filter."""

    modified: int = 0
    added: int = 0
    deleted: int = 0


@dataclass(frozen=True)
class RevisionStats:
    skipped_symlinks: int = 0
    skipped_non_regular: int = 0
    skipped_secret_files: int = 0
    skipped_undecodable_names: int = 0
    #: Files excluded by ``exclude_prefixes`` (in a plain directory, a pruned
    #: excluded subtree counts once: its contents are never even listed).
    excluded_by_scope: int = 0
    capture_attempts: int = 1


@dataclass(frozen=True)
class RepositoryRevision:
    schema_version: int
    revision_id: str
    kind: RevisionKind
    #: Provenance only (``None`` for plain directories and unborn branches).
    commit_sha: str | None
    #: ``None`` for plain directories (no baseline to compare with).
    dirty: bool | None
    changes: RevisionChanges | None
    scope_fingerprint: str
    file_count: int
    total_bytes: int
    entries: tuple[RevisionEntry, ...] = field(repr=False)
    stats: RevisionStats = field(default_factory=RevisionStats)

    def to_dict(self, *, include_entries: bool = True) -> dict[str, object]:
        data: dict[str, object] = {
            "schema_version": self.schema_version,
            "revision_id": self.revision_id,
            "kind": self.kind.value,
            "commit_sha": self.commit_sha,
            "dirty": self.dirty,
            "changes": None if self.changes is None else vars(self.changes).copy(),
            "scope_fingerprint": self.scope_fingerprint,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "stats": vars(self.stats).copy(),
        }
        if include_entries:
            data["entries"] = [[e.path, e.size, e.sha256] for e in self.entries]
        return data


# --- listing -----------------------------------------------------------------


@dataclass
class _Listing:
    paths: list[str]
    secret: int = 0
    excluded: int = 0
    undecodable: int = 0


def _encodable(path: str) -> bool:
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _select(candidates: list[str], scope: RevisionScope) -> _Listing:
    listing = _Listing(paths=[])
    for relative in sorted(set(candidates)):
        if not _encodable(relative):
            listing.undecodable += 1
        elif scope.excludes(relative):
            listing.excluded += 1
        elif is_denylisted_path(relative):
            continue
        elif is_secret_store_path(relative):
            listing.secret += 1
        else:
            listing.paths.append(relative)
    return listing


def _list_directory(root: Path, scope: RevisionScope) -> _Listing:
    candidates: list[str] = []
    pruned_by_scope = 0
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        relative_dir = os.path.relpath(current, root)
        prefix = "" if relative_dir == "." else Path(relative_dir).as_posix() + "/"
        kept = []
        for name in sorted(dirnames):
            rel = prefix + name
            if scope.excludes(rel):
                pruned_by_scope += 1
                continue
            if name == ".git" or is_denylisted_path(rel + "/x"):
                continue
            kept.append(name)
        dirnames[:] = kept  # prune in place: never descend into excluded trees
        candidates.extend(prefix + name for name in filenames)
    listing = _select(candidates, scope)
    listing.excluded += pruned_by_scope
    return listing


def _git(root: Path, *args: str) -> str:
    return run_git([*_GIT_LIST_ONLY, "-C", str(root), *args])


def _git_listing(root: Path, scope: RevisionScope) -> _Listing:
    args = ["ls-files", "-z", "--cached"]
    if scope.include_untracked:
        args += ["--others", "--exclude-standard"]
    output = _git(root, *args)
    return _select([p for p in output.split("\0") if p], scope)


def _git_head(root: Path) -> str | None:
    try:
        return _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}").strip() or None
    except GitError:
        return None  # unborn branch: no commit yet


def _git_object_format(root: Path) -> str:
    try:
        value = _git(root, "rev-parse", "--show-object-format").strip()
    except GitError:
        return "sha1"
    return value if value in ("sha1", "sha256") else "sha1"


def _git_head_blobs(root: Path, scope: RevisionScope) -> dict[str, str]:
    output = _git(root, "ls-tree", "-r", "-z", "HEAD", "--", ".")
    blobs: dict[str, str] = {}
    for record in output.split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        mode, _type, oid = meta.split(" ")
        if mode in _REGULAR_GIT_MODES:
            blobs[path] = oid
    selected = set(_select(list(blobs), scope).paths)
    return {p: oid for p, oid in blobs.items() if p in selected}


def _detect_git(root: Path) -> bool:
    try:
        inside = _git(root, "rev-parse", "--is-inside-work-tree").strip()
    except GitError as exc:
        if "not a git repository" in str(exc).lower():
            return False
        raise RevisionCaptureError(f"git could not inspect {root.name!r}: {exc}") from exc
    if inside != "true":
        raise RevisionCaptureError("the root is inside a git directory but not inside a work tree")
    return True


# --- safe reading ------------------------------------------------------------------


@dataclass(frozen=True)
class _Hashed:
    entry: RevisionEntry
    git_oid: str | None
    signature: tuple[int, int, int, int]


class _Skip(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _skip_reason(exc: OSError) -> str:
    return "symlink" if exc.errno == errno.ELOOP else "missing"


def _open_dir_beneath(root_fd: int, relative_dir: str) -> int:
    """Open directory ``relative_dir`` (``""`` = root) under ``root_fd``,
    one component at a time, never following a symlink."""

    dir_fd = os.dup(root_fd)
    for part in [p for p in relative_dir.split("/") if p]:
        try:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
        except OSError as exc:
            os.close(dir_fd)
            raise _Skip(_skip_reason(exc)) from exc
        os.close(dir_fd)
        dir_fd = next_fd
    return dir_fd


def _open_beneath(root_fd: int, relative: str) -> int:
    """Open file ``relative`` under ``root_fd`` without following any
    symlink (``O_NONBLOCK`` so a FIFO can never block the capture)."""

    parent, _, name = relative.rpartition("/")
    dir_fd = _open_dir_beneath(root_fd, parent)
    try:
        return os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError as exc:
        raise _Skip(_skip_reason(exc)) from exc
    finally:
        os.close(dir_fd)


def _signature(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)


def _hash_file(root_fd: int, relative: str, oid_algorithm: str | None, budget_left: int) -> _Hashed:
    fd = _open_beneath(root_fd, relative)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise _Skip("non_regular")
        if before.st_size > budget_left:
            raise RevisionBudgetExceeded("max_total_bytes exceeded while hashing")
        content = hashlib.sha256()
        oid = None
        if oid_algorithm is not None:
            oid = hashlib.new(oid_algorithm)
            oid.update(b"blob %d\0" % before.st_size)
        read = 0
        while chunk := os.read(fd, _CHUNK_BYTES):
            read += len(chunk)
            if read > before.st_size:
                raise RevisionUnstableError(f"{relative} grew while it was being read")
            content.update(chunk)
            if oid is not None:
                oid.update(chunk)
        after = os.fstat(fd)
        if read != before.st_size or _signature(after) != _signature(before):
            raise RevisionUnstableError(f"{relative} changed while it was being read")
    finally:
        os.close(fd)
    return _Hashed(
        entry=RevisionEntry(relative, before.st_size, content.hexdigest()),
        git_oid=None if oid is None else oid.hexdigest(),
        signature=_signature(before),
    )


def _lstat_signature(root_fd: int, relative: str) -> tuple[int, int, int, int] | None:
    parent, _, name = relative.rpartition("/")
    try:
        fd = _open_dir_beneath(root_fd, parent)
    except _Skip:
        return None
    try:
        return _signature(os.stat(name, dir_fd=fd, follow_symlinks=False))
    except OSError:
        return None
    finally:
        os.close(fd)


# --- capture ---------------------------------------------------------------------


def _revision_id(scope_fingerprint: str, entries: list[RevisionEntry]) -> str:
    digest = hashlib.sha256()
    digest.update(b"patchfrog.revision\0%d\0" % REVISION_SCHEMA_VERSION)
    digest.update(scope_fingerprint.encode("ascii") + b"\0")
    for entry in entries:
        digest.update(entry.path.encode("utf-8") + b"\0%d\0" % entry.size + entry.sha256.encode("ascii") + b"\n")
    return digest.hexdigest()


def _capture_once(root: Path, scope: RevisionScope, is_git: bool) -> RepositoryRevision:
    head = _git_head(root) if is_git else None
    oid_algorithm = _git_object_format(root) if is_git else None
    listing = _git_listing(root, scope) if is_git else _list_directory(root, scope)
    if len(listing.paths) > scope.max_files:
        raise RevisionBudgetExceeded(f"{len(listing.paths)} in-scope files exceed max_files={scope.max_files}")

    hashed: list[_Hashed] = []
    skipped = {"symlink": 0, "non_regular": 0}
    total = 0
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for relative in listing.paths:
            try:
                item = _hash_file(root_fd, relative, oid_algorithm, scope.max_total_bytes - total)
            except _Skip as skip:
                if skip.reason in skipped:
                    skipped[skip.reason] += 1
                continue  # "missing": a tracked file deleted from the work tree
            total += item.entry.size
            hashed.append(item)

        # Consistency: the listing and every hashed file must be unchanged.
        relisted = _git_listing(root, scope) if is_git else _list_directory(root, scope)
        if relisted.paths != listing.paths:
            raise RevisionUnstableError("files were added or removed during capture")
        for item in hashed:
            if _lstat_signature(root_fd, item.entry.path) != item.signature:
                raise RevisionUnstableError(f"{item.entry.path} changed during capture")
        if is_git and _git_head(root) != head:
            raise RevisionUnstableError("HEAD moved during capture")
    finally:
        os.close(root_fd)

    entries = [h.entry for h in hashed]  # already sorted: listing is sorted
    changes: RevisionChanges | None = None
    dirty: bool | None = None
    if is_git:
        head_blobs = _git_head_blobs(root, scope) if head is not None else {}
        present = {h.entry.path: h.git_oid for h in hashed}
        changes = RevisionChanges(
            modified=sum(1 for p, oid in present.items() if p in head_blobs and head_blobs[p] != oid),
            added=sum(1 for p in present if p not in head_blobs),
            deleted=sum(1 for p in head_blobs if p not in present),
        )
        dirty = bool(changes.modified or changes.added or changes.deleted)

    fingerprint = scope.fingerprint()
    return RepositoryRevision(
        schema_version=REVISION_SCHEMA_VERSION,
        revision_id=_revision_id(fingerprint, entries),
        kind=RevisionKind.GIT if is_git else RevisionKind.DIRECTORY,
        commit_sha=head,
        dirty=dirty,
        changes=changes,
        scope_fingerprint=fingerprint,
        file_count=len(entries),
        total_bytes=total,
        entries=tuple(entries),
        stats=RevisionStats(
            skipped_symlinks=skipped["symlink"],
            skipped_non_regular=skipped["non_regular"],
            skipped_secret_files=listing.secret,
            skipped_undecodable_names=listing.undecodable,
            excluded_by_scope=listing.excluded,
        ),
    )


def capture_revision(
    root: Path,
    scope: RevisionScope | None = None,
    *,
    exclude_paths: tuple[Path, ...] = (),
    attempts: int = 2,
) -> RepositoryRevision:
    """Capture the analysed snapshot of ``root``.

    ``exclude_paths`` are absolute paths (e.g. a store directory) that are
    excluded when they lie inside ``root``; paths outside are ignored.
    Raises :class:`RevisionCaptureError` (or a subclass) instead of ever
    returning an identity that does not match one consistent state.
    """

    scope = scope or RevisionScope()
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise RevisionCaptureError(f"cannot resolve the analysis root: {exc}") from exc
    if not resolved.is_dir():
        raise RevisionCaptureError("the analysis root must be a directory")
    extra = []
    for path in exclude_paths:
        try:
            extra.append(path.resolve().relative_to(resolved).as_posix())
        except ValueError:
            continue  # outside the root: nothing to exclude
    if "." in extra:
        raise RevisionCaptureError("an excluded path contains the whole analysis root")
    if extra:
        scope = replace(scope, exclude_prefixes=(*scope.exclude_prefixes, *extra))

    try:
        is_git = _detect_git(resolved)
    except UnicodeDecodeError as exc:  # pragma: no cover - exotic git output encodings
        raise RevisionCaptureError(f"git produced undecodable output: {exc}") from exc

    last_error: RevisionUnstableError | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            revision = _capture_once(resolved, scope, is_git)
        except RevisionUnstableError as exc:
            last_error = exc
            continue
        except GitError as exc:
            raise RevisionCaptureError(f"git listing failed: {exc}") from exc
        except UnicodeDecodeError as exc:
            raise RevisionCaptureError(f"a path is not valid UTF-8 in git output: {exc}") from exc
        if attempt > 1:
            revision = replace(revision, stats=replace(revision.stats, capture_attempts=attempt))
        return revision
    raise RevisionUnstableError(f"tree kept changing during capture ({attempts} attempts): {last_error}")


class RevisionEntryChanged(RevisionCaptureError):
    """A file no longer matches its revision entry (changed, replaced by a
    symlink or a non-regular file, removed, or larger than allowed)."""


def read_revision_file(root: Path, entry: RevisionEntry, *, max_bytes: int) -> bytes:
    """Read the bytes ``entry`` identifies, or raise :class:`RevisionEntryChanged`.

    The file is opened exactly like the capture opens it (component by
    component, ``O_NOFOLLOW``), so a symlink can never redirect the read
    outside ``root``, and its size and sha256 must equal the entry's. A
    caller therefore only ever analyses content that is part of the
    revision it reports (Milestone 2.3)."""

    if max_bytes < 0:
        raise ValueError("max_bytes must be >= 0")
    if entry.size > max_bytes:
        raise RevisionEntryChanged(f"{entry.path}: {entry.size} bytes exceed the read limit of {max_bytes}")
    try:
        resolved = root.resolve(strict=True)
        root_fd = os.open(resolved, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise RevisionEntryChanged(f"cannot open the analysis root: {exc.strerror}") from exc
    try:
        try:
            fd = _open_beneath(root_fd, entry.path)
        except _Skip as exc:
            raise RevisionEntryChanged(f"{entry.path}: {exc.reason}") from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RevisionEntryChanged(f"{entry.path}: non_regular")
            chunks: list[bytes] = []
            read = 0
            while chunk := os.read(fd, _CHUNK_BYTES):
                read += len(chunk)
                if read > entry.size:
                    raise RevisionEntryChanged(f"{entry.path}: changed since capture")
                chunks.append(chunk)
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)
    data = b"".join(chunks)
    if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
        raise RevisionEntryChanged(f"{entry.path}: changed since capture")
    return data


__all__ = [
    "REVISION_SCHEMA_VERSION",
    "RepositoryRevision",
    "RevisionBudgetExceeded",
    "RevisionCaptureError",
    "RevisionChanges",
    "RevisionEntry",
    "RevisionEntryChanged",
    "RevisionKind",
    "RevisionScope",
    "RevisionStats",
    "RevisionUnstableError",
    "capture_revision",
    "read_revision_file",
]
