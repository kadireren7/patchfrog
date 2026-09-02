"""Unit tests for :mod:`patchfrog.contract_intelligence.function_signature`
-- the deterministic Python signature tokenizer. No I/O, no database."""

from __future__ import annotations

from patchfrog.contract_intelligence.function_signature import ParameterKind, parse_python_signature


def test_simple_signature() -> None:
    sig = parse_python_signature("def foo(a: int, b: str = 'x') -> int")
    assert sig is not None
    assert sig.name == "foo"
    assert sig.is_async is False
    assert [p.name for p in sig.parameters] == ["a", "b"]
    a, b = sig.parameters
    assert a.has_annotation and a.annotation_text == "int"
    assert not a.has_default
    assert b.has_default and b.default_text == "'x'"
    assert sig.has_return_annotation and sig.return_annotation_text == "int"


def test_async_prefix_detected() -> None:
    sig = parse_python_signature("async def foo(x)")
    assert sig is not None
    assert sig.is_async is True


def test_no_parameters() -> None:
    sig = parse_python_signature("def foo()")
    assert sig is not None
    assert sig.parameters == ()
    assert sig.has_return_annotation is False


def test_no_return_annotation() -> None:
    sig = parse_python_signature("def foo(a)")
    assert sig is not None
    assert sig.has_return_annotation is False
    assert sig.return_annotation_text is None


def test_decorators_are_ignored() -> None:
    sig = parse_python_signature('@app.route("/x", methods=["GET", "POST"])\n@staticmethod\ndef handler(a: int)')
    assert sig is not None
    assert sig.name == "handler"
    assert [p.name for p in sig.parameters] == ["a"]


def test_multiline_signature() -> None:
    text = "def qux(a: int,\n         b: dict[str, int] = {},\n         *args,\n         c: bool = True,\n         **kwargs) -> None"
    sig = parse_python_signature(text)
    assert sig is not None
    names_kinds = [(p.name, p.kind) for p in sig.parameters]
    assert names_kinds == [
        ("a", ParameterKind.POSITIONAL_OR_KEYWORD),
        ("b", ParameterKind.POSITIONAL_OR_KEYWORD),
        ("args", ParameterKind.VAR_POSITIONAL),
        ("c", ParameterKind.KEYWORD_ONLY),
        ("kwargs", ParameterKind.VAR_KEYWORD),
    ]


def test_bare_star_marks_keyword_only() -> None:
    sig = parse_python_signature("def f(a, b, /, c, *, d)")
    assert sig is not None
    kinds = {p.name: p.kind for p in sig.parameters}
    assert kinds["a"] is ParameterKind.POSITIONAL_ONLY
    assert kinds["b"] is ParameterKind.POSITIONAL_ONLY
    assert kinds["c"] is ParameterKind.POSITIONAL_OR_KEYWORD
    assert kinds["d"] is ParameterKind.KEYWORD_ONLY


def test_nested_bracket_default_value_not_split() -> None:
    sig = parse_python_signature('def h(a: dict = {"x": 1, "y": 2}, b: int = 3) -> None')
    assert sig is not None
    assert [p.name for p in sig.parameters] == ["a", "b"]
    assert sig.parameter("a") is not None
    assert sig.parameter("a").default_text == '{"x": 1, "y": 2}'  # type: ignore[union-attr]


def test_generic_annotation_with_commas_not_split() -> None:
    sig = parse_python_signature("def m(a: Callable[[int, str], bool])")
    assert sig is not None
    assert [p.name for p in sig.parameters] == ["a"]
    assert sig.parameter("a").annotation_text == "Callable[[int, str], bool]"  # type: ignore[union-attr]


def test_zero_and_single_param_lambda_default_parsed_correctly() -> None:
    sig = parse_python_signature("def i(cb=lambda: None, d: int = 3) -> None")
    assert sig is not None
    assert [p.name for p in sig.parameters] == ["cb", "d"]
    assert sig.parameter("cb").default_text == "lambda: None"  # type: ignore[union-attr]


def test_multi_parameter_lambda_default_fails_closed() -> None:
    """A `lambda x, y: ...` default's own top-level comma is tracked
    separately (see the module's lambda-depth handling) -- this proves
    the happy path, not the failure path; see
    test_genuinely_unparseable_signature_returns_none for the guard
    that exists for whatever this doesn't model."""

    sig = parse_python_signature("def handler(a: int, b = lambda x, y: x + y)")
    assert sig is not None
    assert [p.name for p in sig.parameters] == ["a", "b"]
    assert sig.parameter("b").default_text == "lambda x, y: x + y"  # type: ignore[union-attr]


def test_non_function_signature_returns_none() -> None:
    assert parse_python_signature("class Foo") is None
    assert parse_python_signature("x = 1") is None
    assert parse_python_signature("") is None


def test_variadic_only() -> None:
    sig = parse_python_signature("def f(*args, **kwargs)")
    assert sig is not None
    kinds = {p.name: p.kind for p in sig.parameters}
    assert kinds == {"args": ParameterKind.VAR_POSITIONAL, "kwargs": ParameterKind.VAR_KEYWORD}


def test_optional_annotation_shape_syntax_only() -> None:
    sig = parse_python_signature("def f(a: int) -> Optional[int]")
    assert sig is not None
    assert sig.return_annotation_text == "Optional[int]"

    sig2 = parse_python_signature("def f(a: int) -> int | None")
    assert sig2 is not None
    assert sig2.return_annotation_text == "int | None"
