"""Read-only access to evaluation baseline artifacts on disk.

No database -- evaluation results are file artifacts by default (see
the module docstring of :mod:`patchfrog.evaluation.reporting`). This
module is the one place both the CLI's ``eval compare``/``eval report``
commands and any future CI consumer look for a baseline; never a second,
divergent lookup path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from patchfrog.evaluation.reporting import read_json

DEFAULT_BASELINES_ROOT = Path(__file__).resolve().parents[2] / "evaluation_baselines"
DEFAULT_BASELINE_NAME = "phase8_baseline.json"


def default_baseline_path(baselines_root: Path = DEFAULT_BASELINES_ROOT) -> Path:
    return baselines_root / DEFAULT_BASELINE_NAME


def list_baselines(baselines_root: Path = DEFAULT_BASELINES_ROOT) -> list[Path]:
    if not baselines_root.is_dir():
        return []
    return sorted(baselines_root.glob("*.json"))


def load_baseline(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"no evaluation baseline at {path}")
    return read_json(path)
