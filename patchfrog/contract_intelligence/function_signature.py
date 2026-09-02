"""Deterministic, structural parsing of a Python function/method
signature *string* -- never a second parser, never a guess about
semantic types.

The input is always :attr:`patchfrog.persistence.models.code_index.SymbolModel.signature`
(or the equivalent freshly-parsed :attr:`patchfrog.domain.code.ParsedSymbol.signature`
for a base-commit symbol) -- the exact ``def``/``async def`` header text
:mod:`patchfrog.parsing.python` already extracts from the real Tree-sitter
parse (decorators prefixed on their own lines, trailing ``:`` already
stripped). This module only re-derives the *structure* already present
in that text (parameter names, defaults, ``*args``/``**kwargs``,
keyword-only/positional-only markers, the return annotation, ``async``)
via a small bracket/quote-depth-aware tokenizer -- it never resolves,
evaluates, or type-checks an annotation or default expression; both are
kept as opaque, un-interpreted text (spec section 3: "Do NOT infer
semantic types not present in source/index metadata").

Only Python is supported -- see
``validation/contract_intelligence/latest-summary.md`` section 1 for why
C/C++ signature structure is deferred rather than risked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_DEF_RE = re.compile(r"(?:^|\n)[ \t]*(async\s+def|def)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ParameterKind(StrEnum):
    POSITIONAL_ONLY = "positional_only"
    POSITIONAL_OR_KEYWORD = "positional_or_keyword"
    VAR_POSITIONAL = "var_positional"
    KEYWORD_ONLY = "keyword_only"
    VAR_KEYWORD = "var_keyword"


@dataclass(frozen=True, slots=True)
class Parameter:
    name: str
    kind: ParameterKind
    has_annotation: bool
    annotation_text: str | None
    has_default: bool
    default_text: str | None


@dataclass(frozen=True, slots=True)
class FunctionSignature:
    name: str
    is_async: bool
    parameters: tuple[Parameter, ...]
    has_return_annotation: bool
    return_annotation_text: str | None

    def parameter(self, name: str) -> Parameter | None:
        for p in self.parameters:
            if p.name == name:
                return p
        return None

    @property
    def named_parameters(self) -> tuple[Parameter, ...]:
        """Real, nameable parameters -- excludes bare ``*``/``/`` markers
        (which never reach here as a `Parameter` at all) but *includes*
        ``*args``/``**kwargs`` (callers needing only the addressable
        set filter those out explicitly by `kind`)."""

        return self.parameters


def parse_python_signature(signature_text: str) -> FunctionSignature | None:
    """Returns ``None`` (never a guess) when the text doesn't contain a
    recognizable ``def``/``async def`` header -- e.g. a class/module/
    macro symbol's signature, or genuinely malformed input."""

    match = _DEF_RE.search(signature_text)
    if match is None:
        return None

    is_async = match.group(1).startswith("async")
    name = match.group(2)
    paren_open = match.end() - 1  # the '(' the regex matched
    paren_close = _find_matching_close(signature_text, paren_open)
    if paren_close is None:
        return None

    params_text = signature_text[paren_open + 1 : paren_close]
    remainder = signature_text[paren_close + 1 :].strip()

    has_return_annotation = False
    return_annotation_text: str | None = None
    if remainder.startswith("->"):
        return_annotation_text = remainder[2:].strip() or None
        has_return_annotation = return_annotation_text is not None

    parameters = _parse_parameters(params_text)
    if any(_IDENTIFIER_RE.fullmatch(p.name) is None for p in parameters):
        # A malformed/unrecognizable parameter name means the top-level
        # comma-split was fooled by something this tokenizer doesn't
        # model (e.g. a multi-parameter `lambda x, y: ...` default,
        # whose own comma isn't bracket-enclosed) -- fail the *whole*
        # parse rather than return a silently wrong parameter list that
        # could produce a false-positive delta. See
        # ``docs/contract-intelligence.md``'s Limitations.
        return None

    return FunctionSignature(
        name=name,
        is_async=is_async,
        parameters=parameters,
        has_return_annotation=has_return_annotation,
        return_annotation_text=return_annotation_text,
    )


def _is_word_boundary(s: str, idx: int) -> bool:
    return idx < 0 or idx >= len(s) or not (s[idx].isalnum() or s[idx] == "_")


