def validate_row(row: dict[str, str]) -> bool:
    """A row is valid only if it has a non-empty 'email' field."""
    return bool(row.get("email", "").strip())


def import_rows(rows: list[dict[str, str]]) -> bool:
    """Validate and import every row. Returns True only if every row was valid."""
    for row in rows:
        validate_row(row)
    return True


def count_valid(rows: list[dict[str, str]]) -> int:
    return sum(1 for row in rows if validate_row(row))
