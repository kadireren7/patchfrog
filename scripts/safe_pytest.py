"""Resource-guarded, batched pytest runner for memory-constrained hosts.

A local developer tool (stdlib only; never used by CI, which runs the
plain suite). It exists because a single full ``pytest`` run on a small
host can be stopped by host memory pressure (see ``docs/ci-health.md`` ->
"Resource-constrained local runs"), and a stopped run must never be
mistaken for a pass.

    python scripts/safe_pytest.py plan   OUT_DIR
    python scripts/safe_pytest.py run    OUT_DIR [--start N] [--only N]
    python scripts/safe_pytest.py report OUT_DIR

* ``plan``   collects the suite once and groups test files into batches:
             files that spawn subprocesses (sandbox, git, nested pytest)
             run alone; others are grouped (<= 12 files, <= 200 tests).
* ``run``    runs batches strictly one at a time, each in its own pytest
             process and process group, with ``--junitxml`` per batch.
             It refuses to start a batch below ``MIN_START_MB`` of
             MemAvailable (default 1000) and kills the batch's whole
             process group and stops below ``KILL_MB`` (default 450).
             Resumable with ``--start``.
* ``report`` reconciles every collected node id against the per-batch
             JUnit results: passed / failed / error / skipped / not
             executed, plus ids that ran but were not in the collection.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MIN_START_MB = int(os.environ.get("MIN_START_MB", "1000"))
KILL_MB = int(os.environ.get("KILL_MB", "450"))
WAIT_FOR_MEMORY_S = 120
MAX_FILES_PER_BATCH = 12
MAX_TESTS_PER_BATCH = 200
_HEAVY_MARKERS = re.compile(
    r"run_sandboxed|bwrap|subprocess\.(run|Popen)|create_subprocess_exec|run_git|git_repo"
)
_PYTEST_ARGS = ("-q", "-p", "no:cacheprovider", "-p", "no:cov", "-p", "no:anyio")


def _env() -> dict[str, str]:
    return dict(os.environ, SEMGREP_ENABLE_VERSION_CHECK="0", SEMGREP_SEND_METRICS="off")


def mem_available_mb() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    raise RuntimeError("MemAvailable not found in /proc/meminfo")


def _tree_rss_mb(pgid: int) -> int:
    total_kb = 0
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            stat = (pid_dir / "stat").read_text()
            if int(stat[stat.rindex(")") + 2 :].split()[2]) != pgid:
                continue
            for line in (pid_dir / "status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    total_kb += int(line.split()[1])
        except (OSError, ValueError, IndexError):
            continue
    return total_kb // 1024


# --- plan -------------------------------------------------------------------


def cmd_plan(out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", *_PYTEST_ARGS],
        cwd=REPO, env=_env(), capture_output=True, text=True, check=False,
    )
    node_ids = [line.strip() for line in result.stdout.splitlines() if "::" in line]
    if result.returncode != 0 or not node_ids:
        print(result.stdout[-2000:], result.stderr[-2000:], sep="\n", file=sys.stderr)
        return 2
    counts: dict[str, int] = {}
    for node_id in node_ids:
        file_path = node_id.split("::", 1)[0]
        counts[file_path] = counts.get(file_path, 0) + 1

    batches: list[list[str]] = []
    current: list[str] = []
    current_tests = 0
    for file_path, n_tests in counts.items():
        if _HEAVY_MARKERS.search((REPO / file_path).read_text()):
            if current:
                batches.append(current)
                current, current_tests = [], 0
            batches.append([file_path])
            continue
        if current and (len(current) >= MAX_FILES_PER_BATCH or current_tests + n_tests > MAX_TESTS_PER_BATCH):
            batches.append(current)
            current, current_tests = [], 0
        current.append(file_path)
        current_tests += n_tests
    if current:
        batches.append(current)

    (out_dir / "collected.json").write_text(json.dumps(sorted(node_ids)))
    (out_dir / "plan.json").write_text(json.dumps({"batches": batches}, indent=1))
    print(f"{len(node_ids)} tests in {len(counts)} files -> {len(batches)} batches")
    return 0


# --- run --------------------------------------------------------------------


def _run_batch(index: int, files: list[str], out_dir: Path) -> dict[str, object]:
    deadline = time.monotonic() + WAIT_FOR_MEMORY_S
    while mem_available_mb() < MIN_START_MB:
        if time.monotonic() > deadline:
            return {"batch": index, "status": "not_started_low_memory", "mem_available_mb": mem_available_mb()}
        time.sleep(5)

    junit = out_dir / f"batch_{index:03d}.xml"
    cmd = [sys.executable, "-m", "pytest", *_PYTEST_ARGS, f"--junitxml={junit}", *files]
    started = time.monotonic()
    with (out_dir / f"batch_{index:03d}.log").open("w") as log:
        proc = subprocess.Popen(cmd, cwd=REPO, env=_env(), stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    stats = {"peak_rss": 0, "min_avail": mem_available_mb(), "killed": False}

    def watch() -> None:
        while proc.poll() is None:
            stats["peak_rss"] = max(stats["peak_rss"], _tree_rss_mb(proc.pid))
            available = mem_available_mb()
            stats["min_avail"] = min(stats["min_avail"], available)
            if available < KILL_MB:
                stats["killed"] = True
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                return
            time.sleep(0.5)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    exit_code = proc.wait()
    watcher.join(timeout=5)
    return {
        "batch": index,
        "status": "killed_low_memory" if stats["killed"] else "completed",
        "exit_code": exit_code,
        "seconds": round(time.monotonic() - started, 1),
        "peak_tree_rss_mb": stats["peak_rss"],
        "min_mem_available_mb": stats["min_avail"],
        "files": files,
    }


def cmd_run(out_dir: Path, start: int, only: int | None) -> int:
    batches: list[list[str]] = json.loads((out_dir / "plan.json").read_text())["batches"]
    end = len(batches) if only is None else min(len(batches), start + only)
    for index in range(start, end):
        result = _run_batch(index, batches[index], out_dir)
        with (out_dir / "ledger.jsonl").open("a") as ledger:
            ledger.write(json.dumps(result) + "\n")
        print(json.dumps({k: v for k, v in result.items() if k != "files"}), flush=True)
        if result["status"] != "completed":
            print(f"STOPPED at batch {index}: {result['status']} (resume with --start {index})", flush=True)
            return 3
    print("DONE", flush=True)
    return 0


# --- report -----------------------------------------------------------------


def _junit_key(classname: str, name: str) -> tuple[str, str]:
    return classname, name


def _collected_key(node_id: str) -> tuple[str, str]:
    # Split on "::" only outside the parametrize id: ids such as
    # "test_x[https://[::1]/x]" contain "::" themselves.
    base, bracket, param = node_id.partition("[")
    parts = base.split("::")
    module = parts[0].removesuffix(".py").replace("/", ".")
    return ".".join([module, *parts[1:-1]]), parts[-1] + bracket + param


def cmd_report(out_dir: Path) -> int:
    collected = json.loads((out_dir / "collected.json").read_text())
    expected = {_collected_key(n): n for n in collected}
    outcome: dict[tuple[str, str], str] = {}
    duplicates: list[str] = []
    for junit in sorted(out_dir.glob("batch_*.xml")):
        for case in ET.parse(junit).getroot().iter("testcase"):
            key = _junit_key(case.get("classname", ""), case.get("name", ""))
            if key in outcome:
                duplicates.append("::".join(key))
            if case.find("failure") is not None:
                status = "failed"
            elif case.find("error") is not None:
                status = "error"
            elif case.find("skipped") is not None:
                status = "skipped"
            else:
                status = "passed"
            outcome[key] = status

    totals = {"collected": len(expected), "passed": 0, "failed": 0, "error": 0, "skipped": 0, "not_executed": 0}
    lists: dict[str, list[str]] = {"failed": [], "error": [], "not_executed": []}
    for key, node_id in sorted(expected.items(), key=lambda item: item[1]):
        status = outcome.get(key, "not_executed")
        totals[status] += 1
        if status in lists:
            lists[status].append(node_id)
    unexpected = sorted("::".join(k) for k in outcome if k not in expected)
    report = {**totals, "ran_but_not_collected": unexpected, "duplicates": duplicates, **lists}
    (out_dir / "report.json").write_text(json.dumps(report, indent=1))
    print(json.dumps({k: v for k, v in report.items() if not isinstance(v, list)}))
    for name in ("failed", "error", "not_executed", "ran_but_not_collected", "duplicates"):
        entries = report[name]
        assert isinstance(entries, list)
        print(f"{name}: {len(entries)}")
        for entry in entries[:50]:
            print(f"  {entry}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="safe_pytest")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "run", "report"):
        p = sub.add_parser(name)
        p.add_argument("out_dir", type=Path)
        if name == "run":
            p.add_argument("--start", type=int, default=0)
            p.add_argument("--only", type=int, default=None)
    args = parser.parse_args()
    if args.command == "plan":
        return cmd_plan(args.out_dir)
    if args.command == "run":
        return cmd_run(args.out_dir, args.start, args.only)
    return cmd_report(args.out_dir)


if __name__ == "__main__":
    sys.exit(main())
