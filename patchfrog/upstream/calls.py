"""Locate an SDK call at a known usage-site line and describe its arguments.

Shared by the consumer mapper (M6.5: "does this call actually pass the
argument that changed?") and the migration rewriters (M7.3: exact
character spans to edit). Python is located with the standard-library
``ast`` (exact spans); JS/TS with a small string/comment/template-aware
lexer -- the same "no JS parser in the engine" constraint M5's scanner
works under.

Every span is a pair of *character* offsets into the source string.
Nothing here executes, imports or evaluates repository code.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

PY_SUFFIXES = (".py", ".pyi")
JS_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")

_NO_LITERAL = object()


@dataclass(frozen=True, slots=True)
class Span:
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class KeywordArg:
    name: str
    name_span: Span
    value_span: Span
    #: The literal value (str/int/float/bool/None) or ``None`` with
    #: ``is_literal=False`` when the value is an expression.
    literal: Any
    is_literal: bool
    #: Python: the key was written as ``name=``; JS: ``{ name }`` shorthand.
    shorthand: bool = False


@dataclass(frozen=True, slots=True)
class LocatedCall:
    language: str  # "python" | "javascript"
    line: int
    #: The span of the matched chain. With a receiver it starts at the
    #: ``.`` before the first matched segment (``.chat.create``);
    #: without one it is the bare name (``OpenAI``).
    chain_span: Span
    has_receiver: bool
    call_span: Span
    open_paren: int
    close_paren: int
    keywords: tuple[KeywordArg, ...]
    #: ``**kwargs`` (Python) / object spread or a non-literal options
    #: argument (JS) -- keyword usage cannot be decided statically.
    indirect_keywords: bool
    positional_args: int
    #: End offset of the last argument (``None`` when there are none).
    last_arg_end: int | None
    #: JS only: the span of the options object literal ``{...}``.
    object_span: Span | None = None

    def keyword(self, name: str) -> KeywordArg | None:
        return next((k for k in self.keywords if k.name == name), None)


def language_for(path: str) -> str | None:
    suffix = PurePosixPath(path).suffix
    if suffix in PY_SUFFIXES:
        return "python"
    if suffix in JS_SUFFIXES:
        return "javascript"
    return None


def locate_calls(source: str, path: str, line: int, chain: str) -> list[LocatedCall]:
    language = language_for(path)
    if language == "python":
        return locate_python_calls(source, line, chain)
    if language == "javascript":
        return locate_js_calls(source, line, chain)
    return []


# -- Python ----------------------------------------------------------------------


class LineIndex:
    """(1-based line, UTF-8 byte column) -> character offset."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.lines = source.splitlines(keepends=True)
        self.starts: list[int] = []
        total = 0
        for text in self.lines:
            self.starts.append(total)
            total += len(text)
        self.length = total

    def offset(self, lineno: int, col_bytes: int) -> int:
        if lineno - 1 >= len(self.lines):
            return self.length
        text = self.lines[lineno - 1]
        return self.starts[lineno - 1] + len(text.encode("utf-8")[:col_bytes].decode("utf-8", errors="ignore"))

    def node_span(self, node: ast.AST) -> Span:
        return Span(
            self.offset(node.lineno, node.col_offset),  # type: ignore[attr-defined]
            self.offset(node.end_lineno or node.lineno, node.end_col_offset or 0),  # type: ignore[attr-defined]
        )


def dotted(node: ast.AST) -> list[str] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return list(reversed(parts))
    if parts:
        # A call on a non-name receiver (``get_client().chat.create``):
        # the chain is still meaningful after the receiver.
        parts.append("<expr>")
        return list(reversed(parts))
    return None


def _literal(node: ast.AST) -> tuple[Any, bool]:
    if isinstance(node, ast.Constant) and (node.value is None or isinstance(node.value, (str, int, float, bool))):
        return node.value, True
    return None, False


def parse_python(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError):
        return None


