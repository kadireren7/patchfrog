"""Structural-AST authority-boundary tests -- Milestone T (T2). MCP is
read-mostly: it may only ever write one narrow, new ``fix_attempts`` row
(via :mod:`patchfrog.fix_verification`) -- never a file write, ``git
commit``/``push``, or GitHub write (Part L/AL/AM)."""

from __future__ import annotations

import ast
from pathlib import Path

_MCP_DIR = Path(__file__).parent.parent.parent / "patchfrog" / "mcp"
_FIX_VERIFICATION_DIR = Path(__file__).parent.parent.parent / "patchfrog" / "fix_verification"

#: Part AL -- GitHub-writing surfaces. Neither package needs them: MCP
#: never talks to GitHub at all; fix_verification only ever mints a
#: read-only clone credential (patchfrog.github.auth), never the full API
#: client (which carries write methods like create_review).
_FORBIDDEN_MODULE_PREFIXES = ("patchfrog.github.client", "patchfrog.publishing")

#: Part AM -- no file-write/commit/push tool anywhere in this surface.
_FORBIDDEN_FUNCTION_NAMES = {"write_file", "apply_patch", "git_commit", "git_push"}


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


def _defined_function_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_mcp_never_imports_github_write_client_or_publishing() -> None:
    for path in _MCP_DIR.glob("*.py"):
        imported = _imported_modules(path)
        for module in imported:
            assert not any(module == p or module.startswith(p + ".") for p in _FORBIDDEN_MODULE_PREFIXES), (
                f"{path} imports {module} -- MCP must never write to GitHub"
            )


def test_fix_verification_never_imports_github_write_client_or_publishing() -> None:
    for path in _FIX_VERIFICATION_DIR.glob("*.py"):
        imported = _imported_modules(path)
        for module in imported:
            assert not any(module == p or module.startswith(p + ".") for p in _FORBIDDEN_MODULE_PREFIXES), (
                f"{path} imports {module} -- fix verification must never write to GitHub"
            )


def test_mcp_never_imports_subprocess_directly() -> None:
    for path in _MCP_DIR.glob("*.py"):
        assert "subprocess" not in _imported_modules(path), (
            f"{path} imports subprocess directly -- MCP must never run arbitrary shell commands itself"
        )


def test_mcp_never_defines_a_source_writing_or_git_mutation_tool() -> None:
    for path in _MCP_DIR.glob("*.py"):
        defined = _defined_function_names(path)
        overlap = defined & _FORBIDDEN_FUNCTION_NAMES
        assert not overlap, f"{path} defines {overlap} -- never a source-writing or git-mutating tool"


async def test_mcp_exposes_exactly_the_five_documented_tools() -> None:
    from patchfrog.config.settings import Settings
    from patchfrog.mcp.server import PatchFrogMCPServer
    from patchfrog.persistence.database import create_engine, create_session_factory

    settings = Settings()
    session_factory = create_session_factory(create_engine(settings.database_url))
    server = PatchFrogMCPServer(session_factory=session_factory, settings=settings)

    tools = await server.mcp.list_tools()
    tool_names = {t.name for t in tools}
    assert tool_names == {
        "list_findings", "get_finding_handoff", "start_fix_attempt", "get_fix_attempt", "get_merge_readiness",
    }
