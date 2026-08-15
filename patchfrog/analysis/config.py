"""Static analysis configuration.

Loaded from an optional ``.patchfrog.yml`` (or ``.patchfrog.yaml``) file at
the analyzed repository's root — deliberately small and declarative, not a
general configuration language. Absent or malformed config falls back to
safe defaults; this file lives inside an *untrusted* repository, so it is
only ever parsed with ``yaml.safe_load`` and a parse failure degrades
gracefully rather than aborting analysis.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import structlog
import yaml
from pydantic import BaseModel, ConfigDict

logger = structlog.get_logger(__name__)

_CONFIG_FILENAMES = (".patchfrog.yml", ".patchfrog.yaml")

#: Bumped whenever the shape/semantics of AnalysisConfig itself changes, so
#: two runs under different PatchFrog versions never share a configuration
#: fingerprint by coincidence.
CONFIG_SCHEMA_VERSION = 1

DEFAULT_ENABLED_ANALYZERS = ("ruff", "cppcheck", "clang_tidy", "semgrep")
DEFAULT_TIMEOUT_SECONDS = 60.0

EnabledSetting = bool | Literal["auto"]


class AnalyzerConfig(BaseModel):
    """Per-analyzer override. ``"auto"`` means "enable if available/capable"."""

    model_config = ConfigDict(extra="ignore")

    enabled: EnabledSetting = "auto"


class AnalysisConfig(BaseModel):
    """Effective static-analysis configuration for one analysis run."""

    model_config = ConfigDict(extra="ignore")

    enabled: list[str] = list(DEFAULT_ENABLED_ANALYZERS)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    severity_threshold: str = "info"
    analyzers: dict[str, AnalyzerConfig] = {}

    def wants(self, name: str) -> EnabledSetting:
        """Whether ``name`` should be considered for this run.

        ``False`` if the analyzer isn't in the top-level ``enabled`` list at
        all, or if it's explicitly disabled per-analyzer. ``"auto"``
        otherwise means "run it if it's actually available/capable" — the
        final decision belongs to analyzer selection, not this config.
        """

        if name not in self.enabled:
            return False
        return self.analyzers.get(name, AnalyzerConfig()).enabled

    def fingerprint(self) -> str:
        """A deterministic fingerprint of this configuration.

        Two analysis runs with an identical fingerprint were configured
        identically — used to decide whether a repeat run's result can be
        distinguished from (or should reuse the identity of) a prior one.
        Deliberately excludes analyzer *versions* (see
        :class:`~patchfrog.persistence.models.analysis.AnalyzerExecutionModel`,
        which records those per-execution) — this is about configuration
        intent, not what happened to be installed when it ran.
        """

        payload = {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "enabled": sorted(self.enabled),
            "timeout_seconds": self.timeout_seconds,
            "severity_threshold": self.severity_threshold,
            "analyzers": {
                name: cfg.enabled for name, cfg in sorted(self.analyzers.items())
            },
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def load_analysis_config(repository_root: Path) -> AnalysisConfig:
    """Load ``.patchfrog.yml``/``.patchfrog.yaml`` from a repository root.

    Returns default configuration if no config file exists, if it isn't
    valid YAML, or if its shape doesn't match :class:`AnalysisConfig` —
    repository-controlled config content must never be able to crash
    analysis, only to configure it.
    """

    for filename in _CONFIG_FILENAMES:
        path = repository_root / filename
        if not path.is_file():
            continue
        try:
            raw = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("analysis_config_unreadable", path=str(path), error=str(exc))
            return AnalysisConfig()

        if raw is None:
            return AnalysisConfig()
        if not isinstance(raw, dict):
            logger.warning("analysis_config_malformed", path=str(path))
            return AnalysisConfig()

        analysis_section = raw.get("analysis", raw)
        try:
            return AnalysisConfig.model_validate(analysis_section)
        except Exception as exc:
            logger.warning("analysis_config_invalid", path=str(path), error=str(exc))
            return AnalysisConfig()

    return AnalysisConfig()
