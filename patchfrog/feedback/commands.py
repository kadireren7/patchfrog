"""Strict, deterministic parser for explicit ``/patchfrog <command>`` reply
commands.

Security-critical (Phase 9 spec sections 30/31): a developer reply is
untrusted text. This parser is the *only* place reply bodies are ever
interpreted as an action, and it recognizes exactly four fixed tokens,
with no arguments, no shell/subprocess involvement, and no LLM in the
loop. Everything else -- a sentence merely mentioning a command, a
command embedded in a code block or blockquote, a command followed by
extra characters -- is treated as inert text, identical to any other
reply body. The caller (:mod:`patchfrog.feedback.sync`) is responsible
for also rejecting bot actors before ever reaching this parser (see
:class:`patchfrog.domain.github_feedback.GitHubActorType`) -- this module
has no actor awareness by design, so it can never be the place a bot
self-command silently slips through.
"""

from __future__ import annotations

from patchfrog.feedback.domain import ExplicitCommand

_PREFIX = "/patchfrog "
_COMMAND_TOKENS: dict[str, ExplicitCommand] = {c.value: c for c in ExplicitCommand}


def parse_explicit_command(body: str) -> ExplicitCommand | None:
    """Returns the recognized command if, and only if, ``body`` -- once
    stripped of surrounding whitespace -- is *exactly* ``/patchfrog
    <token>`` for one of the four allowed tokens. Anything else (extra
    text, arguments, shell-style operators, a command quoted inside a
    code fence or blockquote, multi-line replies) returns ``None``.

    This is deliberately whole-body matching, not a substring search:
    a reply is either a clean, standalone command or it is just a reply.
    A command sitting alongside other prose is ambiguous about whether
    the human meant it as a directive or as an example, so it is never
    parsed as one (Phase 9 spec section 31 -- markdown code examples
    containing command text, quoted command text, and
    ``/patchfrog useful && rm -rf /``-style injection attempts must all
    be ignored).
    """

    trimmed = body.strip()
    if not trimmed.startswith(_PREFIX):
        return None

    token = trimmed[len(_PREFIX) :]
    return _COMMAND_TOKENS.get(token)