def python_calls_at(tree: ast.Module, index: LineIndex, line: int, chain: str) -> list[LocatedCall]:
    wanted = chain.split(".")
    found: list[LocatedCall] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or node.lineno != line:
            continue
        parts = dotted(node.func)
        if parts is None or len(parts) < len(wanted) or parts[-len(wanted):] != wanted:
            continue
        func_span = index.node_span(node.func)
        if len(parts) > len(wanted):
            receiver: ast.AST = node.func
            for _ in range(len(wanted)):
                receiver = receiver.value  # type: ignore[attr-defined]
            chain_span = Span(index.node_span(receiver).end, func_span.end)
            has_receiver = True
        else:
            chain_span = func_span
            has_receiver = False
        call_span = index.node_span(node)
        keywords: list[KeywordArg] = []
        indirect = False
        for kw in node.keywords:
            if kw.arg is None:
                indirect = True
                continue
            start = index.offset(kw.lineno, kw.col_offset)
            literal, is_literal = _literal(kw.value)
            keywords.append(
                KeywordArg(kw.arg, Span(start, start + len(kw.arg)), index.node_span(kw.value), literal, is_literal)
            )
        ends = [index.node_span(a).end for a in node.args] + [k.value_span.end for k in keywords]
        ends += [index.node_span(kw.value).end for kw in node.keywords if kw.arg is None]
        found.append(
            LocatedCall(
                language="python",
                line=line,
                chain_span=chain_span,
                has_receiver=has_receiver,
                call_span=call_span,
                open_paren=_python_open_paren(index.source, func_span.end),
                close_paren=call_span.end - 1,
                keywords=tuple(keywords),
                indirect_keywords=indirect or any(isinstance(a, ast.Starred) for a in node.args),
                positional_args=len(node.args),
                last_arg_end=max(ends) if ends else None,
            )
        )
    return found


def _python_open_paren(source: str, after: int) -> int:
    position = source.find("(", after)
    return position if position >= 0 else after


def locate_python_calls(source: str, line: int, chain: str) -> list[LocatedCall]:
    tree = parse_python(source)
    if tree is None:
        return []
    return python_calls_at(tree, LineIndex(source), line, chain)


# -- JavaScript / TypeScript --------------------------------------------------------


def js_code_mask(source: str) -> list[bool]:
    """``mask[i]`` is True when ``source[i]`` is code (not inside a
    string, comment or the literal text of a template)."""

    mask = [True] * len(source)
    i = 0
    n = len(source)
    # Stack of template-literal brace depths: inside ``${ ... }`` we are in
    # code again until the matching ``}``.
    template_stack: list[int] = []
    brace_depth = 0
    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            end = source.find("\n", i)
            end = n if end < 0 else end
            for j in range(i, end):
                mask[j] = False
            i = end
            continue
        if ch == "/" and nxt == "*":
            end = source.find("*/", i + 2)
            end = n if end < 0 else end + 2
            for j in range(i, end):
                mask[j] = False
            i = end
            continue
        if ch in ("'", '"'):
            j = i + 1
            while j < n and source[j] != ch and source[j] != "\n":
                j += 2 if source[j] == "\\" else 1
            end = min(j + 1, n)
            for k in range(i, end):
                mask[k] = False
            i = end
            continue
        if ch == "`" or (ch == "}" and template_stack and template_stack[-1] == brace_depth):
            if ch == "}":
                template_stack.pop()
            j = i + 1
            while j < n:
                if source[j] == "\\":
                    j += 2
                    continue
                if source[j] == "`":
                    break
                if source[j] == "$" and j + 1 < n and source[j + 1] == "{":
                    break
                j += 1
            if j < n and source[j] == "$":
                for k in range(i, j + 2):
                    mask[k] = False
                template_stack.append(brace_depth)
                i = j + 2
                continue
            end = min(j + 1, n)
            for k in range(i, end):
                mask[k] = False
            i = end
            continue
        if ch == "{":
            brace_depth += 1
        elif ch == "}":
            brace_depth -= 1
        i += 1
    return mask


def matching_close(source: str, mask: list[bool], open_index: int) -> int | None:
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = [pairs[source[open_index]]]
    i = open_index + 1
    while i < len(source):
        if mask[i]:
            ch = source[i]
            if ch in pairs:
                stack.append(pairs[ch])
            elif ch in ")]}":
                if not stack or stack[-1] != ch:
                    return None
                stack.pop()
                if not stack:
                    return i
        i += 1
    return None


def _line_bounds(source: str, line: int) -> tuple[int, int] | None:
    start = 0
    for _ in range(line - 1):
        nl = source.find("\n", start)
        if nl < 0:
            return None
        start = nl + 1
    end = source.find("\n", start)
    return start, (len(source) if end < 0 else end)


_JS_IDENT = r"[A-Za-z_$][\w$]*"
_JS_LITERAL_RE = re.compile(r"""^(?:(['"])((?:(?!\1)[^\\\n]|\\.)*)\1|(-?\d+(?:\.\d+)?)|(true|false|null))$""")


def _js_literal(text: str) -> tuple[Any, bool]:
    match = _JS_LITERAL_RE.match(text.strip())
    if match is None:
        return None, False
    if match.group(1):
        return match.group(2), True
    if match.group(3):
        number = match.group(3)
        return (float(number) if "." in number else int(number)), True
    return {"true": True, "false": False, "null": None}[match.group(4)], True


