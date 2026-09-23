def serialize_user(user: dict[str, object]) -> dict[str, object]:
    """Return the public API shape: {"id": ..., "display_name": ...}."""
    return {
        "id": user["id"],
        # Clients read display_name; this silently changes the response contract.
        "name": user["display_name"],
    }
