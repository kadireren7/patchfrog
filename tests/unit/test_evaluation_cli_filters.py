"""Unit coverage for the CLI's ``eval run`` case-filtering logic
(``--case``/``--tag``/``--language``/``--difficulty``) --
:func:`patchfrog.cli._filter_cases`. A plain argparse.Namespace stand-in
is enough; no subprocess, no real benchmark corpus."""

from __future__ import annotations

import argparse

from patchfrog.cli import _filter_cases
from patchfrog.evaluation.domain import Difficulty, EvaluationCase, Language


def _case(case_id: str, *, tags: tuple[str, ...] = (), language: Language = Language.PYTHON, difficulty: Difficulty = Difficulty.EASY) -> EvaluationCase:
    return EvaluationCase(
        id=case_id, title="t", description="", language=language, fixture=case_id, difficulty=difficulty, tags=tags,
    )


def _args(*, case: list[str] | None = None, tag: list[str] | None = None, language: str | None = None, difficulty: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(case=case or [], tag=tag or [], language=language, difficulty=difficulty)


def test_no_filters_returns_everything() -> None:
    cases = [_case("a"), _case("b")]
    assert _filter_cases(cases, _args()) == cases


def test_case_filter_selects_only_named_ids() -> None:
    cases = [_case("a"), _case("b"), _case("c")]
    result = _filter_cases(cases, _args(case=["a", "c"]))
    assert {c.id for c in result} == {"a", "c"}


def test_tag_filter_is_any_match() -> None:
    cases = [_case("a", tags=("security",)), _case("b", tags=("boundary",)), _case("c", tags=())]
    result = _filter_cases(cases, _args(tag=["security"]))
    assert {c.id for c in result} == {"a"}


def test_language_filter() -> None:
    cases = [_case("a", language=Language.PYTHON), _case("b", language=Language.C)]
    result = _filter_cases(cases, _args(language="c"))
    assert {c.id for c in result} == {"b"}


def test_difficulty_filter() -> None:
    cases = [_case("a", difficulty=Difficulty.EASY), _case("b", difficulty=Difficulty.HARD)]
    result = _filter_cases(cases, _args(difficulty="hard"))
    assert {c.id for c in result} == {"b"}


def test_combined_filters_are_conjunctive() -> None:
    cases = [
        _case("a", tags=("security",), language=Language.PYTHON, difficulty=Difficulty.EASY),
        _case("b", tags=("security",), language=Language.C, difficulty=Difficulty.EASY),
    ]
    result = _filter_cases(cases, _args(tag=["security"], language="python"))
    assert {c.id for c in result} == {"a"}


def test_no_matches_returns_empty_list() -> None:
    cases = [_case("a")]
    assert _filter_cases(cases, _args(tag=["nonexistent-tag"])) == []
