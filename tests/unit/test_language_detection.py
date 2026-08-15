from __future__ import annotations

from patchfrog.domain.code import Language
from patchfrog.parsing.detect import detect_language


def test_detects_python_by_extension() -> None:
    assert detect_language(relative_path="a/b/mod.py") is Language.PYTHON


def test_detects_c_by_extension() -> None:
    assert detect_language(relative_path="src/main.c") is Language.C


def test_detects_cpp_by_extensions() -> None:
    for suffix in (".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx"):
        assert detect_language(relative_path=f"src/main{suffix}") is Language.CPP


def test_unsupported_extension_returns_none() -> None:
    assert detect_language(relative_path="README.md") is None
    assert detect_language(relative_path="config.json") is None


def test_ambiguous_header_without_content_defaults_to_c() -> None:
    assert detect_language(relative_path="node.h") is Language.C


def test_ambiguous_header_with_cpp_markers_detected_as_cpp() -> None:
    content = b"namespace patchfrog {\nclass Widget {};\n}\n"
    assert detect_language(relative_path="widget.h", content=content) is Language.CPP


def test_ambiguous_header_with_plain_c_content_detected_as_c() -> None:
    content = b"typedef struct s_node { void *content; } t_node;\n"
    assert detect_language(relative_path="node.h", content=content) is Language.C
