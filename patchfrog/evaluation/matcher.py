"""Deterministic predicted-finding <-> ground-truth matching.

    case ground truth + predicted findings (static and/or AI)
    -> per-prediction MatchOutcome (TRUE_POSITIVE/FALSE_POSITIVE/
       DUPLICATE/UNSUPPORTED/OUT_OF_SCOPE)
    -> per-expected-finding ExpectedOutcome (FOUND/MISSED)

No LLM judge, no embeddings, no fuzzy prose comparison -- matching uses
only file path (exact), category (exact), symbol (exact or a tolerant
qualified-name suffix match), line-range overlap/tolerance, and an
optional evidence substring check. Deliberately conservative: a
prediction only ever counts as TRUE_POSITIVE when every specified
signal agrees; anything else is explainable in ``detail``.

``issue_family`` on :class:`~patchfrog.evaluation.domain.ExpectedFinding`
is a reporting/grouping tag only -- production findings carry no such
field, so it is never used as a literal match key here (documented
scope decision, not an oversight).

Severity is deliberately excluded from the match predicate itself: a
finding can be the "same bug" at the wrong severity, and Phase 8 wants
that measured separately (see :mod:`patchfrog.evaluation.metrics`'s
severity calibration), not silently turned into a miss.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from patchfrog.evaluation.domain import (
    EvaluationCase,
    EvaluationMode,
    ExpectedFinding,
    ExpectedFindingOutcome,
    ExpectedOutcome,
    ForbiddenFinding,
    GroundTruthSource,
    MatchOutcome,
    PredictedFinding,
    PredictionOutcome,
)


def match_case(
    *,
    case: EvaluationCase,
    mode: EvaluationMode,
    predictions: Sequence[PredictedFinding],
    valid_file_paths: frozenset[str],
    file_line_counts: Mapping[str, int],
) -> tuple[tuple[PredictionOutcome, ...], tuple[ExpectedFindingOutcome, ...]]:
    in_scope_expected = [e for e in case.expected if _in_scope(e, mode)]

    consumed: set[int] = set()
    prediction_expected_id: dict[int, tuple[str, bool]] = {}  # index -> (expected_id, is_first_match)
    expected_outcomes: list[ExpectedFindingOutcome] = []

    for expected in in_scope_expected:
        candidate_indices = [
            i for i, p in enumerate(predictions) if i not in consumed and _is_match(expected, p)
        ]
        if not candidate_indices:
            expected_outcomes.append(
                ExpectedFindingOutcome(
                    expected=expected, outcome=ExpectedOutcome.MISSED,
                    matched_prediction_index=None,
                    detail="no predicted finding matched file/category/symbol/line/evidence",
                )
            )
            continue

        first, *rest = candidate_indices
        consumed.add(first)
        prediction_expected_id[first] = (expected.id, True)
        for idx in rest:
            consumed.add(idx)
            prediction_expected_id[idx] = (expected.id, False)
        expected_outcomes.append(
            ExpectedFindingOutcome(
                expected=expected, outcome=ExpectedOutcome.FOUND, matched_prediction_index=first,
                detail=f"matched by prediction #{first}" + (f" (+{len(rest)} duplicate(s))" if rest else ""),
            )
        )

    prediction_outcomes: list[PredictionOutcome] = []
    for i, pred in enumerate(predictions):
        if i in prediction_expected_id:
            expected_id, is_first = prediction_expected_id[i]
            if is_first:
                prediction_outcomes.append(
                    PredictionOutcome(
                        prediction=pred, outcome=MatchOutcome.TRUE_POSITIVE,
                        matched_expected_id=expected_id, detail=f"matches expected finding {expected_id!r}",
                    )
                )
            else:
                prediction_outcomes.append(
                    PredictionOutcome(
                        prediction=pred, outcome=MatchOutcome.DUPLICATE,
                        matched_expected_id=expected_id,
                        detail=f"duplicates an already-matched prediction for {expected_id!r}",
                    )
                )
            continue

        reason = unsupported_reason(pred, valid_file_paths, file_line_counts)
        if reason is not None:
            prediction_outcomes.append(
                PredictionOutcome(
                    prediction=pred, outcome=MatchOutcome.UNSUPPORTED,
                    matched_expected_id=None, detail=reason,
                )
            )
            continue

        forbidden = _matching_forbidden_rule(pred, case.forbidden)
        if forbidden is not None:
            prediction_outcomes.append(
                PredictionOutcome(
                    prediction=pred, outcome=MatchOutcome.OUT_OF_SCOPE,
                    matched_expected_id=None, detail="matches a forbidden rule for this case",
                    forbidden_reason=forbidden.reason,
                )
            )
            continue

        prediction_outcomes.append(
            PredictionOutcome(
                prediction=pred, outcome=MatchOutcome.FALSE_POSITIVE,
                matched_expected_id=None, detail="no expected finding or forbidden rule matched",
            )
        )

    return tuple(prediction_outcomes), tuple(expected_outcomes)


def _in_scope(expected: ExpectedFinding, mode: EvaluationMode) -> bool:
    if mode is EvaluationMode.STATIC_ONLY:
        return expected.ground_truth_source in (GroundTruthSource.STATIC_EXPECTED, GroundTruthSource.EITHER)
    if mode is EvaluationMode.AI_ONLY:
        return expected.ground_truth_source in (GroundTruthSource.AI_EXPECTED, GroundTruthSource.EITHER)
    return True  # FULL_PIPELINE / INCREMENTAL: both subsystems present


def _is_match(expected: ExpectedFinding, pred: PredictedFinding) -> bool:
    return (
        pred.file_path == expected.file
        and pred.category is expected.category
        and _symbol_matches(expected.symbol, pred.symbol_qualified_name)
        and _line_overlaps(expected, pred)
        and _evidence_matches(expected, pred)
    )


def _symbol_matches(expected_symbol: str | None, predicted_symbol: str | None) -> bool:
    if expected_symbol is None:
        return True
    if predicted_symbol is None:
        return False
    if predicted_symbol == expected_symbol:
        return True
    # Tolerate a qualified-name prefix (e.g. "Account.can_withdraw" matching
    # an expected bare "can_withdraw") -- never the reverse (a bare
    # prediction must not match an expected fully-qualified name it
    # didn't actually report).
    return predicted_symbol.endswith(f".{expected_symbol}") or predicted_symbol.endswith(f"::{expected_symbol}")


def _line_overlaps(expected: ExpectedFinding, pred: PredictedFinding) -> bool:
    rng = expected.effective_line_range
    if rng is None:
        return True
    low, high = rng
    return not (pred.end_line < low or pred.start_line > high)


def _evidence_matches(expected: ExpectedFinding, pred: PredictedFinding) -> bool:
    if expected.evidence_contains is None:
        return True
    haystack = _normalize(f"{pred.evidence_text} {pred.message}")
    return _normalize(expected.evidence_contains) in haystack


def _normalize(text: str) -> str:
    return " ".join(text.split()).strip().lower()


def unsupported_reason(
    pred: PredictedFinding, valid_file_paths: frozenset[str], file_line_counts: Mapping[str, int]
) -> str | None:
    """Public so :mod:`patchfrog.evaluation.metrics` can apply the exact
    same deterministic hallucination check to pre-validation proposals
    (see the "hallucination before vs. after validation" metric) --
    never a second, subtly different implementation."""

    if pred.file_path not in valid_file_paths:
        return f"cites nonexistent file {pred.file_path!r}"
    line_count = file_line_counts.get(pred.file_path)
    if line_count is not None and (pred.start_line > line_count or pred.end_line > line_count):
        return f"cites lines {pred.start_line}-{pred.end_line} beyond {pred.file_path!r}'s {line_count} lines"
    if pred.start_line < 1 or pred.end_line < pred.start_line:
        return f"cites an invalid line range {pred.start_line}-{pred.end_line}"
    return None


def _matching_forbidden_rule(
    pred: PredictedFinding, forbidden: Sequence[ForbiddenFinding]
) -> ForbiddenFinding | None:
    for rule in forbidden:
        if rule.category is not None and pred.category is not rule.category:
            continue
        # issue_family is not a field production predictions carry (see
        # the module docstring) -- a category-only forbidden rule is the
        # only kind matchable against real output; an issue_family-only
        # rule is documentation for benchmark authors, not machine-checked.
        if rule.category is None and rule.issue_family is not None:
            continue
        return rule
    return None
