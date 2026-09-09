from __future__ import annotations

from pathlib import Path

from patchfrog.domain.code import Language
from patchfrog.fix_verification.static_recheck import recheck_static_finding

_BAD = "def f():\n    return undefined_name\n"
_GOOD = "def f():\n    return 1\n"


async def test_recheck_static_finding_still_fires_when_code_unchanged(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(_BAD)
    result = await recheck_static_finding(
        checkout_path=tmp_path, file_path="m.py", source_analyzer="ruff", rule_id="F821",
        original_start_line=2, original_end_line=2, language=Language.PYTHON,
    )
    assert result is True


async def test_recheck_static_finding_no_longer_fires_when_code_fixed(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(_GOOD)
    result = await recheck_static_finding(
        checkout_path=tmp_path, file_path="m.py", source_analyzer="ruff", rule_id="F821",
        original_start_line=2, original_end_line=2, language=Language.PYTHON,
    )
    assert result is False


async def test_recheck_static_finding_returns_none_for_unknown_analyzer(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(_BAD)
    result = await recheck_static_finding(
        checkout_path=tmp_path, file_path="m.py", source_analyzer="not_a_real_analyzer", rule_id="F821",
        original_start_line=2, original_end_line=2, language=Language.PYTHON,
    )
    assert result is None


async def test_recheck_static_finding_returns_false_when_file_deleted(tmp_path: Path) -> None:
    result = await recheck_static_finding(
        checkout_path=tmp_path, file_path="does-not-exist.py", source_analyzer="ruff", rule_id="F821",
        original_start_line=2, original_end_line=2, language=Language.PYTHON,
    )
    assert result is False
