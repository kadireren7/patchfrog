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


def git_is_ancestor(*, ancestor_sha: str, descendant_sha: str, cwd: Path) -> bool:
    """``git merge-base --is-ancestor <ancestor> <descendant>``.

    Unlike every other git subcommand here, exit code alone *is* the
    answer (0 = true, 1 = false) rather than a failure signal -- so this
    is the one place that inspects a raw ``subprocess.run`` exit code
    instead of going through :func:`run_git`. Any exit code other than
    0/1 (missing object, not a git repository, ...) is a genuine error
    and raises :class:`GitError` -- callers must never interpret "I
    couldn't tell" as "false" themselves (see
    :mod:`patchfrog.repository.ancestry`, which treats *any* failure to
    prove ancestry, true or unknown, identically: no incremental reuse).
    """

    command = [
        "git", "-c", "core.hooksPath=/dev/null",
        "merge-base", "--is-ancestor", ancestor_sha, descendant_sha,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30.0,
            env={"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"},
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError("git merge-base --is-ancestor timed out") from exc
    except OSError as exc:
        raise GitError(f"failed to execute git: {exc}") from exc

    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise GitError(
        f"git merge-base --is-ancestor failed (exit {result.returncode}): "
        f"{scrub_secrets(result.stderr.strip())}"
    )
