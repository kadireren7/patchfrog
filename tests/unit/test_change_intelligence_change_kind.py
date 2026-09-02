"""Unit coverage for :mod:`patchfrog.change_intelligence.change_kind` --
evidence-based classification, never prose/LLM inference."""

from __future__ import annotations

from patchfrog.change_intelligence.change_kind import (
    classify_candidate,
    classify_file_path,
    combine_kinds,
)
from patchfrog.change_intelligence.domain import ChangeKind


def test_test_file_classified_as_test() -> None:
    assert classify_file_path("tests/test_foo.py", is_test=True) is ChangeKind.TEST


def test_config_extension_classified_as_configuration() -> None:
    assert classify_file_path("app/settings.yaml", is_test=False) is ChangeKind.CONFIGURATION


def test_config_path_marker_classified_as_configuration() -> None:
    assert classify_file_path("patchfrog/config/settings.py", is_test=False) is ChangeKind.CONFIGURATION


def test_infra_path_classified_as_infrastructure() -> None:
    assert classify_file_path(".github/workflows/ci.yml", is_test=False) is ChangeKind.INFRASTRUCTURE


def test_infra_marker_wins_over_generic_config_extension() -> None:
    """A .yaml file under an infra-marked directory (deploy/, docker/,
    .github/workflows/) is classified INFRASTRUCTURE, not the generic
    CONFIGURATION -- infra markers are checked first precisely because
    they're the more specific signal."""

    assert classify_file_path("deploy/values.yaml", is_test=False) is ChangeKind.INFRASTRUCTURE


def test_persistence_path_classified_as_persistence() -> None:
    assert classify_file_path("patchfrog/persistence/models/review.py", is_test=False) is ChangeKind.PERSISTENCE


def test_no_path_signal_returns_none() -> None:
    assert classify_file_path("patchfrog/review/service.py", is_test=False) is None


def test_candidate_with_cross_file_caller_is_contract() -> None:
    kind = classify_candidate(file_path="patchfrog/review/service.py", is_test=False, has_cross_file_caller=True)
    assert kind is ChangeKind.CONTRACT


def test_candidate_with_no_signal_is_behavior() -> None:
    kind = classify_candidate(file_path="patchfrog/review/service.py", is_test=False, has_cross_file_caller=False)
    assert kind is ChangeKind.BEHAVIOR


def test_path_signal_wins_over_contract_signal() -> None:
    """A config file with a cross-file caller is still CONFIGURATION --
    path-based structural evidence is checked first and is more
    specific than the generic "has callers elsewhere" signal."""

    kind = classify_candidate(file_path="patchfrog/config/settings.py", is_test=False, has_cross_file_caller=True)
    assert kind is ChangeKind.CONFIGURATION


def test_combine_kinds_single_agreement() -> None:
    assert combine_kinds([ChangeKind.BEHAVIOR, ChangeKind.BEHAVIOR]) is ChangeKind.BEHAVIOR


def test_combine_kinds_disagreement_is_mixed() -> None:
    assert combine_kinds([ChangeKind.BEHAVIOR, ChangeKind.PERSISTENCE]) is ChangeKind.MIXED


def test_combine_kinds_single_candidate() -> None:
    assert combine_kinds([ChangeKind.CONTRACT]) is ChangeKind.CONTRACT
