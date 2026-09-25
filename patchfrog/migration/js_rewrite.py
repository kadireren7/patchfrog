"""Lexically-located span edits for JavaScript/TypeScript sources.

The engine has no JS parser (same constraint as M5's scanner), so edits
are located with the string/comment/template-aware lexer in
:mod:`patchfrog.upstream.calls`. The supported shapes are the common SDK
call forms: ``client.a.b({ key: value, ... })``, module specifiers in
``import``/``require``, and path literals. Anything else is a
``RewriteError`` (step FAILED), never a guess.
"""

from __future__ import annotations

import json
import re

from patchfrog.migration.domain import EditOperation, MigrationStep
from patchfrog.migration.edits import NotNeeded, RewriteError, TextEdit, line_of
from patchfrog.migration.path_rewrite import contains_path, replace_path
from patchfrog.upstream.calls import LocatedCall, js_code_mask, locate_js_calls

_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")


class _Source:
    def __init__(self, text: str, step: MigrationStep) -> None:
        self.text = text
        self.step = step
        self.line = step.target.line or 0

    def calls(self, chain: str) -> list[LocatedCall]:
        return locate_js_calls(self.text, self.line, chain)

    def edit(self, start: int, end: int, replacement: str, scope: tuple[int, int]) -> TextEdit:
        if "\n" in replacement:
            raise RewriteError("replacement would change line numbers")
        return TextEdit(self.step.target.file_path, start, end, replacement, self.step.step_id, scope)

    def call_scope(self, call: LocatedCall) -> tuple[int, int]:
        return (line_of(self.text, call.call_span.start), line_of(self.text, call.call_span.end))

    def line_bounds(self) -> tuple[int, int]:
        start = 0
        for _ in range(self.line - 1):
            start = self.text.index("\n", start) + 1
        end = self.text.find("\n", start)
        return start, (len(self.text) if end < 0 else end)


def _target_calls(src: _Source) -> list[LocatedCall]:
    calls = src.calls(src.step.target.usage_token)
    if not calls:
        raise RewriteError(f"no call of `{src.step.target.usage_token}` at line {src.line}")
    return calls


def _rename_call_chain(src: _Source, op: EditOperation) -> list[TextEdit]:
    old, new = op.get("old").split("."), op.get("new").split(".")
    calls = src.calls(".".join(old))
    if not calls:
        if src.calls(".".join(new)):
            raise NotNeeded(f"already calls `{'.'.join(new)}`")
        raise RewriteError(f"no call of `{'.'.join(old)}` at line {src.line}")
    edits: list[TextEdit] = []
    for call in calls:
        if not call.has_receiver:
            raise RewriteError("bare-name calls (imported names) are not renamed automatically")
        text = src.text[call.chain_span.start:call.chain_span.end]
        idents = list(_IDENT_RE.finditer(text))
        if [m.group() for m in idents] != old:
            raise RewriteError("unexpected call chain formatting")
        if len(old) == len(new):
            # Segment by segment: keeps any line breaks/indentation between them.
            for match, replacement in zip(idents, new, strict=True):
                if match.group() != replacement:
                    start = call.chain_span.start + match.start()
                    edits.append(src.edit(start, start + len(match.group()), replacement, src.call_scope(call)))
        elif "\n" not in text:
            edits.append(src.edit(call.chain_span.start, call.chain_span.end, "." + ".".join(new),
                                  src.call_scope(call)))
        else:
            raise RewriteError("chain length changes across lines")
    return edits


