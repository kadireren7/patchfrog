from app.ai.chat import generate_reply


def reply_route(payload: dict) -> dict:
    return {"reply": generate_reply(payload["message"])}


def bulk_reply(payloads: list[dict]) -> list[dict]:
    return [reply_route(p) for p in payloads]
