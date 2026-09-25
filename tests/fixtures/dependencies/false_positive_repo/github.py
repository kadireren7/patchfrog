"""The project's OWN module that happens to be called github -- not PyGithub."""


def repo_url(owner: str, name: str) -> str:
    return f"https://github.com/{owner}/{name}"
