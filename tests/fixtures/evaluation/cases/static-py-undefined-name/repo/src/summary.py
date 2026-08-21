def build_summary(items: list[str]) -> str:
    # Typo: `total_items` was never defined anywhere in this function --
    # a real NameError at runtime, and exactly the class of bug pyflakes'
    # F821 (undefined name) exists to catch.
    return f"{total_items} item(s): {', '.join(items)}"


def build_empty_summary() -> str:
    return "no items"
