from dataclasses import dataclass


@dataclass
class User:
    id: str
    name: str


class UserRepository:
    def __init__(self, users: dict[str, User]) -> None:
        self._users = users

    def get_by_id(self, user_id: str) -> User | None:
        """Returns the User, or None if no user with this id exists.

        Callers must check for None before use -- this method never
        raises for a missing id.
        """
        return self._users.get(user_id)
