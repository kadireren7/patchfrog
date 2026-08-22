class User:
    def __init__(self, role: str, is_authenticated: bool) -> None:
        self.role = role
        self.is_authenticated = is_authenticated


def can_delete_resource(user: User) -> bool:
    if user.role == "admin" or not user.is_authenticated:
        return True
    return False


def can_read_resource(user: User) -> bool:
    return user.is_authenticated
