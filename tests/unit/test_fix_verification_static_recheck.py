from __future__ import annotations

from pathlib import Path

from patchfrog.domain.code import Language
from patchfrog.fix_verification.static_recheck import StaticRecheckStatus, recheck_static_finding
from patchfrog.fix_verification.surface_mapping import MappedSurface, SurfaceMappingStatus

_MAPPED = MappedSurface(SurfaceMappingStatus.UNCHANGED, file_path="m.py", start_line=2, end_line=2)


async def test_recheck_static_finding_still_fires_at_mapped_surface(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("def f():\n    return undefined_name\n")
    result = await recheck_static_finding(
        checkout_path=tmp_path, mapped_surface=_MAPPED, source_analyzer="ruff", rule_id="F821",
        language=Language.PYTHON,
    )
    assert result is StaticRecheckStatus.STILL_PRESENT


async def test_recheck_static_finding_absent_at_mapped_surface_when_fixed(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("def f():\n    return 1\n")
    result = await recheck_static_finding(
        checkout_path=tmp_path, mapped_surface=_MAPPED, source_analyzer="ruff", rule_id="F821",
        language=Language.PYTHON,
    )
    assert result is StaticRecheckStatus.ABSENT_AT_MAPPED_SURFACE


async def test_recheck_static_finding_unavailable_for_unknown_analyzer(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("def f():\n    return undefined_name\n")
    result = await recheck_static_finding(
        checkout_path=tmp_path, mapped_surface=_MAPPED, source_analyzer="not_a_real_analyzer", rule_id="F821",
        language=Language.PYTHON,
    )
    assert result is StaticRecheckStatus.UNAVAILABLE


async def test_recheck_static_finding_inconclusive_when_surface_unmapped(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("def f():\n    return undefined_name\n")
    unmapped = MappedSurface(SurfaceMappingStatus.UNMAPPABLE)
    result = await recheck_static_finding(
        checkout_path=tmp_path, mapped_surface=unmapped, source_analyzer="ruff", rule_id="F821",
        language=Language.PYTHON,
    )
    assert result is StaticRecheckStatus.INCONCLUSIVE


async def test_recheck_static_finding_inconclusive_when_surface_ambiguous(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("def f():\n    return undefined_name\n")
    ambiguous = MappedSurface(SurfaceMappingStatus.AMBIGUOUS)
    result = await recheck_static_finding(
        checkout_path=tmp_path, mapped_surface=ambiguous, source_analyzer="ruff", rule_id="F821",
        language=Language.PYTHON,
    )
    assert result is StaticRecheckStatus.INCONCLUSIVE
