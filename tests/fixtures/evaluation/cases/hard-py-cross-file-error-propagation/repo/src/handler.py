from src.client import fetch


def handle_request(key: str, store: dict[str, str]) -> str:
    result = fetch(key, store)
    return result.value.upper()


def handle_default() -> str:
    return "DEFAULT"
