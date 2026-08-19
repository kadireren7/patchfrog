"""Unit coverage for patchfrog.publishing.config -- safe-by-default
``.patchfrog.yml`` ``publish:`` section loading."""

from __future__ import annotations

from pathlib import Path

from patchfrog.analysis.domain import Severity
from patchfrog.publishing.config import PublicationConfig, load_publication_config


def test_default_config_never_publishes() -> None:
    config = PublicationConfig()
    assert config.enabled is False


def test_missing_file_defaults_to_disabled(tmp_path: Path) -> None:
    config = load_publication_config(tmp_path)
    assert config.enabled is False


def test_malformed_yaml_falls_back_to_disabled_defaults(tmp_path: Path) -> None:
    (tmp_path / ".patchfrog.yml").write_text("publish: [not a mapping\n")
    config = load_publication_config(tmp_path)
    assert config == PublicationConfig()


def test_valid_publish_section_is_applied(tmp_path: Path) -> None:
    (tmp_path / ".patchfrog.yml").write_text(
        "publish:\n  enabled: true\n  min_severity: high\n  max_inline_comments: 5\n  max_summary_findings: 10\n"
    )
    config = load_publication_config(tmp_path)
    assert config.enabled is True
    assert config.min_severity is Severity.HIGH
    assert config.max_inline_comments == 5
    assert config.max_summary_findings == 10


def test_unrelated_review_section_does_not_enable_publishing(tmp_path: Path) -> None:
    (tmp_path / ".patchfrog.yml").write_text("review:\n  max_candidates: 5\n")
    config = load_publication_config(tmp_path)
    assert config.enabled is False


def test_non_mapping_publish_section_falls_back_to_defaults(tmp_path: Path) -> None:
    (tmp_path / ".patchfrog.yml").write_text("publish: not-a-mapping\n")
    config = load_publication_config(tmp_path)
    assert config == PublicationConfig()
