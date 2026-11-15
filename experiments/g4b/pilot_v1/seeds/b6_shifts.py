from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SITE = ZoneInfo("Europe/Brussels")
RATE_CENTS_PER_HOUR = 2150


def local(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    """A wall-clock time at the site, as an aware datetime."""
    return datetime(y, m, d, hh, mm, tzinfo=SITE)


def hours_worked(start: datetime, end: datetime) -> float:
    """Hours worked between two site times, for payroll. Night shifts cross midnight."""
    if end <= start:
        raise ValueError("a shift ends after it starts")
    return (end - start) / timedelta(hours=1)


def pay(start: datetime, end: datetime) -> int:
    """Pay in cents for one shift, rounded down to the cent."""
    return int(hours_worked(start, end) * RATE_CENTS_PER_HOUR)


def week_of(t: datetime) -> str:
    """ISO year-week, e.g. '2026-W44', for grouping shifts on a payslip."""
    y, w, _ = t.isocalendar()
    return f"{y}-W{w:02d}"
