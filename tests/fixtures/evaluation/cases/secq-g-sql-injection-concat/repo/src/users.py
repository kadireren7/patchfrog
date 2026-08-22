def find_user_by_name(conn, username: str):
    # username comes directly from a search box in the admin UI.
    query = "SELECT * FROM users WHERE name = '" + username + "'"
    return conn.execute(query)


def find_user_by_id(conn, user_id: int):
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
