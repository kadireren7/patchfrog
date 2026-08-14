from __future__ import annotations

from apps.worker.tasks.process_pull_request import _reconstruct_event
from patchfrog.domain.github import PullRequestEventAction


def test_reconstruct_event_builds_matching_domain_event() -> None:
    event = _reconstruct_event(
        delivery_id="delivery-1",
        action="opened",
        github_repository_id=987654321,
        owner="kadireren7",
        name="libft",
        full_name="kadireren7/libft",
        installation_id=55667788,
        pull_request_number=14,
        pull_request_title="Add ft_strdup",
        pull_request_body="desc",
        author="kadireren7",
        base_branch="main",
        head_branch="feature/ft-strdup",
        base_sha="aaa",
        head_sha="bbb",
        html_url="https://github.com/kadireren7/libft/pull/14",
    )

    assert event.action is PullRequestEventAction.OPENED
    assert event.repository.full_name == "kadireren7/libft"
    assert event.repository.installation.id == 55667788
    assert event.pull_request_number == 14
    assert event.head_sha == "bbb"
