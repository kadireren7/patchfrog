"""AST-located span edits for Python sources.

``ast`` finds exactly *what* to change (the call at the usage-site line,
its keyword, the import, the result variable); only those character
spans are replaced, so everything else in the file keeps its formatting.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator

from patchfrog.migration.domain import EditOperation, MigrationStep
from patchfrog.migration.edits import NotNeeded, RewriteError, TextEdit, line_of
from patchfrog.migration.path_rewrite import contains_path, replace_path
from patchfrog.upstream.calls import LineIndex, LocatedCall, dotted, parse_python, python_calls_at

_STRING_PREFIX_RE = re.compile(r"^([rRbBuUfF]*)('''|\"\"\"|'|\")")


def python_literal(value_json: str) -> str:
    value = json.loads(value_json)
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "True" if value else "False"
    return repr(value) if isinstance(value, str) else json.dumps(value)


def _requote(original: str, value: str) -> str:
    """A new string literal in the original's quote style."""

    match = _STRING_PREFIX_RE.match(original)
    prefix, quote = (match.group(1), match.group(2)) if match else ("", '"')
    escaped = value.replace("\\", "\\\\").replace(quote[0], "\\" + quote[0])
    return f"{prefix}{quote}{escaped}{quote}"


class _Source:
    def __init__(self, text: str, step: MigrationStep) -> None:
        tree = parse_python(text)
        if tree is None:
            raise RewriteError("file does not parse")
        self.text = text
        self.tree = tree
        self.index = LineIndex(text)
        self.step = step
        self.line = step.target.line or 0

    def calls(self, chain: str) -> list[LocatedCall]:
        return python_calls_at(self.tree, self.index, self.line, chain)

    def edit(self, start: int, end: int, replacement: str, scope: tuple[int, int] | None = None,
             role: str = "primary") -> TextEdit:
        if "\n" in replacement:
            raise RewriteError("replacement would change line numbers")
        return TextEdit(self.step.target.file_path, start, end, replacement, self.step.step_id,
                        scope or (self.line, max(self.line, line_of(self.text, end))), role)

    def call_scope(self, call: LocatedCall) -> tuple[int, int]:
        return (line_of(self.text, call.call_span.start), line_of(self.text, call.call_span.end))


def _target_calls(src: _Source) -> list[LocatedCall]:
    calls = src.calls(src.step.target.usage_token)
    if not calls:
        raise RewriteError(f"no call of `{src.step.target.usage_token}` at line {src.line}")
    return calls


def _rename_call_chain(src: _Source, op: EditOperation) -> list[TextEdit]:
    old, new = op.get("old"), op.get("new")
    calls = src.calls(old)
    if not calls:
        if src.calls(new):
            raise NotNeeded(f"already calls `{new}`")
        raise RewriteError(f"no call of `{old}` at line {src.line}")
    edits = []
    for call in calls:
        text = src.text[call.chain_span.start:call.chain_span.end]
        if not call.has_receiver:
            raise RewriteError("bare-name calls (imported names) are not renamed automatically")
        if "\n" in text or re.sub(r"\s+", "", text) != "." + old:
            raise RewriteError("call chain spans lines or has unexpected formatting")
        edits.append(src.edit(call.chain_span.start, call.chain_span.end, "." + new, src.call_scope(call)))
    return edits


def _rename_keyword(src: _Source, op: EditOperation) -> list[TextEdit]:
    old, new = op.get("old"), op.get("new")
    edits: list[TextEdit] = []
    for call in _target_calls(src):
        keyword, target = call.keyword(old), call.keyword(new)
        if keyword is None:
            if target is not None:
                continue  # already migrated
            if call.indirect_keywords:
                raise RewriteError("arguments passed indirectly (**kwargs)")
            continue  # this call does not pass the argument
        if target is not None:
            raise RewriteError(f"call passes both `{old}` and `{new}`")
        edits.append(src.edit(keyword.name_span.start, keyword.name_span.end, new, src.call_scope(call)))
    if not edits:
        raise NotNeeded(f"no call passes `{old}`")
    return edits


