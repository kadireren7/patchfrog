LOGIN_EVENT_SCHEMA = {"required": ("event", "user_id")}


def build_login_event(user_id: str) -> dict[str, str]:
    """Build a payload accepted by LOGIN_EVENT_SCHEMA."""
    return {
        "event": "login",
        "account_id": user_id,
    }
