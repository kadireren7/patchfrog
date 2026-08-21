class UserRepository:
    def __init__(self, db) -> None:
        self._db = db

    def get_by_id(self, user_id: int) -> dict:
        return self._db.query_one("SELECT * FROM users WHERE id = ?", user_id)

    def get_by_ids(self, user_ids: list[int]) -> list[dict]:
        return self._db.query_many("SELECT * FROM users WHERE id IN (?)", user_ids)


def load_comment_authors(repo: UserRepository, comment_author_ids: list[int]) -> list[dict]:
    authors = []
    for author_id in comment_author_ids:
        authors.append(repo.get_by_id(author_id))
    return authors
