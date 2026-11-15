def page(items: list, number: int, size: int = 20) -> list:
    """Return page `number` of `items`, counting pages from 1.

    page(rows, 1) is the first `size` rows; a page past the end is empty.
    """
    if number < 1:
        raise ValueError("pages are counted from 1")
    if size < 1:
        raise ValueError("a page holds at least one item")
    start = number * size
    return items[start:start + size]


def page_count(n_items: int, size: int = 20) -> int:
    """How many pages `n_items` fill. An empty list still has one, empty, page."""
    if size < 1:
        raise ValueError("a page holds at least one item")
    return max(1, -(-n_items // size))
