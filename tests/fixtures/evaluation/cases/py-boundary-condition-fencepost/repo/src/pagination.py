def get_page(items: list[str], page: int, page_size: int) -> list[str]:
    """Return the items for a zero-indexed page of page_size items, with
    no item appearing on more than one page."""
    start = page * page_size
    end = start + page_size
    return items[start : end + 1]


def total_pages(items: list[str], page_size: int) -> int:
    return (len(items) + page_size - 1) // page_size
