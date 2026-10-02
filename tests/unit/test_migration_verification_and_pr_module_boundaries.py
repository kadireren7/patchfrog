"""M8/M9's own structural provider-free invariant (mirrors
``test_deterministic_migration_modules_never_import_a_provider`` in
``tests/unit/test_migration_generator.py``): no module in
``patchfrog.migration_verification`` or ``patchfrog.migration_pr`` may
import an LLM provider abstraction or SDK. Neither package has any
optional model-assisted path (unlike ``patchfrog.migration.assist``), so
this is unconditional for every module in both packages."""

from __future__ import annotations

import ast
from pathlib import Path

_PACKAGE_DIRS = (
    Path(__file__).resolve().parent.parent.parent / "patchfrog" / "migration_verification",
    Path(__file__).resolve().parent.parent.parent / "patchfrog" / "migration_pr",
)


def test_migration_verification_and_pr_modules_never_import_a_provider() -> None:
    for package_dir in _PACKAGE_DIRS:
        for path in package_dir.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert not node.module.startswith(("patchfrog.review.provider", "patchfrog.review.providers")), (
                        f"{path} imports {node.module}"
                    )
                if isinstance(node, ast.Import):
                    assert not any(a.name.split(".")[0] in ("anthropic", "openai", "google") for a in node.names), (
                        f"{path} imports a provider SDK"
                    )
