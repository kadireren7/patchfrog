class UserService:
    def __init__(self, external_client) -> None:
        self.external_client = external_client

    def get_display_name(self, user_id: str) -> str:
        # external_client is a third-party SDK object that can return
        # None even for a "successful" call (documented quirk of the
        # client) -- this check is required, not redundant.
        user = self.external_client.fetch_user(user_id)
        if user is None:
            return "Unknown User"
        return user.name

    def is_active(self, user_id: str) -> bool:
        user = self.external_client.fetch_user(user_id)
        return user is not None and user.active
