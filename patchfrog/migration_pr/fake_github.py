"""Deterministic, in-memory :class:`~patchfrog.migration_pr.github_publisher.MigrationGitHubPublisher`
for tests (M9.9).

Mirrors :class:`patchfrog.publishing.fake_publisher.FakeReviewPublisher`'s
role exactly -- structural typing is what makes this a legitimate stand-in
rather than a mock of internal plumbing. Simulates a minimal in-memory
"GitHub": refs, commits (content only, no real git objects), and pull
requests, plus check runs. No real GitHub PR is ever opened by using
this."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from patchfrog.domain.github_check import GitHubCheckRun, GitHubCheckRunInput
from patchfrog.domain.github_git import GitTreeEntry
from patchfrog.domain.pull_request import PullRequestMetadata


def _fake_sha(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:40]


@dataclass
class _FakePullRequest:
    number: int
    title: str
    body: str
    head_ref: str
    base_ref: str
    head_sha: str
    state: str = "open"

    def to_metadata(self, *, owner: str, repository: str) -> PullRequestMetadata:
        return PullRequestMetadata(
            number=self.number, title=self.title, body=self.body, author="patchfrog[bot]",
            base_branch=self.base_ref, head_branch=self.head_ref, base_sha=self.head_sha, head_sha=self.head_sha,
            html_url=f"https://github.com/{owner}/{repository}/pull/{self.number}", state=self.state,
        )


class FakeMigrationGitHubPublisher:
    """Not a subclass of anything -- satisfies
    :class:`patchfrog.migration_pr.github_publisher.MigrationGitHubPublisher`
    structurally."""

    def __init__(self, *, refs: dict[str, str] | None = None) -> None:
        #: "owner/repository:heads/<branch>" -> sha.
        self._refs: dict[str, str] = dict(refs or {})
        #: sha -> tree sha (fake: just a digest over the entries).
        self._commit_trees: dict[str, str] = {}
        self._pull_requests: list[_FakePullRequest] = []
        self._next_pr_number = 100
        self.checks: list[GitHubCheckRun] = []
        self.create_ref_calls: list[str] = []
        self.update_ref_calls: list[str] = []
        self.create_pull_request_calls: list[str] = []
        self.update_pull_request_calls: list[int] = []

    def set_ref(self, *, owner: str, repository: str, ref: str, sha: str) -> None:
        self._refs[f"{owner}/{repository}:{ref}"] = sha

    async def get_ref(self, *, owner: str, repository: str, ref: str) -> str | None:
        return self._refs.get(f"{owner}/{repository}:{ref}")

    async def create_ref(self, *, owner: str, repository: str, ref: str, sha: str) -> None:
        key = f"{owner}/{repository}:{ref.removeprefix('refs/')}"
        if key in self._refs:
            from patchfrog.github.errors import GitHubUnprocessableError

            raise GitHubUnprocessableError(f"ref already exists: {ref}")
        self._refs[key] = sha
        self.create_ref_calls.append(ref)

    async def update_ref(self, *, owner: str, repository: str, ref: str, sha: str, force: bool = False) -> None:
        self._refs[f"{owner}/{repository}:{ref}"] = sha
        self.update_ref_calls.append(ref)

    async def get_commit_tree_sha(self, *, owner: str, repository: str, commit_sha: str) -> str:
        return self._commit_trees.get(commit_sha, _fake_sha("tree", commit_sha))

    async def create_tree(
        self, *, owner: str, repository: str, base_tree_sha: str, entries: list[GitTreeEntry]
    ) -> str:
        payload = json.dumps(
            [{"path": e.path, "content": e.content} for e in entries], sort_keys=True,
        )
        return _fake_sha("tree", base_tree_sha, payload)

    async def create_commit(
        self, *, owner: str, repository: str, message: str, tree_sha: str, parent_sha: str
    ) -> str:
        commit_sha = _fake_sha("commit", message, tree_sha, parent_sha)
        self._commit_trees[commit_sha] = tree_sha
        return commit_sha

    async def create_pull_request(
        self, *, owner: str, repository: str, title: str, body: str, head: str, base: str
    ) -> PullRequestMetadata:
        self.create_pull_request_calls.append(head)
        head_sha = self._refs.get(f"{owner}/{repository}:heads/{head}", "")
        pr = _FakePullRequest(
            number=self._next_pr_number, title=title, body=body, head_ref=head, base_ref=base, head_sha=head_sha,
        )
        self._next_pr_number += 1
        self._pull_requests.append(pr)
        return pr.to_metadata(owner=owner, repository=repository)

    async def list_pull_requests(
        self, *, owner: str, repository: str, head: str | None = None, state: str = "all"
    ) -> list[PullRequestMetadata]:
        results = []
        for pr in self._pull_requests:
            if head is not None and f"{owner}:{pr.head_ref}" != head:
                continue
            if state != "all" and pr.state != state:
                continue
            results.append(pr.to_metadata(owner=owner, repository=repository))
        return results

    async def update_pull_request(
        self, *, owner: str, repository: str, number: int, title: str | None = None, body: str | None = None
    ) -> PullRequestMetadata:
        self.update_pull_request_calls.append(number)
        pr = next(p for p in self._pull_requests if p.number == number)
        if title is not None:
            pr.title = title
        if body is not None:
            pr.body = body
        pr.head_sha = self._refs.get(f"{owner}/{repository}:heads/{pr.head_ref}", pr.head_sha)
        return pr.to_metadata(owner=owner, repository=repository)

    def close_pull_request(self, number: int) -> None:
        next(p for p in self._pull_requests if p.number == number).state = "closed"

    async def reconcile_check(
        self, *, owner: str, repository: str, head_sha: str, check: GitHubCheckRunInput
    ) -> GitHubCheckRun:
        match = next(
            (c for c in self.checks if c.external_id == check.external_id and c.head_sha == head_sha), None
        )
        if match is None:
            run = GitHubCheckRun(
                id=len(self.checks) + 1, name=check.name, head_sha=head_sha, external_id=check.external_id,
                status=check.status, conclusion=check.conclusion,
            )
            self.checks.append(run)
            return run
        self.checks.remove(match)
        updated = GitHubCheckRun(
            id=match.id, name=check.name, head_sha=head_sha, external_id=check.external_id, status=check.status,
            conclusion=check.conclusion,
        )
        self.checks.append(updated)
        return updated


__all__ = ["FakeMigrationGitHubPublisher"]
