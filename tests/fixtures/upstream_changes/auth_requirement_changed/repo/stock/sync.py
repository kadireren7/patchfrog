import os

import httpx

HOST = "https://inventory.internal.example"
HEADERS = {"X-Api-Key": os.environ.get("INVENTORY_API_KEY", "")}


def fetch_item(sku: str) -> dict:
    return httpx.get(HOST + f"/v1/items/{sku}", headers=HEADERS).json()


def push_stock(sku: str, count: int) -> None:
    httpx.put(HOST + f"/v1/items/{sku}/stock", json={"count": count}, headers=HEADERS)
