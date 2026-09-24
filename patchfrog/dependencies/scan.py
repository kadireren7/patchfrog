"""Deterministic source scanners: imports, SDK constructors/call chains,
HTTP endpoint hosts and environment-variable *names*.

Two front ends, one output type (:class:`SourceObservation`):

- Python: the standard-library ``ast`` -- exact, never executes code.
- JavaScript/TypeScript: a lexical scanner (comments and template
  literals stripped first), because PatchFrog ships no JS parser; it only
  recognises explicit ``import``/``require`` forms and names bound by them.

Neither front end ever returns a source line or a literal value: only
module names, identifier chains, hostnames (plus a sanitized path for
hosts an adapter marks as API hosts) and ``UPPER_SNAKE`` variable names.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit


class ObservationKind(StrEnum):
    IMPORT = "import"
    CONSTRUCTOR = "constructor"
    CALL = "call"
    URL = "url"
    ENV_VAR = "env_var"
    STRING_PATH = "string_path"


@dataclass(frozen=True, slots=True)
class SourceObservation:
    kind: ObservationKind
    line: int
    #: ``IMPORT``: the imported module (e.g. ``openai``, ``stripe``).
    #: ``CONSTRUCTOR``/``CALL``: ``module`` it resolves to.
    #: ``URL``: the lower-cased hostname. ``ENV_VAR``: the variable name.
    #: ``STRING_PATH``: a literal that looks like an API path (``/v1/x``).
    subject: str
    #: ``CONSTRUCTOR``/``CALL``: the identifier chain after the bound
    #: name (``chat.completions.create``); ``URL``: the sanitized path.
    detail: str = ""


_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_IDENT_CHAIN_RE = re.compile(r"^[A-Za-z_][\w.]{0,255}$")
_API_PATH_RE = re.compile(r"^/[A-Za-z0-9_{}\-./:]{2,200}$")
_TOKENISH_SEGMENT_RE = re.compile(r"^(?=.*\d)[A-Za-z0-9_\-]{20,}$")


def url_host(value: str) -> str | None:
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    return parts.hostname.lower()


def sanitized_url_path(value: str) -> str:
    """The URL path with query/fragment dropped, placeholders normalised
    to ``{}`` and token-looking segments redacted -- safe to store for an
    API host (tokens travel in headers, but never trust that)."""

    try:
        path = urlsplit(value.strip()).path
    except ValueError:
        return ""
    segments: list[str] = []
    for segment in path.split("/"):
        if not segment:
            continue
        if "{" in segment or "%" in segment:
            segments.append("{}")
        elif _TOKENISH_SEGMENT_RE.match(segment):
            segments.append("{redacted}")
        else:
            segments.append(segment[:64])
    return "/" + "/".join(segments) if segments else "/"


# -- Python --------------------------------------------------------------------


def _dotted(node: ast.AST) -> list[str] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return list(reversed(parts))
    return None


def _string_parts(node: ast.AST) -> Iterable[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    elif isinstance(node, ast.JoinedStr):
        rendered = "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{}" for v in node.values
        )
        yield rendered


def scan_python(source: str, *, tracked_modules: frozenset[str]) -> list[SourceObservation]:
    """``tracked_modules``: top-level module names any adapter cares
    about. Names bound to them (by import or by assigning a constructor
    call on them) are followed to call chains."""

    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []

    observations: list[SourceObservation] = []
    bound: dict[str, str] = {}  # local name -> tracked top-level module
    instances: dict[str, str] = {}  # variable/attribute chain -> module

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                observations.append(SourceObservation(ObservationKind.IMPORT, node.lineno, alias.name))
                if root in tracked_modules:
                    bound[(alias.asname or alias.name).split(".")[0]] = root
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            root = node.module.split(".")[0]
            observations.append(SourceObservation(ObservationKind.IMPORT, node.lineno, node.module))
            if root in tracked_modules:
                for alias in node.names:
                    if alias.name != "*":
                        bound[alias.asname or alias.name] = root

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call):
            chain = _dotted(node.value.func)
            if chain and chain[0] in bound:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    target_chain = _dotted(target)
                    if target_chain:
                        instances[".".join(target_chain)] = bound[chain[0]]

    fstring_parts = {
        id(value) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr) for value in node.values
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            chain = _dotted(node.func)
            if chain:
                observation = _python_call_observation(chain, node.lineno, bound, instances)
                if observation is not None:
                    observations.append(observation)
            observations.extend(_python_env_observation(node))
        elif isinstance(node, ast.Subscript):
            base = _dotted(node.value)
            if base and base[-1] == "environ" and isinstance(node.slice, ast.Constant):
                name = node.slice.value
                if isinstance(name, str) and _ENV_NAME_RE.match(name):
                    observations.append(SourceObservation(ObservationKind.ENV_VAR, node.lineno, name))
        elif isinstance(node, (ast.Constant, ast.JoinedStr)) and id(node) not in fstring_parts:
            lineno = getattr(node, "lineno", 0)
            for text in _string_parts(node):
                observations.extend(_string_observations(text, lineno))
    return sorted(set(observations), key=lambda o: (o.line, o.kind.value, o.subject, o.detail))


def _python_call_observation(
    chain: list[str], line: int, bound: dict[str, str], instances: dict[str, str]
) -> SourceObservation | None:
    for split in range(len(chain) - 1, 0, -1):
        receiver = ".".join(chain[:split])
        if receiver in instances:
            detail = ".".join(chain[split:])
            if _IDENT_CHAIN_RE.match(detail):
                return SourceObservation(ObservationKind.CALL, line, instances[receiver], detail)
    if chain[0] in bound:
        module = bound[chain[0]]
        rest = ".".join(chain[1:])
        if not rest:
            return SourceObservation(ObservationKind.CONSTRUCTOR, line, module, chain[0])
        if len(chain) == 2 and chain[1][:1].isupper():
            # ``openai.OpenAI()`` / ``stripe.StripeClient()`` -- a client class.
            return SourceObservation(ObservationKind.CONSTRUCTOR, line, module, chain[1])
        if _IDENT_CHAIN_RE.match(rest):
            return SourceObservation(ObservationKind.CALL, line, module, rest)
    return None


def _python_env_observation(call: ast.Call) -> list[SourceObservation]:
    chain = _dotted(call.func)
    if not chain or not call.args:
        return []
    is_env_call = chain[-2:] in (["environ", "get"], ["os", "getenv"]) or chain == ["getenv"]
    first = call.args[0]
    if (
        is_env_call
        and isinstance(first, ast.Constant)
        and isinstance(first.value, str)
        and _ENV_NAME_RE.match(first.value)
    ):
        return [SourceObservation(ObservationKind.ENV_VAR, call.lineno, first.value)]
    return []


def _string_observations(text: str, line: int) -> list[SourceObservation]:
    stripped = text.strip()
    if stripped.startswith(("http://", "https://")) and " " not in stripped:
        host = url_host(stripped)
        if host:
            return [SourceObservation(ObservationKind.URL, line, host, sanitized_url_path(stripped))]
        return []
    if _API_PATH_RE.match(stripped) and stripped.count("/") >= 2:
        return [SourceObservation(ObservationKind.STRING_PATH, line, stripped)]
    return []


# -- JavaScript / TypeScript -----------------------------------------------------

_JS_IMPORT_DEFAULT_RE = re.compile(
    r"^\s*import\s+(?:type\s+)?([A-Za-z_$][\w$]*)?\s*,?\s*(?:\{([^}]*)\})?\s*(?:\*\s+as\s+([A-Za-z_$][\w$]*))?"
    r"\s*from\s+['\"]([^'\"]+)['\"]",
)
_JS_REQUIRE_RE = re.compile(
    r"(?:const|let|var)\s+(?:([A-Za-z_$][\w$]*)|\{([^}]*)\})\s*=\s*require\(\s*['\"]([^'\"]+)['\"]\s*\)(\s*\()?"
)
_JS_BARE_IMPORT_RE = re.compile(r"^\s*import\s+['\"]([^'\"]+)['\"]")
_JS_STRING_RE = re.compile(r"'([^'\n]{1,500})'|\"([^\"\n]{1,500})\"|`([^`]{1,500})`")
_JS_ENV_RE = re.compile(r"process\.env(?:\.([A-Z][A-Z0-9_]{1,127})|\[\s*['\"]([A-Z][A-Z0-9_]{1,127})['\"]\s*\])")


def _mask_comments(source: str) -> str:
    """Replace ``//`` and ``/* */`` comments with spaces, keeping newlines
    (line numbers stay exact) -- string-aware, so ``"https://..."`` or a
    ``//`` inside a template literal is never mistaken for a comment."""

    out = list(source)
    i, n = 0, len(source)
    quote: str | None = None
    while i < n:
        ch = source[i]
        if quote is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == quote or (ch == "\n" and quote != "`"):
                quote = None
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
            i += 1
            continue
        if source.startswith("//", i):
            end = source.find("\n", i)
            end = n if end == -1 else end
            for j in range(i, end):
                out[j] = " "
            i = end
            continue
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            end = n if end == -1 else end + 2
            for j in range(i, end):
                if out[j] != "\n":
                    out[j] = " "
            i = end
            continue
        i += 1
    return "".join(out)


