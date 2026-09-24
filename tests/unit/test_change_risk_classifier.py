"""M4.1: the deterministic PR-level change/risk classifier."""

from __future__ import annotations

import pytest

from patchfrog.change_risk import (
    ChangeRiskPolicy,
    ChangeRiskSignal,
    ChangeRiskTier,
    FileChangeClass,
    classify_change,
)
from patchfrog.change_risk.comments import count_semantic_changed_lines, python_comment_lines
from patchfrog.change_risk.paths import classify_path, is_security_sensitive_path
from patchfrog.diff.models import DiffFile
from patchfrog.diff.parser import build_diff_file

S = ChangeRiskSignal
T = ChangeRiskTier


def _patch(removed: list[str], added: list[str], *, context: list[str] | None = None) -> str:
    ctx = context or []
    body = [f" {line}" for line in ctx] + [f"-{line}" for line in removed] + [f"+{line}" for line in added]
    return f"@@ -1,{len(ctx) + len(removed)} +1,{len(ctx) + len(added)} @@\n" + "\n".join(body) + "\n"


def _file(path: str, removed: list[str], added: list[str], *, context: list[str] | None = None) -> DiffFile:
    return build_diff_file(path, _patch(removed, added, context=context))


# -- NO_AI -------------------------------------------------------------------


def test_python_comment_only_change_is_no_ai() -> None:
    result = classify_change([_file("app/x.py", ["    # old words"], ["    # new words", ""], context=["def f():"])])
    assert result.tier is T.NO_AI
    assert result.no_ai_reason is S.COMMENT_ONLY
    assert result.semantic_changed_lines == 0


def test_c_style_line_and_block_comment_only_change_is_no_ai() -> None:
    added = ["// explain", "/* block", " * continued", " */", "/* one-liner */"]
    result = classify_change([_file("src/lib.ts", [], added)])
    assert result.tier is T.NO_AI


def test_docs_only_change_is_no_ai() -> None:
    result = classify_change([_file("README.md", ["a"], ["b"]), _file("docs/guide.rst", [], ["text"])])
    assert result.tier is T.NO_AI
    assert result.no_ai_reason is S.DOCS_ONLY


def test_mixed_docs_and_comment_only_is_no_ai_with_combined_reason() -> None:
    result = classify_change([_file("README.md", [], ["x"]), _file("app/y.py", [], ["# note"])])
    assert result.tier is T.NO_AI
    assert result.no_ai_reason is S.NON_CODE_ONLY


def test_generated_and_vendor_only_change_is_no_ai_when_policy_excludes_them() -> None:
    files = [_file("proto/api_pb2.py", [], ["X = 1"]), _file("vendor/lib/a.go", [], ["package a"])]
    assert classify_change(files).no_ai_reason is S.GENERATED_OR_VENDOR_ONLY
    reviewed = classify_change(files, policy=ChangeRiskPolicy(exclude_generated_and_vendor=False))
    assert reviewed.tier is not T.NO_AI


def test_lockfile_only_change_is_no_ai() -> None:
    result = classify_change([_file("poetry.lock", ['version = "1"'], ['version = "2"'])])
    assert result.tier is T.NO_AI
    assert result.no_ai_reason is S.LOCKFILE_ONLY


def test_empty_diff_is_no_ai() -> None:
    assert classify_change([]).no_ai_reason is S.EMPTY_DIFF


# -- conservative comment detection (never skip real code) -------------------


def test_c_preprocessor_hash_line_is_code_not_a_comment() -> None:
    result = classify_change([_file("src/a.c", ["#define LIMIT 1"], ["#define LIMIT 2"])])
    assert result.tier is not T.NO_AI


@pytest.mark.parametrize(
    "line",
    ["#!/usr/bin/env python3", "# -*- coding: latin-1 -*-", "x = []  # type: list[int]", "# type: ignore"],
)
def test_python_directive_comments_count_as_code(line: str) -> None:
    result = classify_change([_file("tool.py", [], [line])])
    assert result.tier is not T.NO_AI


