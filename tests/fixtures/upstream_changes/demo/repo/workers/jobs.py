from workers.summary import summarize


def nightly_digest(texts: list[str]) -> list[str]:
    return [summarize(t) for t in texts]
