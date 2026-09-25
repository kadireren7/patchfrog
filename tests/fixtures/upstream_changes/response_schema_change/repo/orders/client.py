import httpx

BASE = "https://orders.internal.example"


def order_total(order_id: str) -> int:
    data = httpx.get(BASE + f"/v1/orders/{order_id}").json()
    return data["total"]


def order_ids() -> list[str]:
    return [o["id"] for o in httpx.get(BASE + "/v1/orders").json()]
