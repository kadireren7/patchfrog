from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from patchfrog.analysis.analyzers.base import resolve_analyzer_binary


def test_resolves_console_script_beside_active_interpreter_when_not_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    interpreter = tmp_path / "python"
    analyzer = tmp_path / "example-analyzer"
    interpreter.write_text("")
    analyzer.write_text("#!/bin/sh\n")
    analyzer.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(interpreter))
    monkeypatch.setenv("PATH", "")

    assert resolve_analyzer_binary("example-analyzer") == str(analyzer)


def test_non_executable_sibling_is_not_reported_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    interpreter = tmp_path / "python"
    analyzer = tmp_path / "example-analyzer"
    interpreter.write_text("")
    analyzer.write_text("not executable")
    analyzer.chmod(0o644)
    monkeypatch.setattr(sys, "executable", str(interpreter))
    monkeypatch.setenv("PATH", "")

    assert not os.access(analyzer, os.X_OK)
    assert resolve_analyzer_binary("example-analyzer") is None
