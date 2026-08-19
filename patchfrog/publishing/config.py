"""GitHub review-publishing configuration and safe-by-default policy.

Mirrors :mod:`patchfrog.review.config`'s split and its untrusted-repo-content
safety rules exactly: :class:`PublicationConfig` captures the optional
``publish:`` section of a repository's ``.patchfrog.yml``, loaded the same
way (``yaml.safe_load`` only, absent/malformed-under-defaults-mode always
falls back to safe defaults). Never a source of credentials -- there are
none to configure here; GitHub write access is entirely a function of the
existing GitHub App installation token (see :mod:`patchfrog.github.auth`),
never anything read from a repository-controlled file.

The single most important default in this module is ``enabled=False``:
see the module docstring of :mod:`patchfrog.publishing.service` for why
publication must never happen just because credentials are present.
"""

from __future__ import annotations

from pathlib import Path

import structlog
import yaml
from pydantic import BaseModel, ConfigDict

from patchfrog.analysis.domain import Severity

logger = structlog.get_logger(__name__)

_CONFIG_FILENAMES = (".patchfrog.yml", ".patchfrog.yaml")

DEFAULT_ENABLED = False
DEFAULT_MIN_SEVERITY: Severity = Severity.MEDIUM
DEFAULT_MAX_INLINE_COMMENTS = 20
DEFAULT_MAX_SUMMARY_FINDINGS = 50


class PublicationConfig(BaseModel):
    """Effective publication policy for one repository.

    ``enabled`` gates nothing about *this process's* intent to publish --
    that is always the explicit ``--publish`` CLI flag / Celery task
    ``mode`` argument (see :mod:`patchfrog.publishing.service`). It exists
    so a repository can opt *out* even when an operator explicitly
    requests ``PUBLISH`` mode -- belt-and-suspenders, never the other way
    around. A repository can never opt itself *into* being published to
    just by setting this file; that decision is always made by the caller
    running PatchFrog, never by the reviewed repository's own content.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = DEFAULT_ENABLED
    min_severity: Severity = DEFAULT_MIN_SEVERITY
    max_inline_comments: int = DEFAULT_MAX_INLINE_COMMENTS
    max_summary_findings: int = DEFAULT_MAX_SUMMARY_FINDINGS


def load_publication_config(repository_root: Path) -> PublicationConfig:
    """Load the ``publish:`` section of ``.patchfrog.yml``/``.patchfrog.yaml``
    from a repository root. Absent or malformed always falls back to safe
    (non-publishing) defaults -- publication planning is never blocked by
    a bad config file the way a real review run is (see
    :func:`patchfrog.review.config.load_review_config`'s ``on_malformed``);
    worst case, publication policy is simply conservative defaults."""

    for filename in _CONFIG_FILENAMES:
        path = repository_root / filename
        if not path.is_file():
            continue

        try:
            raw = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("publication_config_unreadable", path=str(path), error=str(exc))
            return PublicationConfig()

        if raw is None:
            return PublicationConfig()
        if not isinstance(raw, dict):
            logger.warning("publication_config_malformed", path=str(path))
            return PublicationConfig()

        publish_section = raw.get("publish", {})
        if not isinstance(publish_section, dict):
            logger.warning("publication_config_section_malformed", path=str(path))
            return PublicationConfig()

        try:
            return PublicationConfig.model_validate(publish_section)
        except Exception as exc:
            logger.warning("publication_config_invalid", path=str(path), error=str(exc))
            return PublicationConfig()

    return PublicationConfig()
