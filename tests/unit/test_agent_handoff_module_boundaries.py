"""Structural-AST credential-boundary tests -- Milestone T (T1). Mirrors
Milestone S6's own ``test_verifier_process_never_imports_the_credential_
settings_class``/``test_verifier_process_never_imports_github_or_provider_
modules`` pattern: a handoff-generation failure must always be "not
enough persisted evidence," never a credential/network failure, which is
only true if this package never imports the modules that would let it
make one."""

from __future__ import annotations

import ast
from pathlib import Path

_PACKAGE_DIR = Path(__file__).parent.parent.parent / "patchfrog" / "agent_handoff"
_FORBIDDEN_MODULES = {"patchfrog.config.settings"}
_FORBIDDEN_PREFIXES = ("patchfrog.github", "patchfrog.review.providers", "patchfrog.review.provider")


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
    return modules


def test_agent_handoff_never_imports_the_credential_settings_class() -> None:
    for path in _PACKAGE_DIR.glob("*.py"):
        imported = _imported_modules(path)
        forbidden = imported & _FORBIDDEN_MODULES
        assert not forbidden, f"{path} imports {forbidden} -- a handoff must never hold these credentials"


def test_agent_handoff_never_imports_github_or_provider_modules() -> None:
    for path in _PACKAGE_DIR.glob("*.py"):
        imported = _imported_modules(path)
        for module in imported:
            assert not any(module == p or module.startswith(p + ".") for p in _FORBIDDEN_PREFIXES), (
                f"{path} imports {module} -- a handoff is a deterministic projection of persisted "
                "state, never a provider call"
            )


def test_agent_handoff_never_imports_subprocess() -> None:
    for path in _PACKAGE_DIR.glob("*.py"):
        assert "subprocess" not in _imported_modules(path), f"{path} imports subprocess -- never allowed here"
