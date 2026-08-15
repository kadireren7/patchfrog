"""Low-level, safe Git command execution.

Every git invocation goes through :func:`run_git` so that: hooks are
disabled, no interactive credential prompt can hang the process, secrets
never leak into logs or exceptions, and commands time out rather than
hang forever on a misbehaving remote.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_TOKEN_IN_URL_RE = re.compile(r"https://[^@\s/]+@")


def scrub_secrets(text: str) -> str:
    """Redact any embedded HTTP credential (``https://user:token@...``)."""

    return _TOKEN_IN_URL_RE.sub("https://***@", text)


class GitError(RuntimeError):
    """A git subprocess exited non-zero, or otherwise failed."""


def run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: float = 120.0,
) -> str:
    """Run ``git <args>`` and return stdout, disabling hooks and prompts.

    Raises :class:`GitError` (with any embedded credentials scrubbed) on
    a non-zero exit, timeout, or missing ``git`` binary.
    """

    command = ["git", "-c", "core.hooksPath=/dev/null", *args]
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env={"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"},
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git command timed out after {timeout_seconds}s: {scrub_secrets(' '.join(args))}") from exc
    except OSError as exc:
        raise GitError(f"failed to execute git: {exc}") from exc

    if result.returncode != 0:
        raise GitError(
            f"git command failed (exit {result.returncode}): "
            f"{scrub_secrets(' '.join(args))}\n{scrub_secrets(result.stderr.strip())}"
        )

    return result.stdout
