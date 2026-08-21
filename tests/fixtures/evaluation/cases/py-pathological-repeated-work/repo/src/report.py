def _parse_rules(raw_rules: list[str]) -> list[tuple[str, str]]:
    parsed = []
    for raw in raw_rules:
        field, _, pattern = raw.partition(":")
        parsed.append((field, pattern))
    return sorted(parsed)


def apply_rules_to_rows(rows: list[dict], raw_rules: list[str]) -> list[dict]:
    matched = []
    for row in rows:
        rules = _parse_rules(raw_rules)
        if all(row.get(field) == pattern for field, pattern in rules):
            matched.append(row)
    return matched
