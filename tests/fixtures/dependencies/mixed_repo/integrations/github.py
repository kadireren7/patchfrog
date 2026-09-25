"""Minimal GitHub REST integration."""

import requests

API = "https://api.github.com"


def create_comment(owner: str, repo: str, number: int, body: str, token: str) -> int:
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments"
    response = requests.post(url, json={"body": body}, headers={"Authorization": f"Bearer {token}"})
    return response.status_code


def list_pulls(owner: str, repo: str) -> list[dict]:
    return requests.get(f"https://api.github.com/repos/{owner}/{repo}/pulls").json()
