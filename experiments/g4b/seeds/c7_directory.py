"""The staff directory: sign-up, lookup and search.

Staff work in Germany, Turkey and Greece and type their own display names. Two
accounts may not have names that differ only in letter case, as Unicode defines
case. The search box on the intranet page passes what the user typed to `search`.
"""
from __future__ import annotations

import sqlite3
import unicodedata

SCHEMA = """
CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY,
    display TEXT NOT NULL,
    folded TEXT NOT NULL UNIQUE,
    team TEXT NOT NULL
);
"""


class NameTaken(ValueError):
    pass


def connect(path: str = ":memory:") -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def fold(name: str) -> str:
    """The form two names are compared in."""
    return unicodedata.normalize("NFC", " ".join(name.split())).lower()


def add(db: sqlite3.Connection, display: str, team: str) -> int:
    display = " ".join(display.split())
    if not 2 <= len(display) <= 80:
        raise ValueError("a name is 2-80 characters")
    try:
        cur = db.execute("INSERT INTO staff (display, folded, team) VALUES (?, ?, ?)",
                         (display, fold(display), team))
    except sqlite3.IntegrityError as exc:
        raise NameTaken(display) from exc
    return int(cur.lastrowid)


def find(db: sqlite3.Connection, name: str) -> dict | None:
    row = db.execute("SELECT id, display, team FROM staff WHERE folded = ?", (fold(name),)).fetchone()
    return None if row is None else {"id": row[0], "display": row[1], "team": row[2]}


def search(db: sqlite3.Connection, text: str, limit: int = 20) -> list[str]:
    """Display names containing `text`, ignoring case, at most `limit`, A-Z."""
    text = fold(text)
    if not text:
        return []
    rows = db.execute("SELECT display FROM staff WHERE folded LIKE ? ORDER BY folded LIMIT ?",
                      (f"%{text}%", limit)).fetchall()
    return [r[0] for r in rows]


def rename(db: sqlite3.Connection, staff_id: int, display: str) -> None:
    display = " ".join(display.split())
    try:
        db.execute("UPDATE staff SET display = ?, folded = ? WHERE id = ?",
                   (display, fold(display), staff_id))
    except sqlite3.IntegrityError as exc:
        raise NameTaken(display) from exc
