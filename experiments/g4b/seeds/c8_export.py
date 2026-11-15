"""Expense export for the finance team.

Employees type the merchant and the note on each expense themselves. Finance
downloads the export and opens it in Excel on Windows. The export job runs on a
Windows server.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

COLUMNS = ["date", "employee", "merchant", "amount", "currency", "note"]


@dataclass(frozen=True)
class Expense:
    day: date
    employee: str
    merchant: str
    amount: Decimal
    currency: str
    note: str = ""


def row(e: Expense) -> list[str]:
    return [e.day.isoformat(), e.employee, e.merchant, f"{e.amount:.2f}", e.currency, e.note]


def totals_by_currency(expenses: list[Expense]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for e in expenses:
        out[e.currency] = out.get(e.currency, Decimal(0)) + e.amount
    return dict(sorted(out.items()))


def export(expenses: list[Expense], path: Path) -> int:
    """Write the export file; returns the number of expense rows written."""
    expenses = sorted(expenses, key=lambda e: (e.day, e.employee))
    with open(path, "w", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for e in expenses:
            w.writerow(row(e))
        w.writerow([])
        for cur, total in totals_by_currency(expenses).items():
            w.writerow(["", "", "TOTAL", f"{total:.2f}", cur, ""])
    return len(expenses)