def _rename_keyword(src: _Source, op: EditOperation) -> list[TextEdit]:
    old, new = op.get("old"), op.get("new")
    edits: list[TextEdit] = []
    for call in _target_calls(src):
        prop, target = call.keyword(old), call.keyword(new)
        if prop is None:
            if target is None and call.indirect_keywords:
                raise RewriteError("options passed indirectly (spread / variable)")
            continue
        if target is not None:
            raise RewriteError(f"call passes both `{old}` and `{new}`")
        replacement = f"{new}: {old}" if prop.shorthand else new
        edits.append(src.edit(prop.name_span.start, prop.name_span.end, replacement, src.call_scope(call)))
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
        if call.indirect_keywords or (call.object_span is None and call.last_arg_end is not None):
            raise RewriteError("options passed indirectly; cannot tell whether it is set")
        if "value_json" in params:
            code = json.dumps(json.loads(params["value_json"]))
        else:
            source = call.keyword(params["from_parameter"])
            if source is None:
                raise RewriteError(f"call has no `{params['from_parameter']}` option to take the value from")
            code = source.name if source.shorthand else src.text[source.value_span.start:source.value_span.end]
        if call.object_span is None:
            edits.append(src.edit(call.close_paren, call.close_paren, f"{{ {name}: {code} }}", src.call_scope(call)))
            continue
        if call.keywords:
            last = max(k.value_span.end for k in call.keywords)
            edits.append(src.edit(last, last, f", {name}: {code}", src.call_scope(call)))
        else:
            inside = call.object_span.start + 1
            edits.append(src.edit(inside, inside, f" {name}: {code} ", src.call_scope(call)))
    if not edits:
        raise NotNeeded(f"every call already passes `{name}`")
    return edits


def _replace_keyword_literal(src: _Source, op: EditOperation) -> list[TextEdit]:
    name, old, new = op.get("name"), op.get("old"), op.get("new")
    edits: list[TextEdit] = []
    for call in _target_calls(src):
        prop = call.keyword(name)
        if prop is None:
            continue
        if not prop.is_literal:
            raise RewriteError(f"`{name}` is not a literal; its value cannot be mapped")
        if prop.literal != old:
            continue
        original = src.text[prop.value_span.start:prop.value_span.end]
        quote = original[0] if original[:1] in ("'", '"') else '"'
        escaped = new.replace("\\", "\\\\").replace(quote, "\\" + quote)
        edits.append(src.edit(prop.value_span.start, prop.value_span.end, f"{quote}{escaped}{quote}",
                              src.call_scope(call)))
    if not edits:
        raise NotNeeded(f"no call passes `{name}: {old!r}`")
    return edits


def _rename_import_module(src: _Source, op: EditOperation) -> list[TextEdit]:
    old, new = op.get("old"), op.get("new")
    start, end = src.line_bounds()
    line_text = src.text[start:end]
    pattern = re.compile(r"""(?:\bfrom\s*|\brequire\s*\(\s*|\bimport\s*\(\s*|^\s*import\s+)(['"])"""
                         + re.escape(old) + r"""(?=['"/])""")
    edits = [
        src.edit(start + m.end() - len(old), start + m.end(), new, (src.line, src.line))
        for m in pattern.finditer(line_text)
    ]
    if not edits:
        if re.search(r"""['"]""" + re.escape(new) + r"""['"/]""", line_text):
            raise NotNeeded(f"already imports `{new}`")
        raise RewriteError(f"no import of `{old}` at line {src.line}")
    return edits


def _replace_path_literal(src: _Source, op: EditOperation) -> list[TextEdit]:
    old, new = op.get("old"), op.get("new")
    start, end = src.line_bounds()
    mask = js_code_mask(src.text)
    edits = [
        src.edit(start + s, start + e, replacement, (src.line, src.line))
        for s, e, replacement in replace_path(src.text[start:end], old, new)
        if not mask[start + s]  # inside a string/template literal, never code
    ]
    if not edits:
        if contains_path(src.text[start:end], new):
            raise NotNeeded("already uses the new endpoint")
        raise RewriteError(f"endpoint path not found in a string literal at line {src.line}")
    return edits


_HANDLERS = {
    "rename_call_chain": _rename_call_chain,
    "rename_keyword": _rename_keyword,
    "add_keyword": _add_keyword,
    "replace_keyword_literal": _replace_keyword_literal,
    "rename_import_module": _rename_import_module,
    "replace_path_literal": _replace_path_literal,
}


def js_edits(step: MigrationStep, text: str) -> list[TextEdit]:
    assert step.operation is not None
    handler = _HANDLERS.get(step.operation.kind)
    if handler is None:
        raise RewriteError(f"{step.operation.kind} is not supported for JavaScript/TypeScript")
    return handler(_Source(text, step), step.operation)


__all__ = ["js_edits"]
