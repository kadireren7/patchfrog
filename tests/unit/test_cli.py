from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from patchfrog.cli import _run_index


def _args(repository: Path) -> argparse.Namespace:
    return argparse.Namespace(repository=repository, full_name=None)


def test_run_index_rejects_a_missing_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "does-not-exist"

    exit_code = _run_index(_args(missing))

    assert exit_code == 1
    assert "not a directory" in capsys.readouterr().err


def test_run_index_rejects_a_non_git_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()

    exit_code = _run_index(_args(plain_dir))

    assert exit_code == 1
    assert "not a git repository" in capsys.readouterr().err