def test_hash_line_inside_python_triple_quoted_string_is_never_comment_only() -> None:
    patch = '@@ -1,2 +1,3 @@\n SQL = """\n+# SELECT * FROM users\n """\n'
    diff = build_diff_file("q.py", patch)
    _semantic, comment_only = count_semantic_changed_lines(diff)
    assert not comment_only
    assert classify_change([diff]).tier is not T.NO_AI


def test_hash_line_after_a_closed_docstring_is_still_a_comment() -> None:
    patch = '@@ -1,2 +1,3 @@\n def f():\n     """Doc."""\n+    # clarified\n'
    assert classify_change([build_diff_file("q.py", patch)]).tier is T.NO_AI


def test_exact_tokenizer_evidence_catches_a_string_the_diff_window_cannot_see() -> None:
    """The one gap of the diff-only rule: a hunk entirely inside a
    multi-line string opened before and closed after the hunk window.
    With exact head evidence (what the service supplies) it is code."""

    source = 'TEMPLATE = """\nline\n# Heading\nmore\n"""\n'
    patch = "@@ -2,2 +2,3 @@\n line\n+# Heading\n more\n"
    diff = build_diff_file("render.py", patch)
    assert classify_change([diff]).tier is T.NO_AI  # the documented diff-only blind spot
    evidence = {"render.py": (python_comment_lines(source), None)}
    assert classify_change([diff], comment_lines=evidence).tier is not T.NO_AI


def test_python_comment_lines_is_exact() -> None:
    source = "x = 1  # trailing\n# own line\ns = '''\n# in string\n'''\n"
    assert python_comment_lines(source) == frozenset({2})


def test_c_dereference_line_starting_with_star_is_code() -> None:
    result = classify_change([_file("src/a.c", ["*ptr = 0;"], ["*ptr = 1;"])])
    assert result.tier is not T.NO_AI


def test_block_comment_followed_by_code_on_same_line_is_code() -> None:
    result = classify_change([_file("src/a.js", [], ["/* note */ doThing();"])])
    assert result.tier is not T.NO_AI


def test_go_build_tag_directive_is_code() -> None:
    assert classify_change([_file("main.go", [], ["//go:build linux"])]).tier is not T.NO_AI


def test_binary_code_file_without_hunks_is_never_no_ai_or_tiny() -> None:
    result = classify_change([DiffFile(path="lib/native.so.py", hunks=())])
    assert result.tier not in (T.NO_AI, T.TINY)


def test_unknown_language_whitespace_change_is_not_comment_only() -> None:
    assert classify_change([_file("data/config.json", ["{}"], ["{ }"])]).tier is not T.NO_AI


# -- tiers ---------------------------------------------------------------------


def test_small_code_change_is_tiny() -> None:
    result = classify_change([_file("app/calc.py", ["    return a - b"], ["    return a + b"], context=["def f(a, b):"])])
    assert result.tier is T.TINY
    assert result.has(S.SMALL_CHANGE)


def test_medium_single_module_change_is_normal() -> None:
    added = [f"    total += item_{i}" for i in range(30)]
    result = classify_change([_file("app/calc.py", [], added)])
    assert result.tier is T.NORMAL


def test_test_only_change_gets_larger_tiny_allowance_and_is_flagged() -> None:
    added = [f"    assert f({i}) == {i}" for i in range(40)]
    result = classify_change([_file("tests/test_calc.py", [], added)])
    assert result.tier is T.TINY
    assert result.has(S.TEST_ONLY)


def test_public_signature_change_is_elevated() -> None:
    result = classify_change([_file("app/api.py", ["def fetch(a):"], ["def fetch(a, b):"])])
    assert result.tier is T.ELEVATED
    assert result.has(S.PUBLIC_INTERFACE_CHANGE)


def test_private_helper_signature_change_is_not_a_public_interface_change() -> None:
    result = classify_change([_file("app/api.py", ["def _helper(a):"], ["def _helper(a, b):"])])
    assert not result.has(S.PUBLIC_INTERFACE_CHANGE)


