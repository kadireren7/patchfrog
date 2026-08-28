"""Unit coverage for :mod:`patchfrog.review.config_resolution` -- the
single code path both the CLI and the production Celery task use to load
a repository's ``.patchfrog.yml`` at an exact commit. No real network
access: the "remote" branch is exercised against a real *local* git
repository used as ``clone_url`` (git natively supports a local path as a
remote), so this stays fast and hermetic while still exercising the real
git-fetch-and-checkout code path, not a mock of it.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from patchfrog.review.config import MalformedReviewConfigError, ReviewConfig
from patchfrog.review.config_resolution import resolve_repository_review_config
from tests.support.git_repo import commit_all, materialize_fixture_repo


async def test_local_and_remote_resolve_identical_config_for_same_commit(tmp_path: Path) -> None:
    """The core parity guarantee: given the exact same commit's content,
    local (already-checked-out) and remote (freshly cloned) resolution
    must produce byte-for-byte identical ReviewConfig objects."""

    snapshot = materialize_fixture_repo(tmp_path / "repo", "ai_review_python", full_name=f"test/{uuid.uuid4().hex[:8]}")
    (snapshot.root_path / ".patchfrog.yml").write_text(
        "review:\n  max_candidates: 7\n  min_final_confidence: high\n"
    )
    commit_sha = commit_all(snapshot.root_path, "add review config")

    local_config = await resolve_repository_review_config(
        local=True, commit_sha=commit_sha, repository_full_name="test/parity", root_path=snapshot.root_path
    )
    remote_config = await resolve_repository_review_config(
        local=False,
        commit_sha=commit_sha,
        repository_full_name="test/parity",
        clone_url=str(snapshot.root_path),
    )

    assert local_config == remote_config
    assert local_config.fingerprint() == remote_config.fingerprint()
    assert local_config.max_candidates == 7
    assert local_config.min_final_confidence.value == "high"


async def test_local_and_remote_both_default_when_config_missing(tmp_path: Path) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "ai_review_python", full_name=f"test/{uuid.uuid4().hex[:8]}")

    local_config = await resolve_repository_review_config(
        local=True, commit_sha=snapshot.commit_sha, repository_full_name="test/parity", root_path=snapshot.root_path
    )
    remote_config = await resolve_repository_review_config(
        local=False,
        commit_sha=snapshot.commit_sha,
        repository_full_name="test/parity",
        clone_url=str(snapshot.root_path),
    )

    assert local_config == remote_config == ReviewConfig()


async def test_local_and_remote_both_raise_on_malformed_config(tmp_path: Path) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "ai_review_python", full_name=f"test/{uuid.uuid4().hex[:8]}")
    (snapshot.root_path / ".patchfrog.yml").write_text("review: [this is not a mapping\n")
    commit_sha = commit_all(snapshot.root_path, "add malformed config")

    with pytest.raises(MalformedReviewConfigError):
        await resolve_repository_review_config(
            local=True, commit_sha=commit_sha, repository_full_name="test/parity", root_path=snapshot.root_path
        )
    with pytest.raises(MalformedReviewConfigError):
        await resolve_repository_review_config(
            local=False,
            commit_sha=commit_sha,
            repository_full_name="test/parity",
            clone_url=str(snapshot.root_path),
        )


async def test_remote_resolution_is_pinned_to_the_requested_commit_not_head(tmp_path: Path) -> None:
    """A wrong/stale checkout must never be able to supply another
    commit's config -- resolving at an *earlier* commit's SHA must return
    that commit's config even though the repository's current HEAD (a
    later commit) has different, changed content."""

    snapshot = materialize_fixture_repo(tmp_path / "repo", "ai_review_python", full_name=f"test/{uuid.uuid4().hex[:8]}")
    (snapshot.root_path / ".patchfrog.yml").write_text("review:\n  max_candidates: 5\n")
    first_commit_sha = commit_all(snapshot.root_path, "config v1: max_candidates=5")

    (snapshot.root_path / ".patchfrog.yml").write_text("review:\n  max_candidates: 99\n")
    second_commit_sha = commit_all(snapshot.root_path, "config v2: max_candidates=99")
    assert first_commit_sha != second_commit_sha

    config_at_first = await resolve_repository_review_config(
        local=False,
        commit_sha=first_commit_sha,
        repository_full_name="test/pinning",
        clone_url=str(snapshot.root_path),
    )
    config_at_second = await resolve_repository_review_config(
        local=False,
        commit_sha=second_commit_sha,
        repository_full_name="test/pinning",
        clone_url=str(snapshot.root_path),
    )

    assert config_at_first.max_candidates == 5
    assert config_at_second.max_candidates == 99


async def test_credential_shaped_fields_are_ignored_in_both_modes(tmp_path: Path) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "ai_review_python", full_name=f"test/{uuid.uuid4().hex[:8]}")
    (snapshot.root_path / ".patchfrog.yml").write_text(
        "review:\n  api_key: sk-ant-should-be-ignored\n  max_candidates: 3\n"
    )
    commit_sha = commit_all(snapshot.root_path, "config with credential field")

    local_config = await resolve_repository_review_config(
        local=True, commit_sha=commit_sha, repository_full_name="test/creds", root_path=snapshot.root_path
    )
    remote_config = await resolve_repository_review_config(
        local=False, commit_sha=commit_sha, repository_full_name="test/creds", clone_url=str(snapshot.root_path)
    )

    assert not hasattr(local_config, "api_key")
    assert not hasattr(remote_config, "api_key")
    assert local_config.max_candidates == remote_config.max_candidates == 3


async def test_operator_only_field_in_committed_config_raises_for_both_local_and_remote(
    tmp_path: Path,
) -> None:
    """Milestone C trust boundary: a committed ``.patchfrog.yml`` trying
    to set ``provider``/``model``/``critic_model``/``request_timeout_seconds``
    must fail loudly for a real review attempt -- both the CLI's local
    path and the production Celery task's remote path use
    ``on_malformed="raise"`` (see :func:`resolve_repository_review_config`'s
    docstring), so neither can silently let a repository influence
    provider/model selection."""

    snapshot = materialize_fixture_repo(tmp_path / "repo", "ai_review_python", full_name=f"test/{uuid.uuid4().hex[:8]}")
    (snapshot.root_path / ".patchfrog.yml").write_text("review:\n  provider: gemini\n  model: gemini-3.6-flash\n")
    commit_sha = commit_all(snapshot.root_path, "attempt to set operator-only fields")

    with pytest.raises(MalformedReviewConfigError, match="no longer repository-controlled"):
        await resolve_repository_review_config(
            local=True, commit_sha=commit_sha, repository_full_name="test/boundary", root_path=snapshot.root_path
        )
    with pytest.raises(MalformedReviewConfigError, match="no longer repository-controlled"):
        await resolve_repository_review_config(
            local=False,
            commit_sha=commit_sha,
            repository_full_name="test/boundary",
            clone_url=str(snapshot.root_path),
        )


async def test_local_requires_root_path() -> None:
    with pytest.raises(ValueError, match="root_path"):
        await resolve_repository_review_config(local=True, commit_sha="a" * 40, repository_full_name="test/x")


async def test_remote_requires_clone_url() -> None:
    with pytest.raises(ValueError, match="clone_url"):
        await resolve_repository_review_config(local=False, commit_sha="a" * 40, repository_full_name="test/x")
