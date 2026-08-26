"""Security-boundary tests for public beta readiness (spec section 45).

Signature forgery / missing signature / malformed payload are already
covered by ``tests/integration/test_webhook_route.py``. This file covers
the remaining items from the security test list that don't already have
dedicated coverage elsewhere:

- oversized webhook payload (413, rejected before signature verification
  even parses the body)
- symlink escape out of a repository snapshot root
- a huge (but syntactically valid) ``.patchfrog.yml`` doesn't crash the
  parser (known, documented limitation: no explicit byte-size cap before
  ``yaml.safe_load`` -- see the module docstring for why this wasn't
  changed)
- an exception carrying a secret-shaped value never survives structured
  logging unredacted (end-to-end through the real configured logger
  pipeline, not just the processor function in isolation -- see
  ``tests/unit/test_logging_redaction.py`` for the unit-level coverage)

Prompt-injection resilience is covered by the Phase "Security Review
Quality" evaluation corpus (``secq-o-prompt-injection-claims-safe``),
re-run as part of the final validation gate rather than duplicated here.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from pathlib import Path

import httpx
import pytest
import structlog
import yaml
from httpx import ASGITransport

from apps.api.main import app
from patchfrog.config.logging import _REDACTED, configure_logging
from patchfrog.repository.snapshot import RepositorySnapshot
from patchfrog.review.config import load_review_config

WEBHOOK_SECRET = os.environ["GITHUB_WEBHOOK_SECRET"]


def _signature(body: bytes) -> str:
    digest = hmac.new(WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def test_oversized_webhook_payload_is_rejected_before_parsing() -> None:
    oversized_body = b'{"padding": "' + b"a" * (5 * 1024 * 1024 + 1) + b'"}'

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/webhooks/github",
            content=oversized_body,
            headers={
                "X-Hub-Signature-256": _signature(oversized_body),
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "delivery-oversized",
                "Content-Type": "application/json",
                "Content-Length": str(len(oversized_body)),
            },
        )

    assert response.status_code == 413


async def test_oversized_payload_rejected_by_content_length_header_alone() -> None:
    """The Content-Length pre-check must reject before the body is even
    read off the wire, not just after -- a forged Content-Length lying
    small while the real body is huge is still caught by the post-read
    check above; this test targets the other direction (an honest,
    large Content-Length)."""

    small_body = b'{"action": "opened"}'

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/webhooks/github",
            content=small_body,
            headers={
                "X-Hub-Signature-256": _signature(small_body),
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "delivery-fake-length",
                "Content-Type": "application/json",
                "Content-Length": str(6 * 1024 * 1024),
            },
        )

    assert response.status_code == 413


def test_symlink_pointing_outside_snapshot_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret_file = outside / "secret.txt"
    secret_file.write_text("should never be reachable")

    escape_symlink = root / "innocuous_looking_file.py"
    escape_symlink.symlink_to(secret_file)

    snapshot = RepositorySnapshot(
        repository_full_name="test/escape", commit_sha="0" * 40, root_path=root, owns_root=False
    )

    with pytest.raises(ValueError, match="escapes repository root"):
        snapshot.resolve_path("innocuous_looking_file.py")


def test_traversal_via_symlinked_directory_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "passwd").write_text("root:x:0:0")

    (root / "linked_dir").symlink_to(outside)

    snapshot = RepositorySnapshot(
        repository_full_name="test/escape", commit_sha="0" * 40, root_path=root, owns_root=False
    )

    with pytest.raises(ValueError, match="escapes repository root"):
        snapshot.resolve_path("linked_dir/passwd")


def test_huge_but_valid_patchfrog_yml_does_not_crash_the_parser(tmp_path: Path) -> None:
    """Documents current behavior rather than asserting a specific limit:
    there is no explicit byte-size cap before `yaml.safe_load` in any of
    the four `.patchfrog.yml` loaders (review/analysis/publishing/
    review_memory config) -- a large committed config is parsed in full,
    bounded only by available worker memory. This was deliberately not
    changed for public-beta-readiness (touching four near-duplicate
    parsing call sites is architecture-adjacent surface, not a small
    fix) -- tracked as a known limitation in docs/operations.md rather
    than silently left unverified."""

    huge_comment_padding = "# padding\n" * 200_000  # ~2MB of harmless comment lines
    content = huge_comment_padding + "review:\n  max_candidates: 7\n"
    (tmp_path / ".patchfrog.yml").write_text(content)

    started = time.monotonic()
    config = load_review_config(tmp_path, on_malformed="raise")
    elapsed = time.monotonic() - started

    assert config.max_candidates == 7
    assert elapsed < 10, "a 2MB config took unexpectedly long to parse -- investigate before raising the cap"


def test_yaml_alias_reuse_does_not_multiply_memory(tmp_path: Path) -> None:
    """PyYAML's `safe_load` resolves anchors/aliases as *shared object
    references*, not deep copies -- so a `.patchfrog.yml` built entirely
    of nested aliases (the classic "billion laughs" shape) does not
    actually expand into an exponential in-memory structure the way an
    XML entity bomb would. This test pins that behavior so a future
    PyYAML version change that silently starts deep-copying aliases
    would be caught here, not discovered in production."""

    bomb = 'a0: &a0 ["x"]\n'
    for i in range(1, 12):
        bomb += f"a{i}: &a{i} [*a{i - 1}, *a{i - 1}, *a{i - 1}, *a{i - 1}, *a{i - 1}]\n"

    started = time.monotonic()
    result = yaml.safe_load(bomb)
    elapsed = time.monotonic() - started

    assert elapsed < 2
    assert len(result["a11"]) == 5


async def test_secret_shaped_exception_never_reaches_stdout_unredacted(capsys: pytest.CaptureFixture[str]) -> None:
    """End-to-end through the real configured structlog pipeline (not
    just the `redact_secrets` processor called in isolation, as in
    tests/unit/test_logging_redaction.py)."""

    configure_logging("INFO")
    logger = structlog.get_logger("test_security_boundaries")

    secret_value = "ghp_" + "s" * 36
    try:
        raise RuntimeError(f"GitHub call failed with token={secret_value}")
    except RuntimeError as exc:
        logger.error("simulated_failure", detail=str(exc))

    captured = capsys.readouterr()
    assert secret_value not in captured.out
    assert _REDACTED in captured.out