def _top_level_mask(s: str) -> list[bool]:
    """``True`` at index ``i`` iff ``s[i]`` sits at bracket depth 0,
    outside any string literal, and outside an unparenthesized
    ``lambda ...:`` parameter list -- the shared scan every top-level
    split/find in this module is built on.

    The ``lambda`` case matters because ``lambda x, y: ...`` used as a
    default value has a real top-level comma (``x, y``) with no
    enclosing bracket at all -- without tracking it separately, that
    comma would be indistinguishable from the outer parameter-list's
    own separator and silently produce a wrong parameter split (see
    ``docs/contract-intelligence.md``'s Limitations for the one shape
    still not handled: a `lambda` default nested inside brackets that
    themselves contain the closing `:` ambiguously)."""

    mask = [False] * len(s)
    depth = 0
    lambda_depth = 0
    quote: str | None = None
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if quote is not None:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            i += 1
            continue
        if c in "([{":
            depth += 1
            i += 1
            continue
        if c in ")]}":
            depth -= 1
            i += 1
            continue
        if (
            depth == 0
            and c == "l"
            and s[i : i + 6] == "lambda"
            and _is_word_boundary(s, i - 1)
            and _is_word_boundary(s, i + 6)
        ):
            lambda_depth += 1
            i += 6
            continue
        if c == ":" and depth == 0 and lambda_depth > 0:
            lambda_depth -= 1
            i += 1
            continue
        if depth == 0 and lambda_depth == 0:
            mask[i] = True
        i += 1
    return mask


def _find_matching_close(s: str, open_idx: int) -> int | None:
    depth = 0
    quote: str | None = None
    i = open_idx
    n = len(s)
    while i < n:
        c = s[i]
        if quote is not None:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _split_top_level(s: str, sep: str) -> list[str]:
    mask = _top_level_mask(s)
    parts: list[str] = []
    current: list[str] = []
    for i, c in enumerate(s):
        if mask[i] and c == sep:
            parts.append("".join(current))
            current = []
        else:
            current.append(c)
    parts.append("".join(current))
    return parts


def _find_top_level(s: str, ch: str) -> int | None:
    mask = _top_level_mask(s)
    for i, c in enumerate(s):
        if mask[i] and c == ch:
            return i
    return None


def _parse_parameters(params_text: str) -> tuple[Parameter, ...]:
    raw_tokens = [t.strip() for t in _split_top_level(params_text, ",")]
    raw_tokens = [t for t in raw_tokens if t]

    keyword_only_from: int | None = None
    positional_only_until: int | None = None
    for idx, token in enumerate(raw_tokens):
        if token == "*" or (token.startswith("*") and not token.startswith("**")):
            # A bare `*` or a `*args` both open the keyword-only section
            # for every parameter *after* them (a `*args` parameter
            # itself is VAR_POSITIONAL, not keyword-only).
            keyword_only_from = idx
        elif token == "/":
            positional_only_until = idx

    parameters: list[Parameter] = []
    for idx, token in enumerate(raw_tokens):
        if token in ("*", "/"):
            continue

        if token.startswith("**"):
            body = token[2:].strip()
            n, ann, has_ann = _split_annotation(body)
            parameters.append(
                Parameter(
                    name=n,
                    kind=ParameterKind.VAR_KEYWORD,
                    has_annotation=has_ann,
                    annotation_text=ann,
                    has_default=False,
                    default_text=None,
                )
            )
            continue

        if token.startswith("*"):
            body = token[1:].strip()
            n, ann, has_ann = _split_annotation(body)
            parameters.append(
                Parameter(
                    name=n,
                    kind=ParameterKind.VAR_POSITIONAL,
                    has_annotation=has_ann,
                    annotation_text=ann,
                    has_default=False,
                    default_text=None,
                )
            )
            continue

        eq_idx = _find_top_level(token, "=")
        if eq_idx is not None:
            head, default_text = token[:eq_idx].strip(), token[eq_idx + 1 :].strip()
            has_default = True
        else:
            head, default_text = token, None
            has_default = False

        name, annotation_text, has_annotation = _split_annotation(head)
        if not name:
            continue  # malformed/unrecognizable token -- never guess a name

        if positional_only_until is not None and idx <= positional_only_until:
            kind = ParameterKind.POSITIONAL_ONLY
        elif keyword_only_from is not None and idx > keyword_only_from:
            kind = ParameterKind.KEYWORD_ONLY
        else:
            kind = ParameterKind.POSITIONAL_OR_KEYWORD

        parameters.append(
            Parameter(
                name=name,
                kind=kind,
                has_annotation=has_annotation,
                annotation_text=annotation_text,
                has_default=has_default,
                default_text=default_text,
            )
        )

    return tuple(parameters)


def _split_annotation(head: str) -> tuple[str, str | None, bool]:
    """``head`` is a parameter's ``name`` or ``name: Annotation`` part
    (default already stripped, if any). Returns ``(name, annotation,
    has_annotation)``."""

    colon_idx = _find_top_level(head, ":")
    if colon_idx is None:
        return head.strip(), None, False
    name = head[:colon_idx].strip()
    annotation = head[colon_idx + 1 :].strip()
    return name, (annotation or None), bool(annotation)
