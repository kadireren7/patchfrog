"""Unit coverage for :mod:`patchfrog.feedback.commands` -- the strict
explicit-command parser. Security-critical: every case here mirrors an
adversarial scenario from the Phase 9 spec (sections 30/31)."""

from __future__ import annotations

import pytest

from patchfrog.feedback.commands import parse_explicit_command
from patchfrog.feedback.domain import ExplicitCommand


@pytest.mark.parametrize(
    "body,expected",
    [
        ("/patchfrog useful", ExplicitCommand.USEFUL),
        ("/patchfrog false-positive", ExplicitCommand.FALSE_POSITIVE),
        ("/patchfrog fixed", ExplicitCommand.FIXED),
        ("/patchfrog ignore", ExplicitCommand.IGNORE),
        ("  /patchfrog useful  ", ExplicitCommand.USEFUL),
        ("\n/patchfrog fixed\n", ExplicitCommand.FIXED),
    ],
)
def test_exact_allowed_commands_parse(body: str, expected: ExplicitCommand) -> None:
    assert parse_explicit_command(body) == expected


@pytest.mark.parametrize(
    "body",
    [
        "",
        "just a normal reply",
        "/patchfrog",
        "/patchfrog useful extra",
        "/patchfrog useful && rm -rf /",
        "/patchfrog usefull",  # typo
        "/patchfrog USEFUL",  # case mismatch -- exact vocabulary only
        "this contains /patchfrog useful inline",
        "> /patchfrog useful",  # quoted in a blockquote
        "```\n/patchfrog useful\n```",  # inside a code fence
        "/patchfrog useful\nand also this",
        "/patchfrog unsupported-command",
        "/patchfrog useful/",
        "/patchfrog  useful",  # double space is not the exact token
    ],
)
def test_everything_else_is_never_a_command(body: str) -> None:
    assert parse_explicit_command(body) is None


def test_no_arguments_are_ever_accepted() -> None:
    assert parse_explicit_command("/patchfrog false-positive because I think so") is None
