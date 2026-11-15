import sqlite3
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Sale:
    region: str
    cents: int


class BadRecord(ValueError):
    pass


def validate(sales: Iterable[Sale]) -> None:
    for s in sales:
        if not s.region:
            raise BadRecord("a sale with no region")
        if s.cents < 0:
            raise BadRecord(f"negative sale in {s.region}")


def totals(sales: Iterable[Sale]) -> dict[str, int]:
    """Total cents per region; refuses the whole batch if any record is bad."""
    validate(sales)
    out: dict[str, int] = {}
    for s in sales:
        out[s.region] = out.get(s.region, 0) + s.cents
    return out


def top(totals_by_region: dict[str, int], n: int = 3) -> list[tuple[str, int]]:
    """The n regions with the highest totals, highest first, ties by name."""
    return sorted(totals_by_region.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


def monthly_report(db: sqlite3.Connection, month: str) -> list[tuple[str, int]]:
    """The top three regions for a month such as '2026-10'."""
    rows = db.execute("SELECT region, cents FROM sales WHERE month = ?", (month,))
    return top(totals(Sale(region, cents) for region, cents in rows))
