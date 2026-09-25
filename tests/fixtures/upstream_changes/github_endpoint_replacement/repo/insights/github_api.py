import requests


def repo_stats(owner: str, repo: str) -> dict:
    url = f"https://api.github.com/repos/{owner}/{repo}/legacy-stats"
    return requests.get(url, timeout=10).json()


def open_issues(owner: str, repo: str) -> list:
    return requests.get(f"https://api.github.com/repos/{owner}/{repo}/issues", timeout=10).json()
