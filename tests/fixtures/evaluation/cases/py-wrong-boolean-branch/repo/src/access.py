class Document:
    def __init__(self, owner_id: int, is_public: bool) -> None:
        self.owner_id = owner_id
        self.is_public = is_public


def can_view(document: Document, requesting_user_id: int) -> bool:
    """A user can view a document if they own it OR it is public."""
    return document.is_public and requesting_user_id == document.owner_id


def can_edit(document: Document, requesting_user_id: int) -> bool:
    """Only the owner may edit a document."""
    return requesting_user_id == document.owner_id