def test_migration_is_elevated_and_migration_plus_schema_is_high_risk() -> None:
    migration = _file("migrations/versions/0001_add.py", [], ["op.add_column('t', col)"])
    assert classify_change([migration]).tier is T.ELEVATED
    model = _file("app/models.py", [], ["    email = Column(String)"])
    combined = classify_change([migration, model])
    assert combined.tier is T.HIGH_RISK
    assert combined.has(S.DATABASE_MIGRATION) and combined.has(S.SCHEMA_MODEL_CHANGE)


def test_security_sensitive_path_alone_is_elevated_never_high_risk() -> None:
    result = classify_change([_file("app/auth/tokens.py", ["    ttl = 60"], ["    ttl = 120"])])
    assert result.has(S.SECURITY_SENSITIVE_PATH)
    assert result.tier is T.ELEVATED


def test_security_path_corroborated_by_public_interface_change_is_high_risk() -> None:
    result = classify_change([_file("app/auth/login.py", ["def login(user, pw):"], ["def login(user, pw, otp):"])])
    assert result.tier is T.HIGH_RISK


def test_static_high_risk_finding_plus_security_path_is_high_risk() -> None:
    files = [_file("app/auth/tokens.py", ["    ttl = 60"], ["    ttl = 120"])]
    assert classify_change(files, static_high_risk_finding=True).tier is T.HIGH_RISK


def test_ci_workflow_and_manifest_changes_are_elevating_signals() -> None:
    ci = classify_change([_file(".github/workflows/ci.yml", ["on: push"], ["on: pull_request_target"])])
    assert ci.has(S.CI_CONFIG_CHANGE) and ci.tier is T.ELEVATED
    manifest = classify_change([_file("package.json", ['"stripe": "^11"'], ['"stripe": "^12"'])])
    assert manifest.has(S.DEPENDENCY_MANIFEST_CHANGE) and manifest.tier is T.ELEVATED


def test_cross_module_change_is_elevated() -> None:
    files = [_file(f"pkg/mod{i}/core.py", ["    x = 1"], ["    x = 2"]) for i in range(3)]
    result = classify_change(files)
    assert result.has(S.CROSS_MODULE)
    assert result.tier is T.ELEVATED


def test_deletion_heavy_change_is_flagged() -> None:
    result = classify_change([_file("app/legacy.py", [f"x{i} = {i}" for i in range(60)], ["pass"])])
    assert result.has(S.DELETION_HEAVY)


def test_classification_is_deterministic_and_order_independent() -> None:
    a = _file("app/auth/login.py", ["def login(u):"], ["def login(u, otp):"])
    b = _file("README.md", [], ["docs"])
    first = classify_change([a, b])
    second = classify_change([b, a])
    assert first == second


def test_to_dict_contains_counts_and_signals_only_never_line_content() -> None:
    secret_line = "API_KEY = 'PFTEST-FAKE-KEY-000'"
    result = classify_change([_file("app/settings.py", [], [secret_line])])
    serialized = str(result.to_dict())
    assert "PFTEST-FAKE-KEY" not in serialized
    assert result.to_dict()["tier"] == result.tier.value


# -- path classification --------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("README.md", FileChangeClass.DOCS),
        ("requirements.txt", FileChangeClass.MANIFEST),
        ("requirements-dev.txt", FileChangeClass.MANIFEST),
        ("yarn.lock", FileChangeClass.LOCKFILE),
        ("node_modules/x/index.js", FileChangeClass.VENDOR),
        ("web/app.min.js", FileChangeClass.GENERATED),
        ("tests/test_x.py", FileChangeClass.TEST),
        ("src/app.py", FileChangeClass.CODE),
        ("CMakeLists.txt", FileChangeClass.CODE),
    ],
)
def test_classify_path(path: str, expected: FileChangeClass) -> None:
    assert expected in classify_path(path)


def test_security_path_tokens_match_whole_tokens_only() -> None:
    assert is_security_sensitive_path("app/auth/views.py")
    assert is_security_sensitive_path("app/password_reset.py")
    assert not is_security_sensitive_path("app/author.py")
    assert not is_security_sensitive_path("app/tokenizer.py")
