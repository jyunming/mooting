from bisect import bisect_left

#: Volume discounts. Each tier applies from its threshold upward.
THRESHOLDS = [0, 100, 500, 1000]
DISCOUNT_PERCENT = [0, 5, 8, 12]


def discount_for(units: int) -> int:
    """The discount percentage for an order of `units`."""
    if units < 0:
        raise ValueError("units cannot be negative")
    i = bisect_left(THRESHOLDS, units) - 1
    return DISCOUNT_PERCENT[max(i, 0)]


def price(units: int, unit_cents: int) -> int:
    """Order total in cents after the volume discount, rounded down."""
    gross = units * unit_cents
    return gross * (100 - discount_for(units)) // 100


def next_tier(units: int) -> int | None:
    """How many more units reach the next discount, or None at the top tier."""
    for t in THRESHOLDS:
        if t > units:
            return t - units
    return None
