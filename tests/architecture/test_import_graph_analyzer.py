"""Correctness of the static import analyzer itself, on tiny synthetic
trees (``tmp_path``), so the boundary tests can be trusted."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.architecture.import_graph import (
    ImportKind,
    build_graph,
    import_closure,
    strongly_connected_components,
    unit_edges,
)


def _tree(root: Path, files: dict[str, str]) -> Path:
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return root


def _edges(root: Path) -> set[tuple[str, str, str]]:
    return {(e.importer, e.target, e.kind.value) for e in build_graph(root).edges}


def test_relative_imports_resolve_against_the_importers_package(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "patchfrog/__init__.py": "",
            "patchfrog/a/__init__.py": "from . import b\nfrom .b import thing\n",
            "patchfrog/a/b.py": "thing = 1\n",
            "patchfrog/a/sub/__init__.py": "",
            "patchfrog/a/sub/c.py": "from .. import b\nfrom ..b import thing\nfrom ...z import q\n",
            "patchfrog/z.py": "q = 1\n",
        },
    )
    edges = _edges(root)

    assert ("patchfrog.a", "patchfrog.a.b", "module") in edges
    assert ("patchfrog.a.sub.c", "patchfrog.a.b", "module") in edges
    assert ("patchfrog.a.sub.c", "patchfrog.z", "module") in edges


def test_relative_import_beyond_top_level_is_rejected(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"patchfrog/__init__.py": "", "patchfrog/x.py": "from .... import nope\n"})
    with pytest.raises(ValueError, match="relative import beyond top-level package"):
        build_graph(root)


def test_from_import_distinguishes_submodules_from_attributes(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "patchfrog/__init__.py": "",
            "patchfrog/pkg/__init__.py": "VALUE = 1\n",
            "patchfrog/pkg/mod.py": "",
            "patchfrog/user.py": "from patchfrog.pkg import mod, VALUE\nimport patchfrog.pkg.mod\n",
        },
    )
    edges = _edges(root)

    assert ("patchfrog.user", "patchfrog.pkg.mod", "module") in edges
    assert ("patchfrog.user", "patchfrog.pkg", "module") in edges


def test_import_kinds_module_lazy_type_checking_and_class_body(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "patchfrog/__init__.py": "",
            "patchfrog/t.py": "",
            "patchfrog/l.py": "",
            "patchfrog/c.py": "",
            "patchfrog/n.py": "",
            "patchfrog/cond.py": "",
            "patchfrog/user.py": (
                "from __future__ import annotations\n"
                "from typing import TYPE_CHECKING\n"
                "import typing\n"
                "if TYPE_CHECKING:\n    import patchfrog.t\n"
                "if typing.TYPE_CHECKING:\n    import patchfrog.n\n"
                "try:\n    import patchfrog.cond\nexcept ImportError:\n    pass\n"
                "class K:\n    import patchfrog.c\n"
                "def f():\n    import patchfrog.l\n"
                "    def g():\n        if TYPE_CHECKING:\n            import patchfrog.t\n"
                "NOTE = 'import patchfrog.not_a_dependency'\n"
                "# import patchfrog.comment_only\n"
            ),
        },
    )
    graph = build_graph(root)
    by_target = {e.target: e for e in graph.edges if e.importer == "patchfrog.user" and e.internal}

    assert by_target["patchfrog.t"].kind is ImportKind.TYPE_CHECKING
    assert by_target["patchfrog.n"].kind is ImportKind.TYPE_CHECKING
    assert by_target["patchfrog.cond"].kind is ImportKind.MODULE  # conditional = conservative
    assert by_target["patchfrog.c"].kind is ImportKind.MODULE  # class bodies run at import
    assert by_target["patchfrog.l"].kind is ImportKind.LAZY
    assert by_target["patchfrog.l"].function == "f"
    assert set(by_target) == {"patchfrog.t", "patchfrog.n", "patchfrog.cond", "patchfrog.c", "patchfrog.l"}
    assert not any(e.target == "__future__" for e in graph.edges)


def test_closure_includes_parent_package_inits_and_reports_a_chain(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "patchfrog/__init__.py": "",
            "patchfrog/core.py": "import patchfrog.deep.leaf\n",
            "patchfrog/deep/__init__.py": "import sqlalchemy\n",
            "patchfrog/deep/leaf.py": "",
        },
    )
    internal, external = import_closure(build_graph(root), ["patchfrog.core"])

    assert "patchfrog.deep" in internal  # executed as a parent package init
    assert "sqlalchemy" in external
    assert any("(package init) patchfrog.deep" in step for step in external["sqlalchemy"].chain)


def test_unit_graph_and_scc_are_independent_of_import_order(tmp_path: Path) -> None:
    files = {
        "patchfrog/__init__.py": "",
        "patchfrog/a/__init__.py": "import patchfrog.b\nimport patchfrog.c\n",
        "patchfrog/b/__init__.py": "import patchfrog.a\n",
        "patchfrog/c/__init__.py": "",
    }
    first = build_graph(_tree(tmp_path / "one", files))
    reordered = dict(files)
    reordered["patchfrog/a/__init__.py"] = "import patchfrog.c\n\n\nimport patchfrog.b   # reordered + reformatted\n"
    second = build_graph(_tree(tmp_path / "two", reordered))

    assert unit_edges(first).keys() == unit_edges(second).keys()
    assert strongly_connected_components(unit_edges(first)) == [frozenset({"patchfrog.a", "patchfrog.b"})]
    assert strongly_connected_components(unit_edges(second)) == strongly_connected_components(unit_edges(first))


def test_overrides_never_touch_the_filesystem(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"patchfrog/__init__.py": "", "patchfrog/x.py": "x = 1\n"})
    graph = build_graph(root, overrides={"patchfrog/x.py": "import patchfrog.y\n", "patchfrog/y.py": ""})

    assert ("patchfrog.x", "patchfrog.y") in {(e.importer, e.target) for e in graph.edges}
    assert (root / "patchfrog/x.py").read_text() == "x = 1\n"
    assert not (root / "patchfrog/y.py").exists()
