"""Unit tests for :mod:`patchfrog.contract_intelligence.delta` -- the
deterministic breaking-characteristic rules (spec section 6)."""

from __future__ import annotations

from patchfrog.contract_intelligence.delta import diff_signatures
from patchfrog.contract_intelligence.domain import BREAKING_CHARACTERISTICS, BreakingCharacteristic
from patchfrog.contract_intelligence.function_signature import parse_python_signature


def _sig(text: str):  # type: ignore[no-untyped-def]
    sig = parse_python_signature(text)
    assert sig is not None
    return sig


def test_required_parameter_added_is_breaking() -> None:
    diff = diff_signatures(_sig("def f(a)"), _sig("def f(a, b)"))
    assert diff == (BreakingCharacteristic.REQUIRED_PARAMETER_ADDED,)
    assert diff[0] in BREAKING_CHARACTERISTICS


def test_optional_parameter_added_is_not_breaking() -> None:
    diff = diff_signatures(_sig("def f(a)"), _sig("def f(a, b=1)"))
    assert diff == (BreakingCharacteristic.OPTIONAL_PARAMETER_ADDED,)
    assert diff[0] not in BREAKING_CHARACTERISTICS


def test_parameter_removed_is_breaking() -> None:
    diff = diff_signatures(_sig("def f(a, b)"), _sig("def f(a)"))
    assert diff == (BreakingCharacteristic.PARAMETER_REMOVED,)
    assert diff[0] in BREAKING_CHARACTERISTICS


def test_default_removed_is_breaking() -> None:
    diff = diff_signatures(_sig("def f(a, b=1)"), _sig("def f(a, b)"))
    assert diff == (BreakingCharacteristic.DEFAULT_REMOVED,)
    assert diff[0] in BREAKING_CHARACTERISTICS


def test_default_added_is_not_breaking() -> None:
    diff = diff_signatures(_sig("def f(a, b)"), _sig("def f(a, b=1)"))
    assert diff == (BreakingCharacteristic.DEFAULT_ADDED,)
    assert diff[0] not in BREAKING_CHARACTERISTICS


def test_return_became_optional_is_breaking() -> None:
    diff = diff_signatures(_sig("def f() -> int"), _sig("def f() -> Optional[int]"))
    assert diff == (BreakingCharacteristic.RETURN_BECAME_OPTIONAL,)
    assert diff[0] in BREAKING_CHARACTERISTICS


def test_return_became_optional_union_syntax() -> None:
    diff = diff_signatures(_sig("def f() -> int"), _sig("def f() -> int | None"))
    assert diff == (BreakingCharacteristic.RETURN_BECAME_OPTIONAL,)


def test_return_became_required_is_not_breaking() -> None:
    diff = diff_signatures(_sig("def f() -> Optional[int]"), _sig("def f() -> int"))
    assert diff == (BreakingCharacteristic.RETURN_BECAME_REQUIRED,)
    assert diff[0] not in BREAKING_CHARACTERISTICS


def test_return_annotation_changed_generic() -> None:
    diff = diff_signatures(_sig("def f() -> int"), _sig("def f() -> str"))
    assert diff == (BreakingCharacteristic.RETURN_ANNOTATION_CHANGED,)
    assert diff[0] not in BREAKING_CHARACTERISTICS


def test_sync_to_async_is_breaking() -> None:
    diff = diff_signatures(_sig("def f(x)"), _sig("async def f(x)"))
    assert diff == (BreakingCharacteristic.SYNC_TO_ASYNC,)
    assert diff[0] in BREAKING_CHARACTERISTICS


def test_async_to_sync_is_breaking() -> None:
    diff = diff_signatures(_sig("async def f(x)"), _sig("def f(x)"))
    assert diff == (BreakingCharacteristic.ASYNC_TO_SYNC,)
    assert diff[0] in BREAKING_CHARACTERISTICS


def test_identical_signatures_produce_no_characteristics() -> None:
    diff = diff_signatures(_sig("def f(a: int, b: str = 'x') -> int"), _sig("def f(a: int, b: str = 'x') -> int"))
    assert diff == ()


def test_reordering_same_named_params_produces_no_characteristics() -> None:
    """Deliberate scope choice (see the module docstring): comparison is
    by name, not positional index."""

    diff = diff_signatures(_sig("def f(a, b)"), _sig("def f(b, a)"))
    assert diff == ()


def test_multiple_characteristics_all_reported() -> None:
    diff = diff_signatures(_sig("def f(a, b=1) -> int"), _sig("def f(a, c) -> Optional[int]"))
    assert set(diff) == {
        BreakingCharacteristic.PARAMETER_REMOVED,  # b removed
        BreakingCharacteristic.REQUIRED_PARAMETER_ADDED,  # c added
        BreakingCharacteristic.RETURN_BECAME_OPTIONAL,
    }