def _add_keyword(src: _Source, op: EditOperation) -> list[TextEdit]:
    params = dict(op.params)
    name = params["name"]
    edits: list[TextEdit] = []
    for call in _target_calls(src):
        if call.keyword(name) is not None:
            continue
        if call.indirect_keywords:
            raise RewriteError("arguments passed indirectly (**kwargs); cannot tell whether it is set")
        if "value_json" in params:
            code = python_literal(params["value_json"])
        else:
            source = call.keyword(params["from_parameter"])
            if source is None:
                raise RewriteError(f"call has no `{params['from_parameter']}` argument to take the value from")
            code = src.text[source.value_span.start:source.value_span.end]
            if "\n" in code:
                raise RewriteError("source argument spans lines")
        if call.last_arg_end is None:
            edits.append(src.edit(call.close_paren, call.close_paren, f"{name}={code}", src.call_scope(call)))
        else:
            edits.append(src.edit(call.last_arg_end, call.last_arg_end, f", {name}={code}", src.call_scope(call)))
    if not edits:
        raise NotNeeded(f"every call already passes `{name}`")
    return edits


def _replace_keyword_literal(src: _Source, op: EditOperation) -> list[TextEdit]:
    name, old, new = op.get("name"), op.get("old"), op.get("new")
    edits: list[TextEdit] = []
    for call in _target_calls(src):
        keyword = call.keyword(name)
        if keyword is None:
            continue
        if not keyword.is_literal:
            raise RewriteError(f"`{name}` is not a literal; its value cannot be mapped")
        if keyword.literal != old:
            continue
        original = src.text[keyword.value_span.start:keyword.value_span.end]
        edits.append(src.edit(keyword.value_span.start, keyword.value_span.end, _requote(original, new),
                              src.call_scope(call)))
    if not edits:
        raise NotNeeded(f"no call passes `{name}={old!r}`")
    return edits


def _module_matches(module: str, old: str) -> bool:
    return module == old or module.startswith(old + ".")


