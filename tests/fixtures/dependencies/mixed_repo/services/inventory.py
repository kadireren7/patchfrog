"""Client for the internal inventory service (see openapi.yaml)."""

import requests

BASE_URL = "https://inventory.internal.example.com"
ITEM_PATH = "/v1/items/{item_id}"


def get_item(item_id: str) -> dict:
    return requests.get(BASE_URL + ITEM_PATH.format(item_id=item_id)).json()


def reserve(item_id: str, quantity: int) -> dict:
    return requests.post(
        "https://inventory.internal.example.com/v1/reservations", json={"item": item_id, "qty": quantity}
    ).json()
