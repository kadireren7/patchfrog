import acme_http


def fetch(url: str) -> bytes:
    return acme_http.get(url).content
