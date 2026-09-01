"""Structural boundary checks for the Evaluation & Telemetry Intelligence
milestone -- no database, no LLM.

- Telemetry never depends on the evaluation package (benchmark ground
  truth stays physically separate from operational telemetry, spec
  section 39).
- The new Prometheus metrics never carry a high-cardinality label (spec
  section 41).
"""

from __future__ import annotations

import ast
from pathlib import Path

import patchfrog.telemetry.aggregation
import patchfrog.telemetry.collector
import patchfrog.telemetry.domain
import patchfrog.telemetry.reporting

_TELEMETRY_MODULES = (
    patchfrog.telemetry.domain,
    patchfrog.telemetry.aggregation,
    patchfrog.telemetry.collector,
    patchfrog.telemetry.reporting,
)

_FORBIDDEN_HIGH_CARDINALITY_LABELS = {
    "repository",
    "repository_name",
    "full_name",
    "pr",
    "pull_request",
    "pr_number",
    "candidate_id",
    "finding_id",
    "proposal_id",
    "file_path",
    "file",
}

_NEW_METRIC_NAMES = ("candidates_by_tier_total", "candidates_skipped_budget_total", "critic_calls_total")


def test_telemetry_package_never_imports_evaluation_package() -> None:
    for module in _TELEMETRY_MODULES:
        source = Path(module.__file__).read_text()  # type: ignore[arg-type]
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                assert not node.module.startswith("patchfrog.evaluation"), (module.__name__, node.module)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("patchfrog.evaluation"), (module.__name__, alias.name)


def test_new_ops_metrics_have_no_high_cardinality_labels() -> None:
    import patchfrog.ops.metrics as metrics_module

    for name in _NEW_METRIC_NAMES:
        metric = getattr(metrics_module, name)
        # prometheus_client stores the configured label names on `_labelnames`.
        label_names = set(getattr(metric, "_labelnames", ()))
        overlap = label_names & _FORBIDDEN_HIGH_CARDINALITY_LABELS
        assert not overlap, (name, label_names)
        # "tier" is the only label used, and it's a closed 3-value set.
        assert label_names <= {"tier"}
