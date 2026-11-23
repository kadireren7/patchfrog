"""Bounded benchmark of the ``patchfrog scale`` CLI (Milestone 2.5). A
developer tool, never a gate: single-host measurements for docs/scale-cli.md.

    python scripts/bench_scale_cli.py [--repo PATH ...] [--repeats N]

Measures, each in a fresh subprocess and a fresh temporary store:
* startup: ``python -m patchfrog scale --help`` (fast path) versus
  ``python -m patchfrog.cli scale --help`` (full CLI), median of N runs;
* for each repository: first ``scale analyze --json`` and the repeated
  (reused) one, wall time, each child's own peak RSS (getrusage in the
  child), and the size of the stored discovery.json artifact.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from patchfrog.app.discovery import DISCOVERY_ARTIFACT  # noqa: E402
from patchfrog.app.store import LocalRunStore  # noqa: E402

_SELF_RSS = (
    "import resource, runpy, sys\n"
    "sys.argv = sys.argv[1:]\n"
    "code = 0\n"
    "try:\n    runpy.run_module('patchfrog', run_name='__main__')\nexcept SystemExit as exc:\n    code = exc.code or 0\n"
    "sys.stderr.write('\\nRSS_KIB=%d\\n' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)\n"
    "sys.exit(code)\n"
)


def run(args: list[str]) -> tuple[float, int, subprocess.CompletedProcess[str]]:
    started = time.perf_counter()
    result = subprocess.run([sys.executable, "-m", *args], capture_output=True, text=True, check=False, cwd=ROOT)
    return time.perf_counter() - started, 0, result


def run_measured(args: list[str]) -> tuple[float, int, subprocess.CompletedProcess[str]]:
    """``python -m patchfrog ARGS`` with the child's own peak RSS (KiB)."""

    started = time.perf_counter()
    result = subprocess.run([sys.executable, "-c", _SELF_RSS, "patchfrog", *args], capture_output=True, text=True, check=False, cwd=ROOT)
    elapsed = time.perf_counter() - started
    rss = int(result.stderr.rsplit("RSS_KIB=", 1)[1].split()[0])
    return elapsed, rss, result


def startup(repeats: int) -> None:
    for label, args in (
        ("python -m patchfrog scale --help", ["patchfrog", "scale", "--help"]),
        ("python -m patchfrog.cli scale --help", ["patchfrog.cli", "scale", "--help"]),
    ):
        times = []
        for _ in range(repeats):
            elapsed, _peak, result = run(args)
            assert result.returncode == 0, result.stderr
            times.append(elapsed)
        print(f"startup  {label:40} median {statistics.median(times):.2f}s  (min {min(times):.2f}s, n={repeats})")


def analyze(repo: Path, extra: list[str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp) / "store"
        args = ["scale", "analyze", str(repo), "--store", str(store), "--json", *extra]
        first_s, first_rss, first = run_measured(args)
        assert first.returncode in (0, 3), first.stderr
        again_s, again_rss, again = run_measured(args)
        data, repeat = json.loads(first.stdout), json.loads(again.stdout)
        run_id = data["run"]["run_id"]
        size = LocalRunStore(store).get_run(run_id).artifacts[DISCOVERY_ARTIFACT].size
        model = data["model"]
        print(
            f"analyze  {repo.name or repo}{' ' + ' '.join(extra) if extra else ''}: state={data['run']['state']} "
            f"routes={len(model['routes'])} models={len(model['database']['models'])} complete={data['complete']} "
            f"first={first_s:.2f}s repeat={again_s:.2f}s (reused={repeat['run']['reused']}) "
            f"artifact={size / 1024:.1f} KiB peak_rss first={first_rss / 1024:.0f} MiB repeat={again_rss / 1024:.0f} MiB"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, action="append", default=[])
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    startup(args.repeats)
    fixture = ROOT / "tests" / "fixtures" / "scalability" / "database" / "cases" / "05_sessions_sync" / "repo"
    analyze(fixture, [])
    for repo in args.repo:
        analyze(repo, [])
        analyze(repo, ["--exclude", "tests/fixtures"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
