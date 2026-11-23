"""Bounded, reproducible local benchmark for RepositoryRevision and
LocalRunStore (Milestone 2.1). Developer tool: never run by CI or pytest
(TEST_STRATEGY section 3 forbids timing assertions in tests).

    python scripts/bench_local_store.py WORK_DIR [--files 20000] [--stream-mib 256]

Creates everything under WORK_DIR (which must not exist), prints one JSON
document of measurements, and deletes WORK_DIR afterwards. Numbers depend on
the host (CPU, disk, page cache); record them together with the host notes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import shutil
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from patchfrog.app.revision import capture_revision
from patchfrog.app.store import LocalRunStore, RunState
from patchfrog.repository.git import run_git


def timed[T](fn: Callable[[], T]) -> tuple[T, float]:
    start = time.perf_counter()
    value = fn()
    return value, round(time.perf_counter() - start, 3)


def make_tree(root: Path, files: int) -> int:
    total = 0
    for i in range(files):
        path = root / f"pkg{i % 200}" / f"sub{i % 7}" / f"module_{i}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = (f"def f_{i}(x):\n    return x + {i}\n" * 40).encode()
        path.write_bytes(body)
        total += len(body)
    return total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("work_dir", type=Path)
    parser.add_argument("--files", type=int, default=20_000)
    parser.add_argument("--stream-mib", type=int, default=256)
    args = parser.parse_args()
    work: Path = args.work_dir
    work.mkdir(parents=True)
    results: dict[str, object] = {
        "host": {"python": platform.python_version(), "platform": platform.platform(), "cpus": os.cpu_count()},
        "files": args.files,
    }
    try:
        tree = work / "tree"
        results["tree_bytes"] = make_tree(tree, args.files)

        first, results["directory_first_capture_s"] = timed(lambda: capture_revision(tree))
        again, results["directory_repeat_capture_s"] = timed(lambda: capture_revision(tree))
        results["directory_identity_stable"] = first.revision_id == again.revision_id

        run_git(["-C", str(tree), "init", "--quiet"])
        run_git(["-C", str(tree), "-c", "user.email=b@b", "-c", "user.name=b", "add", "-A"])
        run_git(["-C", str(tree), "-c", "user.email=b@b", "-c", "user.name=b", "commit", "--quiet", "-m", "b"])
        clean, results["git_clean_capture_s"] = timed(lambda: capture_revision(tree))
        (tree / "pkg0" / "sub0" / "module_0.py").write_text("changed\n")
        dirty, results["git_dirty_capture_s"] = timed(lambda: capture_revision(tree))
        results["git_clean_matches_directory_identity"] = clean.revision_id == first.revision_id
        results["git_dirty_changes"] = vars(dirty.changes) if dirty.changes else None

        store = LocalRunStore(work / "store", gc_grace_seconds=0)
        payloads = [hashlib.sha256(str(i).encode()).digest() * 64 for i in range(1000)]  # 2 KiB each
        _, results["store_put_1000_small_s"] = timed(lambda: [store.put_bytes(p) for p in payloads])
        _, results["store_put_1000_duplicates_s"] = timed(lambda: [store.put_bytes(p) for p in payloads])

        chunk = os.urandom(1 << 20)

        def stream() -> Iterator[bytes]:
            for _ in range(args.stream_mib):
                yield chunk

        ref, seconds = timed(lambda: store.put_stream(stream()))
        results["store_stream_put_mib_per_s"] = round(args.stream_mib / seconds, 1) if seconds else None
        ok, seconds = timed(lambda: store.verify_artifact(ref))
        results["store_stream_verify_mib_per_s"] = round(args.stream_mib / seconds, 1) if seconds and ok else None

        def many_runs() -> None:
            for i in range(500):
                key = hashlib.sha256(f"k{i}".encode()).hexdigest()
                store.begin_run(idempotency_key=key, revision_id=first.revision_id, kind="analysis").finish(
                    RunState.SUCCEEDED
                )

        _, results["store_500_runs_begin_finish_s"] = timed(many_runs)
        _, results["store_recover_scan_500_runs_s"] = timed(store.recover)
        _, results["store_verify_full_s"] = timed(store.verify)
        _, results["store_prune_to_last_10_s"] = timed(lambda: store.prune(keep_last=10, older_than_seconds=0))
        results["peak_rss_mib"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    json.dump(results, sys.stdout, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