def _line_of(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def scan_javascript(source: str, *, tracked_modules: frozenset[str]) -> list[SourceObservation]:
    code = _mask_comments(source)
    observations: list[SourceObservation] = []
    bound: dict[str, str] = {}

    for number, raw in enumerate(code.splitlines(), start=1):
        match = _JS_IMPORT_DEFAULT_RE.match(raw)
        if match is not None:
            default, named, namespace, module = match.groups()
            observations.append(SourceObservation(ObservationKind.IMPORT, number, module))
            root = _js_module_root(module)
            if root in tracked_modules:
                for name in [default, namespace, *_named_imports(named)]:
                    if name:
                        bound[name] = root
            continue
        bare = _JS_BARE_IMPORT_RE.match(raw)
        if bare is not None:
            observations.append(SourceObservation(ObservationKind.IMPORT, number, bare.group(1)))

    for match in _JS_REQUIRE_RE.finditer(code):
        name, named, module, immediately_called = match.groups()
        line = _line_of(code, match.start())
        observations.append(SourceObservation(ObservationKind.IMPORT, line, module))
        root = _js_module_root(module)
        if root in tracked_modules:
            for bound_name in [name, *_named_imports(named)]:
                if bound_name:
                    bound[bound_name] = root
            if immediately_called and name:
                # `const stripe = require('stripe')(key)` -- the variable is
                # already an instance.
                observations.append(SourceObservation(ObservationKind.CONSTRUCTOR, line, root, name))

    instances: dict[str, str] = {}
    if bound:
        names = "|".join(re.escape(n) for n in sorted(bound, key=len, reverse=True))
        ctor_re = re.compile(
            rf"(?:const|let|var|this\.)?\s*([A-Za-z_$][\w$.]*)\s*=\s*(?:await\s+)?(?:new\s+)?({names})(?:\.[\w$.]+)?\s*\("
        )
        for match in ctor_re.finditer(code):
            instances[match.group(1).removeprefix("this.")] = bound[match.group(2)]
            observations.append(
                SourceObservation(ObservationKind.CONSTRUCTOR, _line_of(code, match.start()), bound[match.group(2)],
                                  match.group(2))
            )
        receivers = {**{n: bound[n] for n in bound}, **instances}
        call_re = re.compile(
            rf"(?<![\w$.])(?:this\.)?({'|'.join(re.escape(n) for n in sorted(receivers, key=len, reverse=True))})"
            r"((?:\s*\.\s*[A-Za-z_$][\w$]*)+)\s*\("
        )
        for match in call_re.finditer(code):
            detail = re.sub(r"\s+", "", match.group(2)).lstrip(".")
            if _IDENT_CHAIN_RE.match(detail.replace("$", "_")):
                observations.append(
                    SourceObservation(ObservationKind.CALL, _line_of(code, match.start()), receivers[match.group(1)],
                                      detail)
                )

    for match in _JS_ENV_RE.finditer(code):
        observations.append(
            SourceObservation(ObservationKind.ENV_VAR, _line_of(code, match.start()), match.group(1) or match.group(2))
        )
    for match in _JS_STRING_RE.finditer(code):
        text = next(g for g in match.groups() if g is not None)
        if "${" in text:
            text = re.sub(r"\$\{[^}]*\}", "{}", text)
        observations.extend(_string_observations(text, _line_of(code, match.start())))

    return sorted(set(observations), key=lambda o: (o.line, o.kind.value, o.subject, o.detail))


def _named_imports(named: str | None) -> list[str]:
    if not named:
        return []
    result: list[str] = []
    for part in named.split(","):
        pieces = part.strip().split(" as ")
        name = pieces[-1].strip().removeprefix("type ").strip()
        if re.match(r"^[A-Za-z_$][\w$]*$", name):
            result.append(name)
    return result


def _js_module_root(module: str) -> str:
    if module.startswith("@"):
        return "/".join(module.split("/")[:2])
    return module.split("/")[0]


# -- enclosing symbols for JS (Python uses the repository parser) ------------------

_JS_FUNCTION_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\("
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)"
    r"|^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)"
    r"|^\s*(?:async\s+)?(?:static\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{"
)
_JS_KEYWORDS = frozenset({"if", "for", "while", "switch", "catch", "function", "return", "else"})


