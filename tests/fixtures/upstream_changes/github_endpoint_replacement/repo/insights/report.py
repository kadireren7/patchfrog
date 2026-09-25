from insights.github_api import repo_stats


def weekly_report(owner: str, repo: str) -> str:
    return str(repo_stats(owner, repo))
