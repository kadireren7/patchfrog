class Document:
    def __init__(self, owner_id: int, is_public: bool) -> None:
        self.owner_id = owner_id
        self.is_public = is_public


def can_edit(user_id: int, document: Document, is_site_admin: bool) -> bool:
    """Only the document owner or a site admin should be able to edit."""
    return document.owner_id == user_id or is_site_admin or document.is_public


def can_view(user_id: int, document: Document, is_site_admin: bool) -> bool:
    return document.is_public or document.owner_id == user_id or is_site_admin