def javascript_symbol_spans(source: str) -> list[tuple[str, int, int]]:
    """``(qualified_name, start_line, end_line)`` for functions, arrow
    functions, classes and methods, by brace matching over
    comment-masked source. Heuristic but deterministic; strings
    containing braces can shift a span, never crash."""

    code = _mask_comments(source)
    lines = code.splitlines()
    spans: list[tuple[str, int, int]] = []
    stack: list[tuple[str, int, int]] = []  # (qualified name, start line, brace depth at open)
    depth = 0
    pending: tuple[str, int] | None = None
    for number, raw in enumerate(lines, start=1):
        match = _JS_FUNCTION_RE.match(raw)
        if match is not None:
            name = next(g for g in match.groups() if g)
            if name not in _JS_KEYWORDS:
                parent = next((q for q, _, _ in reversed(stack)), None)
                pending = (f"{parent}.{name}" if parent else name, number)
        for char in raw:
            if char == "{":
                if pending is not None:
                    stack.append((pending[0], pending[1], depth))
                    pending = None
                depth += 1
            elif char == "}":
                depth -= 1
                if stack and stack[-1][2] == depth:
                    qualified, start, _ = stack.pop()
                    spans.append((qualified, start, number))
        if pending is not None and "=>" in raw and "{" not in raw.split("=>", 1)[1]:
            spans.append((pending[0], pending[1], number))
            pending = None
    return spans


__all__ = [
    "ObservationKind",
    "SourceObservation",
    "javascript_symbol_spans",
    "sanitized_url_path",
    "scan_javascript",
    "scan_python",
    "url_host",
]
