"""An activity feed served page by page to a mobile client.

The client asks for the first page with no cursor, then passes back the cursor
from each response to get the next one, until a page comes back with no cursor.
Events are inserted by many writers; `created_at` is a whole-second Unix time.
"""
from __future__ import annotations

import base64
import json
import sqlite3

PAGE = 50

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    created_at INTEGER NOT NULL,
    actor TEXT NOT NULL,
    verb TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_created ON events (created_at);
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def encode_cursor(created_at: int) -> str:
    """An opaque token safe to put in a URL query string."""
    raw = json.dumps({"t": created_at}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(token: str) -> int:
    try:
        data = json.loads(base64.urlsafe_b64decode(token.encode()))
        return int(data["t"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("bad cursor") from exc


def page(db: sqlite3.Connection, cursor: str | None = None, size: int = PAGE) -> dict:
    """One page of events, oldest first, and the cursor for the next page."""
    if not 1 <= size <= 200:
        raise ValueError("size must be 1-200")
    after = decode_cursor(cursor) if cursor else -1
    rows = db.execute(
        "SELECT id, created_at, actor, verb FROM events WHERE created_at > ? "
        "ORDER BY created_at LIMIT ?", (after, size)).fetchall()
    items = [{"id": r[0], "at": r[1], "actor": r[2], "verb": r[3]} for r in rows]
    nxt = encode_cursor(rows[-1][1]) if len(rows) == size else None
    return {"items": items, "next": nxt}


def everything(db: sqlite3.Connection, size: int = PAGE) -> list[dict]:
    """Walk the feed the way the client does. Used by the export job."""
    out, cursor = [], None
    while True:
        p = page(db, cursor, size)
        out.extend(p["items"])
        if not p["next"]:
            return out
        cursor = p["next"]