def _split_top_level(source: str, mask: list[bool], start: int, end: int) -> list[tuple[int, int]]:
    """Comma-separated segments of ``source[start:end]`` at depth 0."""

    segments: list[tuple[int, int]] = []
    depth = 0
    seg_start = start
    for i in range(start, end):
        if not mask[i]:
            continue
        ch = source[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            segments.append((seg_start, i))
            seg_start = i + 1
    segments.append((seg_start, end))
    return [(s, e) for s, e in segments if source[s:e].strip()]


def _strip_span(source: str, start: int, end: int) -> tuple[int, int]:
    while start < end and source[start].isspace():
        start += 1
    while end > start and source[end - 1].isspace():
        end -= 1
    return start, end


def _js_object_properties(
    source: str, mask: list[bool], open_brace: int, close_brace: int
) -> tuple[list[KeywordArg], bool]:
    props: list[KeywordArg] = []
    indirect = False
    for seg_start, seg_end in _split_top_level(source, mask, open_brace + 1, close_brace):
        s, e = _strip_span(source, seg_start, seg_end)
        text = source[s:e]
        if text.startswith("..."):
            indirect = True
            continue
        key_match = re.match(rf"""(?:({_JS_IDENT})|(['"])({_JS_IDENT})\2)\s*:""", text)
        if key_match is not None:
            group = 1 if key_match.group(1) else 3
            name = key_match.group(group)
            # For a quoted key the span covers the identifier only, so a
            # rename keeps the original quoting.
            name_start, name_end = s + key_match.start(group), s + key_match.end(group)
            value_start, value_end = _strip_span(source, s + key_match.end(), e)
            literal, is_literal = _js_literal(source[value_start:value_end])
            props.append(KeywordArg(name, Span(name_start, name_end), Span(value_start, value_end), literal, is_literal))
            continue
        shorthand = re.fullmatch(_JS_IDENT, text)
        if shorthand is not None:
            props.append(KeywordArg(text, Span(s, e), Span(s, e), None, False, shorthand=True))
            continue
        # Methods, computed keys, getters: not a plain named argument.
        indirect = True
    return props, indirect


def locate_js_calls(source: str, line: int, chain: str) -> list[LocatedCall]:
    bounds = _line_bounds(source, line)
    if bounds is None:
        return []
    mask = js_code_mask(source)
    parts = chain.split(".")
    joined = r"\s*\.\s*".join(re.escape(p) for p in parts)
    pattern = re.compile(rf"(?:(?<=[\w$\)\]])\s*\.\s*{joined}|(?<![\w$.]){joined})\s*\(")
    found: list[LocatedCall] = []
    line_start, line_end = bounds
    # A chain may continue on the next lines (``client.chat\n  .create(``);
    # M5 records the line the chain starts on, so only the start is bounded.
    for match in pattern.finditer(source, line_start):
        if match.start() >= line_end:
            break
        if not mask[match.start()]:
            continue
        has_receiver = source[match.start():match.end()].lstrip().startswith(".")
        open_paren = match.end() - 1
        close_paren = matching_close(source, mask, open_paren)
        if close_paren is None:
            continue
        chain_start = match.start()
        while chain_start < open_paren and source[chain_start].isspace():
            chain_start += 1
        chain_end = open_paren
        while chain_end > chain_start and source[chain_end - 1].isspace():
            chain_end -= 1
        args = _split_top_level(source, mask, open_paren + 1, close_paren)
        keywords: list[KeywordArg] = []
        indirect = False
        object_span: Span | None = None
        if args:
            first_start, first_end = _strip_span(source, *args[0])
            if source[first_start] == "{":
                close_brace = matching_close(source, mask, first_start)
                if close_brace is not None and close_brace == first_end - 1:
                    object_span = Span(first_start, close_brace + 1)
                    keywords, indirect = _js_object_properties(source, mask, first_start, close_brace)
                else:
                    indirect = True
            else:
                indirect = True
        last_arg_end = _strip_span(source, *args[-1])[1] if args else None
        found.append(
            LocatedCall(
                language="javascript",
                line=line,
                chain_span=Span(chain_start, chain_end),
                has_receiver=has_receiver,
                call_span=Span(chain_start, close_paren + 1),
                open_paren=open_paren,
                close_paren=close_paren,
                keywords=tuple(keywords),
                indirect_keywords=indirect,
                positional_args=max(0, len(args) - (1 if object_span else 0)),
                last_arg_end=last_arg_end,
                object_span=object_span,
            )
        )
    return found


__all__ = [
    "JS_SUFFIXES",
    "PY_SUFFIXES",
    "KeywordArg",
    "LineIndex",
    "LocatedCall",
    "Span",
    "dotted",
    "js_code_mask",
    "language_for",
    "locate_calls",
    "locate_js_calls",
    "locate_python_calls",
    "matching_close",
    "parse_python",
    "python_calls_at",
]