def _rename_import_module(src: _Source, op: EditOperation) -> list[TextEdit]:
    old, new = op.get("old"), op.get("new")
    edits: list[TextEdit] = []
    whole_file = (1, len(src.index.lines) or 1)
    plain_imports = False
    for node in ast.walk(src.tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and node.lineno == src.line \
                and _module_matches(node.module, old):
            start = src.index.offset(node.lineno, node.col_offset)
            segment = src.text[start:src.index.offset(node.end_lineno or node.lineno, node.end_col_offset or 0)]
            match = re.match(r"from\s+", segment)
            if match is None or not segment[match.end():].startswith(old):
                raise RewriteError("unexpected import formatting")
            name_start = start + match.end()
            edits.append(src.edit(name_start, name_start + len(old), new))
        elif isinstance(node, ast.Import) and node.lineno == src.line:
            for alias in node.names:
                if not _module_matches(alias.name, old):
                    continue
                name_start = src.index.offset(alias.lineno, alias.col_offset)
                if not src.text.startswith(old, name_start):
                    raise RewriteError("unexpected import formatting")
                edits.append(src.edit(name_start, name_start + len(old), new))
                plain_imports = plain_imports or alias.asname is None
    if not edits:
        raise NotNeeded(f"no import of `{old}` at line {src.line}")
    if plain_imports:
        # ``import a.legacy`` -> references are spelled ``a.legacy.x`` in code.
        edits.extend(_module_references(src, old, new, whole_file))
    return edits


def _module_references(src: _Source, old: str, new: str, scope: tuple[int, int]) -> Iterator[TextEdit]:
    wanted = old.split(".")
    for node in ast.walk(src.tree):
        if isinstance(node, ast.Attribute) and dotted(node) == wanted and node.lineno != src.line:
            span = src.index.node_span(node)
            yield src.edit(span.start, span.end, new, scope, role="secondary")


def _enclosing_scope(src: _Source) -> tuple[ast.AST, tuple[int, int]]:
    best: ast.AST = src.tree
    scope = (1, len(src.index.lines) or 1)
    for node in ast.walk(src.tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.lineno <= src.line <= (node.end_lineno or node.lineno)
            and (best is src.tree or node.lineno >= scope[0])
        ):
            best, scope = node, (node.lineno, node.end_lineno or node.lineno)
    return best, scope


def _rename_result_field(src: _Source, op: EditOperation) -> list[TextEdit]:
    old, new = op.get("old"), op.get("new")
    calls = {(c.call_span.start, c.call_span.end) for c in _target_calls(src)}
    scope_node, scope = _enclosing_scope(src)

    def is_target_call(node: ast.AST) -> bool:
        if isinstance(node, ast.Await):
            node = node.value
        if not isinstance(node, ast.Call):
            return False
        span = src.index.node_span(node)
        return (span.start, span.end) in calls

    variables: set[str] = set()
    edits: list[TextEdit] = []
    for node in ast.walk(scope_node):
        if isinstance(node, ast.Assign) and is_target_call(node.value) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            variables.add(node.targets[0].id)
        elif isinstance(node, ast.AnnAssign) and node.value is not None and is_target_call(node.value) \
                and isinstance(node.target, ast.Name):
            variables.add(node.target.id)
    for node in ast.walk(scope_node):
        if isinstance(node, ast.Attribute) and node.attr == old and (
            (isinstance(node.value, ast.Name) and node.value.id in variables) or is_target_call(node.value)
        ):
            span = src.index.node_span(node)
            edits.append(src.edit(span.end - len(old), span.end, new, scope, role="secondary"))
        elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in variables \
                and isinstance(node.slice, ast.Constant) and node.slice.value == old:
            span = src.index.node_span(node.slice)
            edits.append(src.edit(span.start, span.end, _requote(src.text[span.start:span.end], new), scope,
                                  role="secondary"))
    if not edits:
        raise NotNeeded(f"result field `{old}` is not read in this function")
    return edits


def _replace_path_literal(src: _Source, op: EditOperation) -> list[TextEdit]:
    old, new = op.get("old"), op.get("new")
    edits: list[TextEdit] = []
    for node in ast.walk(src.tree):
        if not isinstance(node, (ast.Constant, ast.JoinedStr)) or getattr(node, "lineno", 0) != src.line:
            continue
        if isinstance(node, ast.Constant) and not isinstance(node.value, str):
            continue
        span = src.index.node_span(node)
        literal = src.text[span.start:span.end]
        for start, end, replacement in replace_path(literal, old, new):
            edits.append(src.edit(span.start + start, span.start + end, replacement))
    if not edits:
        line_text = src.index.lines[src.line - 1] if 0 < src.line <= len(src.index.lines) else ""
        if contains_path(line_text, new):
            raise NotNeeded("already uses the new endpoint")
        raise RewriteError(f"endpoint path not found in a string literal at line {src.line}")
    return edits


_HANDLERS = {
    "rename_call_chain": _rename_call_chain,
    "rename_keyword": _rename_keyword,
    "add_keyword": _add_keyword,
    "replace_keyword_literal": _replace_keyword_literal,
    "rename_import_module": _rename_import_module,
    "rename_result_field": _rename_result_field,
    "replace_path_literal": _replace_path_literal,
}


def python_edits(step: MigrationStep, text: str) -> list[TextEdit]:
    assert step.operation is not None
    handler = _HANDLERS.get(step.operation.kind)
    if handler is None:
        raise RewriteError(f"{step.operation.kind} is not supported for Python")
    return handler(_Source(text, step), step.operation)


__all__ = ["python_edits", "python_literal"]
