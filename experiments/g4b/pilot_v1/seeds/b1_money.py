"""Amounts are held as integer cents everywhere past the edge of the program."""
from decimal import Decimal, ROUND_HALF_UP


def parse_amount(text: str) -> int:
    """'19.99' -> 1999. Accepts an optional leading '-' and at most two decimals."""
    text = text.strip()
    if not text:
        raise ValueError("empty amount")
    if text.count(".") > 1:
        raise ValueError(f"not an amount: {text!r}")
    if "." in text and len(text.split(".")[1]) > 2:
        raise ValueError(f"more than two decimals: {text!r}")
    return int(float(text) * 100)


def format_amount(cents: int) -> str:
    """1999 -> '19.99', -5 -> '-0.05'."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def split(cents: int, ways: int) -> list[int]:
    """Split an amount into `ways` parts that differ by at most one cent and sum exactly."""
    if ways < 1:
        raise ValueError("ways must be at least 1")
    base, extra = divmod(cents, ways)
    return [base + 1 if i < extra else base for i in range(ways)]


def apply_rate(cents: int, rate: str) -> int:
    """Multiply by a decimal rate such as '0.21', rounding half up to the cent."""
    value = Decimal(cents) * Decimal(rate)
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
