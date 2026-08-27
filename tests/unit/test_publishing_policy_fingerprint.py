"""Unit coverage for PublicationConfig.fingerprint() -- the
publication_policy_fingerprint half of a publication's canonical
identity. Pure/synchronous, no network, no database."""

from __future__ import annotations

from unittest import mock

from patchfrog.analysis.domain import Severity
from patchfrog.publishing import config as config_module
from patchfrog.publishing.config import PublicationConfig


def test_identical_config_has_identical_fingerprint() -> None:
    a = PublicationConfig(enabled=True, min_severity=Severity.HIGH, max_inline_comments=5, max_summary_findings=10)
    b = PublicationConfig(enabled=True, min_severity=Severity.HIGH, max_inline_comments=5, max_summary_findings=10)
    assert a.fingerprint() == b.fingerprint()


def test_different_min_severity_changes_fingerprint() -> None:
    a = PublicationConfig(min_severity=Severity.HIGH)
    b = PublicationConfig(min_severity=Severity.LOW)
    assert a.fingerprint() != b.fingerprint()


def test_different_max_inline_comments_changes_fingerprint() -> None:
    a = PublicationConfig(max_inline_comments=5)
    b = PublicationConfig(max_inline_comments=6)
    assert a.fingerprint() != b.fingerprint()


def test_different_max_summary_findings_changes_fingerprint() -> None:
    a = PublicationConfig(max_summary_findings=10)
    b = PublicationConfig(max_summary_findings=11)
    assert a.fingerprint() != b.fingerprint()


def test_different_enabled_changes_fingerprint() -> None:
    a = PublicationConfig(enabled=True)
    b = PublicationConfig(enabled=False)
    assert a.fingerprint() != b.fingerprint()


def test_different_frog_marker_changes_fingerprint() -> None:
    a = PublicationConfig(frog_marker=True)
    b = PublicationConfig(frog_marker=False)
    assert a.fingerprint() != b.fingerprint()


def test_comment_format_version_bump_changes_fingerprint() -> None:
    """A PatchFrog-side formatting change (patchfrog.publishing.body)
    must invalidate reuse even when the repository's own .patchfrog.yml
    publish: section is byte-for-byte identical."""

    config = PublicationConfig(enabled=True, min_severity=Severity.HIGH)
    baseline = config.fingerprint()
    with mock.patch.object(config_module, "COMMENT_FORMAT_VERSION", config_module.COMMENT_FORMAT_VERSION + 1):
        bumped = config.fingerprint()
    assert baseline != bumped


def test_publication_engine_version_bump_changes_fingerprint() -> None:
    """A PatchFrog-side planner/engine change (patchfrog.publishing.planner)
    must invalidate reuse the same way."""

    config = PublicationConfig(enabled=True, min_severity=Severity.HIGH)
    baseline = config.fingerprint()
    with mock.patch.object(
        config_module, "PUBLICATION_ENGINE_VERSION", config_module.PUBLICATION_ENGINE_VERSION + 1
    ):
        bumped = config.fingerprint()
    assert baseline != bumped


def test_fingerprint_is_a_stable_deterministic_hex_digest() -> None:
    config = PublicationConfig(enabled=True)
    assert config.fingerprint() == config.fingerprint()
    fp = config.fingerprint()
    assert len(fp) == 64  # sha256 hex digest
    int(fp, 16)  # must be valid hex
