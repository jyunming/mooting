"""SQLite-backed council board.

Every process in Mooting -- the MCP server each agent CLI spawns for itself, the
supervisor, the human UI -- talks to one file through this module. There is no
server-of-record and no daemon requirement: if nothing else is running, the board
is still readable and writable, which is what lets a failed wake degrade to
catch-up-on-next-turn instead of deadlocking a topic.

Encoding: every text boundary here is pinned to UTF-8 explicitly rather than
left to the platform default. A console that decodes a UTF-8 pipe as something
else produces garbled text that reads like protocol corruption, and diagnosing
that costs a day.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

SCHEMA = Path(__file__).with_name("schema.sql")

#: Kinds that may close a proposal. Enforced in code because the whole point of
#: the platform is that a human holds the decision, and a prompt instruction
#: telling an agent "don't approve your own proposal" is advice, not a fence.
HUMAN_KINDS = frozenset({"human"})

DRIVER_KINDS = frozenset({"stdio_json", "acp", "spawn", "none"})

#: How a topic is framed to its seats. `debate` asks them to find the flaw;
#: `discuss` asks them to build. The difference is entirely in the prompt, and
#: it matters: a seat told disagreement is the product will invent some.
TOPIC_MODES = frozenset({"debate", "discuss", "work"})

#: What a seat may do. `deliberate` seats argue and propose; they must not be able
#: to change anything. `execute` is an explicit escalation the user grants at
#: registration -- an agent woken by a daemon with shell access is a different risk
#: class from one a person is watching, so it is never inferred.
CAPABILITIES = frozenset({"deliberate", "execute"})

#: `@name` in a message body. Restricted to seats on the topic; see
#: Store._record_mentions for why an unseated name stays plain text.
MENTION_RE = re.compile(r"@([A-Za-z0-9_][A-Za-z0-9_-]*)")


#: Leading noise words. Stripped only from the front, only these, deliberately --
#: a clever slugifier that rewrites the middle of someone's sentence produces
#: names they cannot predict, which is worse than a slightly long one.
_LEADING_NOISE = ("the ", "a ", "an ", "on ", "about ", "how to ", "how ")


def slugify(title: str, taken: Iterable[str] = ()) -> str:
    """Derive a typeable handle from a title.

    Nobody should have to invent a short name for their own question. The title
    is the thing they actually have; the slug is a convenience for typing, so it
    is computed rather than demanded.

    Non-alphanumerics become separators and everything else is kept, so a title
    in a non-Latin script keeps its characters instead of slugifying to nothing.
    """
    text = title.strip().lower()
    for noise in _LEADING_NOISE:
        if text.startswith(noise):
            text = text[len(noise):]
            break

    out = "".join(ch if ch.isalnum() else "-" for ch in text)
    words = [w for w in out.split("-") if w]

    slug = ""
    for w in words:
        candidate = f"{slug}-{w}" if slug else w
        if len(candidate) > 40:
            break
        slug = candidate
    slug = slug or "topic"
    if slug.isdigit():                 # would be read as a topic id
        slug = f"topic-{slug}"

    existing = set(taken)
    if slug not in existing:
        return slug
    n = 2
    while f"{slug}-{n}" in existing:
        n += 1
    return f"{slug}-{n}"


def agenda_points(topic) -> list[str]:
    """A topic's agenda as a list of points.

    Empty when the brief is only an echo of the title: `open_topic` seeds it that
    way, so "has an agenda" cannot mean "brief is set" -- it means somebody wrote
    something the title does not already say.

    Lives here rather than in either UI because both the session and the shell
    set agendas, and two copies of this rule would drift the first time one
    changed.
    """
    title = (topic["title"] or "").strip()
    brief = (topic["brief"] or "").strip()
    if not brief or brief == title:
        return []
    return [ln.lstrip("-").strip() for ln in brief.splitlines() if ln.strip()]


def agenda_text(points) -> str:
    """Points back into the stored form."""
    return "\n".join("- " + p.strip() for p in points if p.strip())


def split_points(text: str) -> list[str]:
    """`a; b; c` into three points. One line is all a session input gives you,
    so `;` is how a list gets typed at all."""
    return [seg.strip() for seg in text.split(";") if seg.strip()]


def looks_like_text(path: Path) -> bool:
    """Can this be put in a prompt as characters?

    A NUL byte in the first block is the classic test and it is the right one
    here: the question is not "what format is this" but "will inlining it
    produce text or noise". Decoding as UTF-8 answers the rest.
    """
    try:
        head = path.open("rb").read(8192)
    except OSError:
        return False
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


class StoreError(RuntimeError):
    pass


def clean_text(value: str, what: str) -> str:
    """User text on its way onto the board, or a message naming what was wrong.

    Surrogates arrive from anywhere text is decoded with `surrogateescape` -- a
    mis-set locale, argv on some shells, a file read loosely. They survive every
    layer until SQLite refuses them, and the traceback then points at the write
    rather than at the read. Refused here, where the offending input still has a
    name.
    """
    if not isinstance(value, str):
        return value
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise StoreError(
            f"{what} is not valid text: it carries bytes that were decoded with "
            f"the wrong encoding (position {exc.start}). If it was piped in, the "
            f"source is not UTF-8.") from exc
    return value


#: Routes that cannot be reached from the machine the board lives on. A seat
#: holding a shell can type in a terminal and can call an HTTP port; it cannot
#: be a paired person in a chat.
ON_ANOTHER_MACHINE = frozenset({"telegram"})

#: Bytes behind a handle somebody types. Four rather than three: a pairing
#: handle is the thing that stands between a stranger and a seat, and 24 bits is
#: cheap to guess at online and starts colliding on a board that lives a while.
HANDLE_BYTES = 4


def new_handle() -> str:
    """A handle short enough to retype and long enough to be worth nothing."""
    return secrets.token_hex(HANDLE_BYTES)


def chain_hash(prev: str | None, eid: int, topic_id, kind: str, actor: str,
               payload: str, created_at: str) -> str:
    """One link. Covers the row and the link before it, so an edit anywhere
    downstream stops matching from that point on."""
    material = "\x1f".join([prev or "", str(eid), str(topic_id), kind, actor,
                            payload, created_at or ""])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class NotAuthorised(StoreError):
    """Raised when a seat tries to exercise a power it does not hold."""


@dataclass(frozen=True)
class Event:
    id: int
    topic_id: int | None
    kind: str
    actor: str
    payload: dict[str, Any]
    created_at: str


#: Boards live together under the home directory, one per working directory.
#: Scattering a `.mooting/` into every folder you ever ran this from means a
#: council you cannot find again unless you remember where you were standing.
HOME_BOARDS = Path.home() / ".mooting" / "boards"


def board_key(directory: Path) -> str:
    """A readable, collision-free directory name for one working directory.

    The folder's own name is what makes it recognisable when you look in
    ~/.mooting/boards; the digest is what stops two checkouts called `api` from
    sharing a board.
    """
    full = directory.resolve()
    # normcase, not lower(): Windows and macOS treat C:\Dev and C:\dev as one
    # directory and must key to one board, while on Linux they are genuinely two
    # and must not be merged. normcase is exactly that distinction.
    canonical = os.path.normcase(str(full))
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.normcase(full.name)).strip("-.") or "root"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]
    return f"{name}-{digest}"


def default_db_path(cwd: Path | None = None) -> Path:
    """Where this directory's board lives.

    Explicit beats local beats central: `$MOOTING_DB` wins outright, a
    `.mooting/` that already exists here is honoured so a deliberately
    project-local board keeps working, and otherwise the board is the one this
    directory owns under the home directory.
    """
    env = os.environ.get("MOOTING_DB")
    if env:
        return Path(env)
    here = cwd or Path.cwd()
    local = here / ".mooting" / "board.db"
    if local.exists():
        return local
    return HOME_BOARDS / board_key(here) / "board.db"


class Store:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,  # explicit transactions; see tx()
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.text_factory = str  # sqlite3 decodes as UTF-8; be explicit about it
        # N writers on one file. WAL lets readers proceed during a write, and
        # busy_timeout turns the remaining lock contention into a wait instead of
        # an immediate "database is locked" that would surface as a fake agent error.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")

    # ------------------------------------------------------------------ plumbing

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """IMMEDIATE so a write-intent transaction takes the write lock up front,
        rather than upgrading mid-transaction and hitting SQLITE_BUSY on a peer.

        **Never `await` inside a `tx()` block.** The supervisor runs a whole round
        of seats concurrently on one event loop sharing this connection, and it is
        only safe because every transaction opens and closes synchronously -- no
        coroutine can suspend mid-transaction and let another interleave. That is a
        rule, not an accident: an await in here would corrupt the board under
        concurrency, and the failure would be rare and non-deterministic.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def q(self, sql: str, args: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self._conn.execute(sql, args))

    def q1(self, sql: str, args: Sequence[Any] = ()) -> sqlite3.Row | None:
        cur = self._conn.execute(sql, args)
        return cur.fetchone()

    def init_schema(self) -> None:
        self._conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        # CREATE TABLE IF NOT EXISTS cannot add a column to a board that already
        # exists, so new columns are migrated explicitly. Cheap and idempotent.
        # `asking` defaults to 1 so rows written before the distinction existed
        # keep blocking, which is what the topics holding them expect.
        # `chair` needs no backfill: NULL falls back to `opened_by`, so every
        # topic on an existing board keeps a chair without one being invented.
        for table, column, ddl in (("topics", "mode", "TEXT NOT NULL DEFAULT 'debate'"),
                                   ("topics", "effort", "TEXT"),
                                   ("topics", "chair", "TEXT"),
                                   ("rooms", "topic", "TEXT"),
                                   ("rooms", "host", "TEXT"),
                                   ("topics", "room_id", "INTEGER"),
                                   ("pairings", "ref", "TEXT"),
                                   ("agents", "tg_user_id", "TEXT"),
                                   ("mentions", "asking", "INTEGER NOT NULL DEFAULT 1"),
                                   ("events", "hash", "TEXT"),
                                   ("wakes", "tokens_in", "INTEGER"),
                                   ("wakes", "tokens_out", "INTEGER"),
                                   ("wakes", "cost_usd", "REAL"),
                                   ("tasks", "base_sha", "TEXT"),
                                   ("tasks", "depends_on", "INTEGER")):
            cols = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                if (table, column) == ("mentions", "asking"):
                    self._backfill_asking()


        # Every open, not only when the column is added: a request with no handle
        # cannot be answered, and one can arrive that way from an older board or
        # a path that forgot to set it. The table is small and this is idempotent.
        for row in self._conn.execute(
                "SELECT id FROM pairings WHERE ref IS NULL OR ref = ''").fetchall():
            self._conn.execute("UPDATE pairings SET ref = ? WHERE id = ?",
                               (new_handle(), row["id"]))

    def _backfill_asking(self) -> None:
        """Decide, for mentions written before the column existed, which were asks.

        `ask` posts a body that opens with `@target `, while a name found by
        reading prose sits anywhere else in the paragraph. Taking the column's
        default instead would leave every historical topic paused on somebody's
        summary -- which is the behaviour the column was added to end.
        """
        self._conn.execute(
            "UPDATE mentions SET asking = 0 "
            "WHERE question NOT LIKE '@' || target || ' %' "
            "  AND question NOT LIKE '@' || target || ',%'"
        )

    # ------------------------------------------------------------------- events

    def _emit(
        self,
        conn: sqlite3.Connection,
        topic_id: int | None,
        kind: str,
        actor: str,
        payload: dict[str, Any] | None = None,
    ) -> int:
        body = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
        cur = conn.execute(
            "INSERT INTO events (topic_id, kind, actor, payload) VALUES (?,?,?,?)",
            (topic_id, kind, actor, body),
        )
        eid = int(cur.lastrowid)
        # Chained in the same transaction as the row it covers, so an event and
        # its link are never separately true. The previous link comes from the
        # board rather than from memory: two processes write here.
        row = conn.execute(
            "SELECT created_at FROM events WHERE id = ?", (eid,)).fetchone()
        prev = conn.execute(
            "SELECT hash FROM events WHERE id < ? ORDER BY id DESC LIMIT 1",
            (eid,)).fetchone()
        link = chain_hash(prev["hash"] if prev else None, eid, topic_id, kind,
                          actor, body, row["created_at"])
        conn.execute("UPDATE events SET hash = ? WHERE id = ?", (link, eid))
        return eid

    def events_since(self, cursor: int, topic_id: int | None = None, limit: int = 200,
                     until: int | None = None) -> list[Event]:
        """Events in (cursor, until]. The upper bound is what makes a concurrent
        round actually simultaneous: without it, a seat whose prompt is built a
        moment later sees a peer's message from the same round, and the seats are
        no longer answering the same board."""
        ceiling = until if until is not None else 2**62
        if topic_id is None:
            rows = self.q("SELECT * FROM events WHERE id > ? AND id <= ? ORDER BY id LIMIT ?",
                          (cursor, ceiling, limit))
        else:
            rows = self.q(
                "SELECT * FROM events WHERE id > ? AND id <= ? AND topic_id = ? ORDER BY id LIMIT ?",
                (cursor, ceiling, topic_id, limit),
            )
        return [
            Event(r["id"], r["topic_id"], r["kind"], r["actor"], json.loads(r["payload"]), r["created_at"])
            for r in rows
        ]

    def verify_chain(self) -> dict:
        """Walk the chain and say where, if anywhere, it stops adding up.

        Reports the unchained prefix rather than hiding it: a board that existed
        before this was written cannot be made tamper-evident retroactively, and
        saying so is the difference between a check and a decoration.
        """
        rows = self.q("SELECT * FROM events ORDER BY id")
        unchained = 0
        prev = None
        for row in rows:
            if row["hash"] is None:
                unchained += 1
                continue
            want = chain_hash(prev, int(row["id"]), row["topic_id"], row["kind"],
                              row["actor"], row["payload"], row["created_at"])
            if want != row["hash"]:
                return {"ok": False, "broken_at": int(row["id"]),
                        "checked": len(rows) - unchained, "unchained": unchained}
            prev = row["hash"]
        return {"ok": True, "broken_at": None,
                "checked": len(rows) - unchained, "unchained": unchained}

    def verify_bodies(self) -> list[int]:
        """Message ids whose text no longer matches what was announced."""
        bad = []
        for ev in self.q("SELECT * FROM events WHERE kind = 'message'"):
            payload = json.loads(ev["payload"])
            want, mid = payload.get("digest"), payload.get("message_id")
            if not want or not mid:
                continue
            row = self.q1("SELECT body FROM messages WHERE id = ?", (mid,))
            if row is None:
                bad.append(int(mid))
                continue
            if hashlib.sha256(row["body"].encode("utf-8")).hexdigest() != want:
                bad.append(int(mid))
        return bad

    def verify_decisions(self) -> list[dict]:
        """Sign-offs whose row no longer matches the event that recorded them.

        The chain covers events, and a proposal's verdict lives in a column
        beside them: `UPDATE proposals SET decided_by = ...` left an intact chain
        and a different name against the one act this project exists to
        attribute. Checked against the event's own actor, which is the record.
        """
        wrong = []
        for ev in self.q("SELECT * FROM events WHERE kind = 'decision' ORDER BY id"):
            payload = json.loads(ev["payload"])
            pid = payload.get("proposal_id")
            if pid is None:
                continue
            row = self.q1("SELECT decided_by, status FROM proposals WHERE id = ?", (pid,))
            if row is None:
                wrong.append({"proposal_id": pid, "expected": ev["actor"],
                              "found": "the proposal is gone"})
                continue
            if row["decided_by"] != ev["actor"]:
                wrong.append({"proposal_id": pid, "expected": ev["actor"],
                              "found": row["decided_by"]})
            elif payload.get("status") and row["status"] != payload["status"]:
                wrong.append({"proposal_id": pid, "expected": payload["status"],
                              "found": row["status"]})
        return wrong

    def head(self) -> int:
        row = self.q1("SELECT COALESCE(MAX(id), 0) AS h FROM events")
        return int(row["h"]) if row else 0

    # ------------------------------------------------------------------- agents

    def add_agent(
        self,
        name: str,
        kind: str,
        *,
        display: str | None = None,
        driver: str | None = None,
        driver_cfg: dict[str, Any] | None = None,
    ) -> None:
        cap = (driver_cfg or {}).get("capability")
        if cap is not None and cap not in CAPABILITIES:
            # A typo used to be accepted and only surfaced later as a confusing
            # "not registered with execute capability" refusal.
            raise StoreError(
                f"unknown capability {cap!r}; expected one of {sorted(CAPABILITIES)}")
        driver = driver or ("none" if kind in HUMAN_KINDS or kind == "external" else "spawn")
        if driver not in DRIVER_KINDS:
            raise StoreError(f"unknown driver {driver!r}; expected one of {sorted(DRIVER_KINDS)}")
        with self.tx() as c:
            c.execute(
                """INSERT INTO agents (name, kind, display, driver, driver_cfg)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET
                     kind=excluded.kind, display=excluded.display,
                     driver=excluded.driver, driver_cfg=excluded.driver_cfg""",
                (name, kind, display or name, driver, json.dumps(driver_cfg or {}, ensure_ascii=False)),
            )
            self._emit(c, None, "seat", name, {"action": "registered", "kind": kind, "driver": driver})

    def agent(self, name: str) -> sqlite3.Row:
        row = self.q1("SELECT * FROM agents WHERE name = ?", (name,))
        if row is None:
            raise StoreError(f"no such agent: {name!r}")
        return row

    def agents(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM agents ORDER BY name")

    #: Every place a seat's name is written down. `messages.author` and friends are
    #: plain text with no foreign key, so a rename has to visit them by hand --
    #: leaving them behind would orphan everything the seat ever said.
    NAME_COLUMNS = (
        ("seats", "agent"), ("messages", "author"),
        ("mentions", "asker"), ("mentions", "target"),
        ("proposals", "author"), ("proposals", "decided_by"),
        ("votes", "agent"), ("wakes", "agent"),
        ("tasks", "assignee"), ("tasks", "created_by"),
        ("topics", "opened_by"), ("topics", "chair"),
        # `pairings.seat` carries a real foreign key, so leaving it behind did not
        # orphan a row quietly -- it failed the whole rename at COMMIT. `/me` in a
        # terminal worked and `/me` in a chat answered "FOREIGN KEY constraint
        # failed", because only a paired person has a row here.
        ("pairings", "seat"),
        # `room_seats.agent` is another real foreign key, for the same reason.
        ("room_seats", "agent"),
    )

    def rename_agent(self, old: str, new: str) -> None:
        """Rename a seat everywhere, so the record stays coherent.

        A seat's name is its identity on the board, and it is also how you address
        it. Changing one without the other would leave a transcript attributed to
        somebody who no longer exists.
        """
        self.agent(old)
        if not new or any(c.isspace() for c in new) or new.startswith("@"):
            raise StoreError(f"{new!r} must be a single word without a leading @")
        if self.q1("SELECT 1 FROM agents WHERE name = ?", (new,)):
            raise StoreError(f"{new!r} is already taken")
        with self.tx() as c:
            # seats.agent points at agents.name, so renaming the parent breaks the
            # child for the rest of the transaction whichever order you pick.
            # Deferring enforcement to COMMIT lets both sides move together.
            c.execute("PRAGMA defer_foreign_keys = ON")
            c.execute("UPDATE agents SET name = ? WHERE name = ?", (new, old))
            for table, column in self.NAME_COLUMNS:
                c.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                          (new, old))

    def delete_agent(self, name: str) -> dict[str, int]:
        """Remove a seat from the registry.

        Its seats go with it, because `seats.agent` has a foreign key. Its past
        messages stay: `messages.author` is plain text, and silently rewriting the
        record to tidy the roster would be the wrong trade -- what was said was
        still said.
        """
        counts = {
            "seats": self.q1("SELECT COUNT(*) c FROM seats WHERE agent = ?",
                             (name,))["c"],
            "messages": self.q1("SELECT COUNT(*) c FROM messages WHERE author = ?",
                                (name,))["c"],
        }
        # tasks.assignee is a NOT NULL foreign key onto agents(name), so deleting
        # a seat that was ever assigned work raised a raw IntegrityError from
        # sqlite instead of anything a caller could act on. Refusing is right:
        # the alternative is deleting somebody's work log to tidy a roster.
        owns = self.q("SELECT id, title FROM tasks WHERE assignee = ? OR created_by = ?",
                      (name, name))
        if owns:
            raise StoreError(
                f"{name!r} is on {len(owns)} task(s) (#{owns[0]['id']} "
                f"{owns[0]['title']!r}...). Delete those topics first, or leave the "
                f"seat registered -- its work log refers to it.")
        # `pairings.seat` is a foreign key too, and removing a seat that somebody
        # is paired to raised the same raw IntegrityError. Their access goes with
        # the seat -- a pairing onto a seat that no longer exists grants nothing
        # and cannot be repaired from a chat -- and the count says so rather than
        # letting it happen quietly.
        counts["pairings"] = self.q1(
            "SELECT COUNT(*) c FROM pairings WHERE seat = ?", (name,))["c"]
        counts["teams"] = self.q1(
            "SELECT COUNT(*) c FROM room_seats WHERE agent = ?", (name,))["c"]
        with self.tx() as c:
            c.execute("DELETE FROM room_seats WHERE agent = ?", (name,))
            # Leaving the chair pointing at somebody who is gone made the topic
            # undecidable: not the chair for everyone else, not a human seat for
            # the name itself. Clearing it falls back to whoever opened it.
            c.execute("UPDATE topics SET chair = NULL WHERE chair = ?", (name,))
            c.execute("DELETE FROM pairings WHERE seat = ?", (name,))
            c.execute("DELETE FROM seats WHERE agent = ?", (name,))
            c.execute("DELETE FROM agents WHERE name = ?", (name,))
        return counts

    def is_human(self, name: str) -> bool:
        row = self.q1("SELECT kind FROM agents WHERE name = ?", (name,))
        return bool(row) and row["kind"] in HUMAN_KINDS

    # ------------------------------------------------------------------- topics

    def open_topic(
        self,
        slug: str,
        title: str,
        brief: str,
        opened_by: str,
        *,
        seats: Iterable[str] = (),
        max_rounds: int = 3,
        #: None means "one turn per round", which is what concurrent rounds
        #: actually allow. A separate larger number just made the seat panel say
        #: 2/6 on a topic that could never reach 6.
        max_turns: int | None = None,
        mode: str = "debate",
        effort: str | None = None,
        manager: str | None = None,
        room_id: int | None = None,
    ) -> int:
        # A slug is looked up by name, but every reference site accepts an id too
        # and decides which by `isdigit()`. So an all-numeric slug creates a topic
        # that cannot be reached by its own name -- it resolves to a topic *id*
        # that almost certainly does not exist. Refuse it at creation rather than
        # let someone find out later.
        if not slug or slug.strip() != slug or any(c.isspace() for c in slug):
            raise StoreError(f"slug {slug!r} must be a single word with no spaces")
        if slug.isdigit():
            raise StoreError(
                f"slug {slug!r} is all digits, which would be read as a topic id. "
                "Give it a word -- e.g. `plans-2026`.")
        title = clean_text(title, "the title")
        brief = clean_text(brief, "the brief")
        if mode not in TOPIC_MODES:
            raise StoreError(f"unknown mode {mode!r}; expected one of {sorted(TOPIC_MODES)}")
        with self.tx() as c:
            cur = c.execute(
                """INSERT INTO topics (slug, title, brief, opened_by, max_rounds,
                                         mode, effort, room_id)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (slug, title, brief, opened_by, max_rounds, mode, effort, room_id),
            )
            topic_id = int(cur.lastrowid)
            turns = max_rounds if max_turns is None else max_turns
            for agent in seats:
                c.execute(
                    "INSERT INTO seats (topic_id, agent, role, max_turns) VALUES (?,?,?,?)",
                    (topic_id, agent, "manager" if agent == manager else "participant",
                     turns),
                )
            c.execute(
                "INSERT INTO messages (topic_id, author, kind, body) VALUES (?,?,'system',?)",
                (topic_id, opened_by, brief),
            )
            self._emit(c, topic_id, "topic", opened_by,
                       {"action": "opened", "slug": slug, "seats": list(seats)})
        return topic_id

    def topic(self, ref: int | str) -> sqlite3.Row:
        col = "id" if isinstance(ref, int) else "slug"
        row = self.q1(f"SELECT * FROM topics WHERE {col} = ?", (ref,))
        if row is None:
            raise StoreError(f"no such topic: {ref!r}")
        return row

    def topics(self, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return self.q("SELECT * FROM topics WHERE status = ? ORDER BY id DESC", (status,))
        return self.q("SELECT * FROM topics ORDER BY id DESC")

    def set_rounds(self, topic_id: int, n: int, actor: str) -> None:
        """Make this topic run to `n` rounds.

        Turns move with it. A seat speaks at most once a round, so a seat capped
        below the round count is a seat that goes quiet half way through a
        meeting that is still running -- and from the outside that looks like the
        agent failing, not like a budget. Never reduces a budget somebody raised
        on purpose: rounds are the binding cap anyway.

        A person only. A cap that an agent could raise is not a cap, and this one
        exists to stop a council spending a subscription with nobody watching.
        """
        if not self.is_human(actor):
            raise NotAuthorised(f"{actor!r} may not change the round budget")
        if n < 1:
            raise StoreError("a topic runs for at least one round")
        with self.tx() as c:
            c.execute("UPDATE topics SET max_rounds = ? WHERE id = ?", (n, topic_id))
            c.execute("UPDATE seats SET max_turns = MAX(max_turns, ?) WHERE topic_id = ?",
                      (n, topic_id))

    def raise_budget(self, topic_id: int, rounds: int, turns: int,
                     actor: str) -> tuple[int, int]:
        """Bring a topic up to a budget, never down. Returns what it now holds.

        Used when the chair turns the effort dial: a question worth deep thinking
        is usually worth more rounds, and one worth a quick answer is not worth
        five. Raising only, for the reason `set_rounds` has always given -- a
        budget somebody granted on purpose is not something a later setting
        should quietly take back.
        """
        if not self.is_human(actor):
            raise NotAuthorised(f"{actor!r} may not change the budget")
        with self.tx() as c:
            c.execute("UPDATE topics SET max_rounds = MAX(max_rounds, ?) WHERE id = ?",
                      (rounds, topic_id))
            c.execute("UPDATE seats SET max_turns = MAX(max_turns, ?) WHERE topic_id = ?",
                      (turns, topic_id))
        t = self.topic(topic_id)
        held = [r["max_turns"] for r in self.seats(topic_id)]
        return int(t["max_rounds"]), max(held) if held else turns

    def grant_rounds(self, topic_id: int, n: int, actor: str) -> None:
        """More rounds, and the per-seat turns to use them. A person only.

        Raising one without the other is the trap: a seat that has spent its turns
        stays capped however many rounds you add, so the council looks alive and
        says nothing.
        """
        if not self.is_human(actor):
            raise NotAuthorised(f"{actor!r} may not grant more rounds")
        with self.tx() as c:
            c.execute("UPDATE topics SET max_rounds = max_rounds + ? WHERE id = ?",
                      (n, topic_id))
            c.execute("UPDATE seats SET max_turns = max_turns + ? WHERE topic_id = ?",
                      (n, topic_id))

    def conclude(self, topic_id: int, by: str, note: str = "",
                 via: str = "local") -> int:
        """Close the meeting, on the record.

        A meeting that just stops is not the same as one that concluded, and the
        difference matters later: minutes of an abandoned discussion read exactly
        like minutes of a settled one unless somebody said which it was.

        Reserved to a human for the same reason a ruling is: an agent deciding the
        meeting is over would be deciding the outcome.
        """
        if not self.is_human(by):
            raise NotAuthorised(f"{by!r} is not a human seat; only a human closes a meeting")
        if via not in ON_ANOTHER_MACHINE and self.executing_now():
            raise NotAuthorised(
                "a seat is executing and holds a shell on this machine; conclude "
                "from a chat, or wait for it to finish")
        seated = self.chair(topic_id)
        if seated and by != seated:
            raise NotAuthorised(
                f"{by!r} is not the chair of this meeting — {seated} concludes it")
        topic = self.topic(topic_id)
        if topic["status"] in {"resolved", "aborted"}:
            raise StoreError(f"`{topic['slug']}` is already {topic['status']}")
        with self.tx() as c:
            c.execute("INSERT INTO messages (topic_id, author, kind, body) "
                      "VALUES (?,?,'ruling',?)",
                      (topic_id, by, note.strip() or "Meeting concluded."))
            c.execute("UPDATE topics SET status = 'resolved', "
                      "closed_at = datetime('now') WHERE id = ?", (topic_id,))
            self._emit(c, topic_id, "topic", by, {"action": "resolved", "note": note})
        return topic_id

    def reopen(self, topic_id: int, by: str) -> None:
        """Concluding is not meant to be a trap; a meeting can be resumed."""
        if not self.is_human(by):
            raise NotAuthorised(f"{by!r} is not a human seat")
        with self.tx() as c:
            c.execute("UPDATE topics SET status = 'open', closed_at = NULL WHERE id = ?",
                      (topic_id,))
            self._emit(c, topic_id, "topic", by, {"action": "reopened"})

    def closing_note(self, topic_id: int):
        """The chair's last word, if there was one: a ruling attached to no proposal."""
        return self.q1(
            "SELECT * FROM messages WHERE topic_id = ? AND kind = 'ruling' "
            "AND proposal_id IS NULL ORDER BY id DESC LIMIT 1", (topic_id,))

    def set_topic_status(self, topic_id: int, status: str, actor: str, note: str = "") -> None:
        """Open, pause or close a topic.

        Closing is a decision, so `resolved` and `aborted` need a person, the same
        as `conclude` and `decide`. Opening and pausing are not: the supervisor
        parks a topic itself when a cap is reached, which is the whole point of a
        cap.
        """
        if status in {"resolved", "aborted"} and not self.is_human(actor):
            raise NotAuthorised(f"{actor!r} may not close a topic")
        with self.tx() as c:
            closed = "datetime('now')" if status in {"resolved", "aborted"} else "NULL"
            c.execute(f"UPDATE topics SET status = ?, closed_at = {closed} WHERE id = ?", (status, topic_id))
            self._emit(c, topic_id, "topic", actor, {"action": status, "note": note})

    def chair(self, topic_id: int) -> str | None:
        """Who signs off here. Whoever opened the meeting, unless it named someone.

        Anybody may call a meeting and argue in it. Closing a proposal and
        concluding the meeting belong to one person, because "a human decided"
        says very little when everybody in the room can decide and nobody in
        particular is answerable for it.

        `None` when nobody named is still on the board. A meeting must not become
        undecidable because the person who chaired it was removed, so it falls
        back to the rule that applied before chairs existed: any person may close
        it. Deleting a seat clears the chair as well, and this is the second line
        for a board where `opened_by` names somebody long gone.
        """
        t = self.topic(topic_id)
        for candidate in (t["chair"], t["opened_by"]):
            if candidate and self.is_human(candidate):
                return candidate
        return None

    def set_chair(self, topic_id: int, who: str, actor: str) -> None:
        """Hand the chair over. Only the sitting chair may, and only to a person."""
        if not self.is_human(who):
            raise NotAuthorised(
                f"{who!r} is not a human seat; an agent cannot chair a meeting")
        seated = self.chair(topic_id)
        if seated and actor != seated:
            raise NotAuthorised(f"only {seated} may hand over the chair")
        if not self.is_human(actor):
            raise NotAuthorised(f"{actor!r} is not a human seat")
        with self.tx() as c:
            c.execute("UPDATE topics SET chair = ? WHERE id = ?", (who, topic_id))
            self._emit(c, topic_id, "topic", actor, {"action": "chair", "chair": who})

    # ----------------------------------------------------------------- settings

    def setting(self, key: str, default: str | None = None) -> str | None:
        row = self.q1("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str | None) -> None:
        """Remember, or forget when `value` is None."""
        with self.tx() as c:
            if value is None:
                c.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                c.execute(
                    "INSERT INTO settings (key, value, updated_at) "
                    "VALUES (?, ?, datetime('now')) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                    "updated_at = excluded.updated_at", (key, value))

    def seat_human(self, topic_id: int, seat: str) -> bool:
        """Give a human a seat on this topic if they have not got one.

        A person who pairs into a chat can rule -- `decide` asks only whether
        they are human -- but could not *speak*, because `post` requires a seat
        and pairing grants none. So the second person in a room could approve a
        plan and not say why, which is the wrong way round.

        Human only, and idempotent. Seating an agent is a decision about whose
        subscription gets spent, and it stays deliberate.
        """
        if not self.is_human(seat):
            raise NotAuthorised(
                f"{seat!r} is not a human seat; seating an agent spends its "
                f"subscription and stays a deliberate act")
        if self.seat(topic_id, seat) is not None:
            return False
        topic = self.topic(topic_id)
        with self.tx() as c:
            c.execute("INSERT INTO seats (topic_id, agent, role, max_turns) "
                      "VALUES (?, ?, 'participant', ?)",
                      (topic_id, seat, topic["max_rounds"]))
            self._emit(c, topic_id, "seat", seat, {"action": "joined"})
        return True

    # ------------------------------------------------------------- driving

    #: Who is driving a topic, and since when. A setting rather than a table: it
    #: is one fact, and it should vanish the moment nobody holds it.
    DRIVE_KEY = "drive.holder"

    #: A claim older than this is assumed abandoned. A browser tab closed
    #: mid-round would otherwise hold the lock for ever, and a council nobody
    #: can start is worse than one two people race for.
    DRIVE_STALE_S = 900.0

    def take_drive(self, topic_id: int, who: str) -> str | None:
        """Claim the right to drive. Returns the current holder if refused.

        Sessions are separate processes -- two browser tabs are two `mooting
        tui` -- so "am I already driving" cannot be a variable inside one of
        them. It has to be on the board, which is the only thing they share.
        """
        import time

        raw = self.setting(f"{self.DRIVE_KEY}.{topic_id}")
        if raw:
            held = json.loads(raw)
            if held["who"] != who and time.time() - held["at"] < self.DRIVE_STALE_S:
                return held["who"]
        self.set_setting(f"{self.DRIVE_KEY}.{topic_id}",
                         json.dumps({"who": who, "at": time.time()}))
        return None

    def release_drive(self, topic_id: int, who: str) -> None:
        """Give it up. Only the holder may, so a late release cannot steal it."""
        raw = self.setting(f"{self.DRIVE_KEY}.{topic_id}")
        if raw and json.loads(raw).get("who") == who:
            self.set_setting(f"{self.DRIVE_KEY}.{topic_id}", None)

    # ------------------------------------------------------------- API tokens

    #: One token per human seat. Kept as settings rather than a table because
    #: that is all it is -- a secret bound to a name -- and a table would invite
    #: the idea that tokens are things with a lifecycle.
    TOKEN_PREFIX = "api.token."

    def grant_token(self, seat: str, token: str | None = None) -> str:
        """Issue an API token for a human seat.

        Human only, and checked here rather than at the edge. `Store.decide`
        refuses a non-human because locally identity comes from the operating
        system; over a socket the token *is* the identity, so a token on an
        agent seat would be a way to rule as one.
        """
        import secrets as _secrets

        if not self.is_human(seat):
            raise NotAuthorised(
                f"{seat!r} is not a human seat; a token is an identity, and one "
                f"on an agent seat would be a way to rule as that agent")
        token = token or _secrets.token_urlsafe(24)
        self.set_setting(f"{self.TOKEN_PREFIX}{seat}", token)
        return token

    def revoke_token(self, seat: str) -> None:
        self.set_setting(f"{self.TOKEN_PREFIX}{seat}", None)

    def seat_for_token(self, token: str) -> str | None:
        """Whose token this is, or None.

        Compared in constant time against every issued token: a plain `==` over
        a secret leaks its prefix to anyone willing to measure, and there are
        never many seats.
        """
        import secrets as _secrets

        if not token:
            return None
        for row in self.q("SELECT key, value FROM settings WHERE key LIKE ?",
                          (f"{self.TOKEN_PREFIX}%",)):
            if _secrets.compare_digest(row["value"], token):
                seat = row["key"][len(self.TOKEN_PREFIX):]
                # A seat can stop being human -- renamed, replaced -- after a
                # token was issued. The check that matters is the current one.
                return seat if self.is_human(seat) else None
        return None

    def token_holders(self) -> list[str]:
        return [r["key"][len(self.TOKEN_PREFIX):]
                for r in self.q("SELECT key FROM settings WHERE key LIKE ? "
                                "ORDER BY key", (f"{self.TOKEN_PREFIX}%",))]

    def audit(self, actor: str, action: str, detail: dict | None = None,
              topic_id: int | None = None) -> None:
        """Record that something was done from outside this machine.

        Locally the board is the audit: a message has an author, a ruling has a
        decider. Over a socket the *route* matters too -- an approval that
        arrived by HTTP is a different fact from one typed at the terminal, and
        after the event only the board can say which it was.
        """
        with self.tx() as c:
            self._emit(c, topic_id, "remote", actor,
                       {"action": action, **(detail or {})})

    # -------------------------------------------------------------------- rooms

    #: Work with no chat behind it still happens somewhere. A terminal session
    #: opens topics in the local room, so a room is never a special case that
    #: half the code has to remember.
    LOCAL_ROOM = ("local", "board")

    def ensure_room(self, channel: str = "local", chat_id: str = "board",
                    label: str = "") -> int:
        """The room's id, creating it the first time it is used."""
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO rooms (channel, chat_id, label) "
                      "VALUES (?,?,?)", (channel, str(chat_id), label))
            if label:
                c.execute("UPDATE rooms SET label = ? WHERE channel = ? AND chat_id = ?",
                          (label, channel, str(chat_id)))
        return int(self.q1("SELECT id FROM rooms WHERE channel = ? AND chat_id = ?",
                           (channel, str(chat_id)))["id"])

    def room(self, channel: str, chat_id: str) -> sqlite3.Row | None:
        return self.q1("SELECT * FROM rooms WHERE channel = ? AND chat_id = ?",
                       (channel, str(chat_id)))

    def rooms(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM rooms ORDER BY id")

    def room_host(self, room_id: int) -> str | None:
        """Whose room this is, if anybody's yet.

        Not the same as a topic's chair. A chair runs one meeting and can be
        handed over; a host owns the room and decides who is let into it. Being
        let into a council is not being handed the ability to let others in.
        """
        row = self.q1("SELECT host FROM rooms WHERE id = ?", (room_id,))
        return row["host"] if row and row["host"] else None

    def claim_room(self, room_id: int, who: str) -> str:
        """Make somebody the host, if the room has none. Returns the host."""
        held = self.room_host(room_id)
        if held:
            return held
        if not self.is_human(who):
            raise NotAuthorised(f"{who!r} is not a human seat; an agent cannot "
                                f"host a room")
        with self.tx() as c:
            c.execute("UPDATE rooms SET host = ? WHERE id = ?", (who, room_id))
        return who

    def set_room_topic(self, room_id: int, slug: str | None) -> None:
        """Remember where a room is standing, across restarts."""
        with self.tx() as c:
            c.execute("UPDATE rooms SET topic = ? WHERE id = ?", (slug, room_id))

    def room_topic(self, channel: str, chat_id: str) -> str | None:
        """The topic a room was left on, if it is still there."""
        row = self.room(channel, chat_id)
        if row is None or not row["topic"]:
            return None
        try:
            self.topic(row["topic"])
        except StoreError:
            return None
        return row["topic"]

    def topics_for_room(self, room_id: int | None) -> list[sqlite3.Row]:
        """What this room may see: its own meetings, and the unbound ones.

        A topic opened at a terminal is unbound and stays readable everywhere,
        because starting at the desk and following on a phone is the workflow.
        One opened in a chat belongs to that chat, which is what keeps two teams
        on one board from reading each other.
        """
        if room_id is None:
            # A terminal is the machine the board lives on, so it sees the whole
            # board. Returning only the unbound ones hid every meeting opened in
            # a chat from the one place that administers them.
            return self.q("SELECT * FROM topics ORDER BY id DESC")
        return self.q("SELECT * FROM topics WHERE room_id IS NULL OR room_id = ? "
                      "ORDER BY id DESC", (room_id,))

    def topic_visible_in(self, topic_id: int | None, room_id: int | None) -> bool:
        """Whether a room may be told about something that happened on a topic."""
        if topic_id is None or room_id is None:
            # None asks as the terminal, which sees everything.
            return True
        row = self.q1("SELECT room_id FROM topics WHERE id = ?", (topic_id,))
        if row is None or row["room_id"] is None:
            return True
        return row["room_id"] == room_id

    def room_team(self, room_id: int) -> list[str]:
        """The seats a meeting opened in this room starts with."""
        return [r["agent"] for r in self.q(
            "SELECT agent FROM room_seats WHERE room_id = ? ORDER BY position, agent",
            (room_id,))]

    def set_room_team(self, room_id: int, agents: Iterable[str], actor: str) -> list[str]:
        """Redefine the room's team. A person only, and only over real seats.

        Deliberately a replacement rather than a merge: `/seats add` on a single
        meeting is the temporary gesture, and this is the one that sticks. Two
        commands that both half-change a roster is how you end up unable to say
        what the team is.
        """
        if not self.is_human(actor):
            raise NotAuthorised(f"{actor!r} is not a human seat; a person sets the team")
        wanted = list(dict.fromkeys(agents))
        for name in wanted:
            self.agent(name)                     # raises if it is not a seat
        with self.tx() as c:
            c.execute("DELETE FROM room_seats WHERE room_id = ?", (room_id,))
            for slot, name in enumerate(wanted):
                c.execute("INSERT INTO room_seats (room_id, agent, position) "
                          "VALUES (?,?,?)", (room_id, name, slot))
        return wanted

    # ------------------------------------------------------------------ pairing

    def pairing(self, chat_id: str, user_id: str, channel: str = "telegram"):
        return self.q1("SELECT * FROM pairings WHERE channel = ? AND chat_id = ? "
                       "AND user_id = ?", (channel, str(chat_id), str(user_id)))

    #: How long a claim code is worth anything. Long enough to walk to a phone,
    #: short enough that a code left on a screen is not a standing invitation.
    CLAIM_TTL_S = 900.0

    def new_claim(self, seat: str, ttl_s: float | None = None) -> str:
        """A one-time code that proves whoever redeems it reached this machine.

        Every other way of identifying the owner was something a stranger could
        produce: a name passed on the command line, being the first to pair, or
        creating the Telegram group. This one cannot be produced without reading
        the terminal the board lives on, which is the thing an owner actually
        has and nobody else does.
        """
        if not self.is_human(seat):
            raise NotAuthorised(f"{seat!r} is not a human seat")
        import time

        code = new_handle()
        self.set_setting("claim.code", code)
        self.set_setting("claim.seat", seat)
        self.set_setting("claim.expires",
                         str(time.time() + (self.CLAIM_TTL_S if ttl_s is None else ttl_s)))
        return code

    def redeem_claim(self, code: str) -> str | None:
        """The seat this code was for, once. `None` if it is wrong or stale."""
        import time

        want = self.setting("claim.code")
        seat = self.setting("claim.seat")
        until = self.setting("claim.expires")
        if not want or not seat:
            return None
        if not secrets.compare_digest(str(code).strip().lower(), want):
            return None
        if until and time.time() > float(until):
            self.drop_claim()
            return None
        self.drop_claim()
        return seat

    def drop_claim(self) -> None:
        for key in ("claim.code", "claim.seat", "claim.expires"):
            self.set_setting(key, None)

    def bind_identity(self, seat: str, user_id: str) -> None:
        """Bind a chat account to a seat, so identity is not a name in a message."""
        if not self.is_human(seat):
            raise NotAuthorised(f"{seat!r} is not a human seat")
        with self.tx() as c:
            c.execute("UPDATE agents SET tg_user_id = NULL WHERE tg_user_id = ?",
                      (str(user_id),))
            c.execute("UPDATE agents SET tg_user_id = ? WHERE name = ?",
                      (str(user_id), seat))

    def seat_for_identity(self, user_id: str) -> str | None:
        """The seat this account has proved it holds, in any room or none."""
        row = self.q1("SELECT name FROM agents WHERE tg_user_id = ?", (str(user_id),))
        return row["name"] if row else None

    def seat_for_user(self, user_id: str, channel: str = "telegram") -> str | None:
        """The seat this account already holds, in any room.

        Pairing is per room on purpose -- being trusted in one council is not
        being trusted in another. This is the one question that is about the
        person rather than the room, and it exists so the operator does not have
        to bootstrap themselves from a terminal every time they open a group.
        """
        bound = self.seat_for_identity(user_id)
        if bound:
            return bound
        row = self.q1(
            "SELECT seat FROM pairings WHERE channel = ? AND user_id = ? "
            "AND status = 'approved' AND seat IS NOT NULL LIMIT 1",
            (channel, str(user_id)))
        return row["seat"] if row else None

    def pair_request(self, chat_id: str, user_id: str, display: str = "",
                     channel: str = "telegram") -> int:
        """Record an unknown sender. Inert until somebody approves them."""
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO pairings (channel, chat_id, user_id, display, ref) "
                "VALUES (?, ?, ?, ?, ?)",
                (channel, str(chat_id), str(user_id), display, new_handle()))
        row = self.pairing(chat_id, user_id, channel)
        return int(row["id"])

    def pair_approve(self, pairing_id: int, seat: str, by: str) -> sqlite3.Row:
        """Bind a chat identity to a seat.

        The seat must be a human one. Approving a chat user onto an agent seat
        would let a person speak as an agent, and worse, would put a non-human
        name on something only a human may do.
        """
        row = self.q1("SELECT * FROM pairings WHERE id = ?", (pairing_id,))
        if row is None:
            raise StoreError(f"no pairing request {pairing_id}")
        if not self.is_human(seat):
            raise NotAuthorised(
                f"`{seat}` is not a human seat; pairing a person onto an agent "
                f"seat would let them act as one")
        with self.tx() as c:
            c.execute("UPDATE pairings SET seat = ?, status = 'approved' WHERE id = ?",
                      (seat, pairing_id))
            self._emit(c, None, "pairing", by,
                       {"pairing_id": pairing_id, "seat": seat, "action": "approved"})
        return self.q1("SELECT * FROM pairings WHERE id = ?", (pairing_id,))

    def seat_name_for(self, display: str, fallback: str = "guest") -> str:
        """A human seat named after a person, created if it is not there.

        Approving somebody should not also require inventing a name for them.
        Their own display name is the name they already answer to.
        """
        # First name, unless that is too short to be a name -- "A Colleague"
        # gave the seat `A`, which is nobody. Then the whole thing, joined.
        # `\w` under Unicode, not `[A-Za-z]`: a name in a non-Latin script is
        # still a name, and `slugify` above already keeps one for that reason.
        # An ASCII-only filter turns such a name into `guest`.
        words = [re.sub(r"[^\w-]+", "", w, flags=re.UNICODE)
                 for w in (display or "").split()]
        words = [w for w in words if w]
        base = ""
        if words:
            base = words[0] if len(words[0]) >= 3 else "".join(words)
        base = base or fallback
        name, n = base, 2
        while True:
            # Case-insensitively: `Santa` beside an agent called `santa` is two
            # seats one letter apart, and an @mention could mean either.
            existing = self.q1("SELECT * FROM agents WHERE LOWER(name) = LOWER(?)",
                               (name,))
            if existing is None:
                self.add_agent(name, "human", display=display or name)
                return name
            if existing["kind"] == "human":
                return existing["name"]
            name, n = f"{base}{n}", n + 1

    def pair_deny(self, pairing_id: int, by: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE pairings SET status = 'denied' WHERE id = ?", (pairing_id,))
            self._emit(c, None, "pairing", by,
                       {"pairing_id": pairing_id, "action": "denied"})

    def pairings(self, status: str | None = None, chat_id: str | None = None,
                 channel: str = "telegram") -> list[sqlite3.Row]:
        """Pairings, optionally for one room.

        Listing every room's pending requests in a chat told whoever asked that
        other rooms exist and who is trying to get into them.
        """
        sql, args = "SELECT * FROM pairings WHERE 1=1", []
        if status:
            sql, args = sql + " AND status = ?", args + [status]
        if chat_id is not None:
            sql = sql + " AND channel = ? AND chat_id = ?"
            args = args + [channel, str(chat_id)]
        return self.q(sql + " ORDER BY id", args)

    def pairing_by_ref(self, ref: str, chat_id: str | None = None,
                       channel: str = "telegram") -> sqlite3.Row | None:
        """A request by the handle a person types, in this room only.

        `chat_id` is not optional in a chat: approving is a decision about who
        joins *this* council, and a request from another room is not this room's
        to answer.
        """
        # The number is accepted only from a terminal, where the operator can see
        # the whole board anyway. Inside a room it would leave the sequence
        # guessable, which is the thing the handle was added to stop.
        if chat_id is None:
            return self.q1(
                "SELECT * FROM pairings WHERE ref = ? OR CAST(id AS TEXT) = ? LIMIT 1",
                [str(ref).strip().lower(), str(ref).strip()])
        return self.q1(
            "SELECT * FROM pairings WHERE ref = ? AND channel = ? AND chat_id = ? "
            "LIMIT 1", [str(ref).strip().lower(), channel, str(chat_id)])

    def seat_for_chat(self, chat_id: str, user_id: str,
                      channel: str = "telegram") -> str | None:
        """The seat this person may speak as here, or None if they may not."""
        row = self.pairing(chat_id, user_id, channel)
        if row is None or row["status"] != "approved" or not row["seat"]:
            return None
        return row["seat"]

    # -------------------------------------------------------------- attachments

    #: Inlined into the prompt beyond this and a seat's own turn gets crowded
    #: out by its source material. The rest stays on disk with its path given.
    ATTACHMENT_INLINE = 6_000

    def attach(self, topic_id: int, src: Path | str, by: str, note: str = "",
               name: str | None = None) -> int:
        """Copy a file next to the board and record it against a topic."""
        src = Path(src).expanduser()
        if not src.is_file():
            raise StoreError(f"no such file: {src}")
        name = name or src.name
        dest_dir = self.path.parent / "attachments" / self.topic(topic_id)["slug"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        n = 2
        while dest.exists() and dest.read_bytes() != src.read_bytes():
            dest = dest_dir / f"{src.stem}-{n}{src.suffix}"
            n += 1
        shutil.copy2(src, dest)

        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO attachments (topic_id, name, path, bytes, is_text, "
                "note, added_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (topic_id, dest.name, str(dest.resolve()), dest.stat().st_size,
                 int(looks_like_text(dest)), note, by))
            aid = int(cur.lastrowid)
            self._emit(c, topic_id, "attachment", by,
                       {"attachment_id": aid, "name": dest.name})
        return aid

    def attachments(self, topic_id: int) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM attachments WHERE topic_id = ? ORDER BY id",
                      (topic_id,))

    def detach(self, attachment_id: int, by: str) -> str:
        """Forget an attachment. The copy on disk is left alone -- deleting a
        file somebody handed us is not this function's business."""
        row = self.q1("SELECT * FROM attachments WHERE id = ?", (attachment_id,))
        if row is None:
            raise StoreError(f"no attachment {attachment_id}")
        with self.tx() as c:
            c.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
            self._emit(c, int(row["topic_id"]), "attachment", by,
                       {"attachment_id": attachment_id, "action": "removed"})
        return row["name"]

    def rename_topic(self, topic_id: int, title: str, actor: str) -> str:
        """Give a topic a better name, and return the handle it now answers to.

        The slug follows the title only when it was derived from it in the first
        place. Somebody who chose a handle deliberately keeps it -- re-deriving
        would silently break the thing they type to get here.
        """
        row = self.topic(topic_id)
        title = title.strip()
        if not title:
            raise StoreError("a topic needs a title")
        slug = row["slug"]
        if slug == slugify(row["title"]):
            taken = [t["slug"] for t in self.topics() if t["id"] != topic_id]
            slug = slugify(title, taken)
        with self.tx() as c:
            c.execute("UPDATE topics SET title = ?, slug = ? WHERE id = ?",
                      (title, slug, topic_id))
            self._emit(c, topic_id, "topic", actor, {"action": "renamed", "title": title})
        return slug

    def set_brief(self, topic_id: int, brief: str, actor: str) -> None:
        """Rewrite a topic's agenda. Recorded, because what the council was asked
        to settle is part of the record -- changing it mid-meeting matters."""
        with self.tx() as c:
            c.execute("UPDATE topics SET brief = ? WHERE id = ?", (brief, topic_id))
            self._emit(c, topic_id, "topic", actor, {"action": "agenda"})

    # -------------------------------------------------------------------- seats

    def seat(self, topic_id: int, agent: str) -> sqlite3.Row | None:
        return self.q1("SELECT * FROM seats WHERE topic_id = ? AND agent = ?", (topic_id, agent))

    def seats(self, topic_id: int) -> list[sqlite3.Row]:
        return self.q(
            """SELECT s.*, a.kind, a.driver, a.driver_cfg, a.enabled
               FROM seats s JOIN agents a ON a.name = s.agent
               WHERE s.topic_id = ? ORDER BY s.agent""",
            (topic_id,),
        )

    def set_seat_state(self, topic_id: int, agent: str, state: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE seats SET state = ? WHERE topic_id = ? AND agent = ?",
                      (state, topic_id, agent))

    def set_cli_session(self, topic_id: int, agent: str, cli_session: str) -> None:
        """Persist the CLI's own session identifier so resume is deterministic.

        Claude and Gemini accept a UUID we choose; Codex and Copilot hand one back.
        Either way it lands here, and the supervisor never falls back to
        --last/--continue, which would race across topics sharing one CLI."""
        with self.tx() as c:
            c.execute("UPDATE seats SET cli_session = ? WHERE topic_id = ? AND agent = ?",
                      (cli_session, topic_id, agent))

    def advance_cursor(self, topic_id: int, agent: str, cursor: int) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE seats SET last_seen = MAX(last_seen, ?) WHERE topic_id = ? AND agent = ?",
                (cursor, topic_id, agent),
            )

    def new_session_id(self) -> str:
        return str(uuid.uuid4())

    # ----------------------------------------------------------------- messages

    def post(
        self,
        topic_id: int,
        author: str,
        body: str,
        *,
        kind: str = "say",
        reply_to: int | None = None,
        proposal_id: int | None = None,
        count_turn: bool = True,
        mention_targets: Iterable[str] | None = None,
    ) -> int:
        """Append to the record. Returns the message id.

        `count_turn` is False for system notes and for human interjections -- a
        human joining the discussion must never consume an agent's metered turns.
        """
        body = clean_text(body, "the message")
        topic = self.topic(topic_id)
        if topic["status"] not in {"open", "paused"}:
            raise StoreError(f"topic {topic['slug']} is {topic['status']}; not accepting posts")
        # Identity is bound when a CLI's MCP server is launched, and for the CLIs
        # that cannot take it per-run it comes from a global registration. Get
        # that wrong and a seat posts under another seat's name -- which happened:
        # a seat named Gravity running agy posted as `agy`, and the supervisor
        # then reported Gravity as having said nothing.
        #
        # Refusing the post turns a silent mis-attribution into an error at the
        # moment it happens. `mooting` is the board itself and holds no seat.
        if author != "mooting" and self.seat(topic_id, author) is None:
            raise StoreError(
                f"{author!r} holds no seat on `{topic['slug']}`, so this post would "
                f"be attributed to the wrong councillor. If this seat runs codex, "
                f"gemini or agy, its MCP server needs registering under its own "
                f"name: mooting install {author}")
        with self.tx() as c:
            cur = c.execute(
                """INSERT INTO messages (topic_id, author, kind, body, reply_to, proposal_id)
                   VALUES (?,?,?,?,?,?)""",
                (topic_id, author, kind, body, reply_to, proposal_id),
            )
            msg_id = int(cur.lastrowid)
            if count_turn:
                c.execute(
                    "UPDATE seats SET turns_used = turns_used + 1 WHERE topic_id = ? AND agent = ?",
                    (topic_id, author),
                )
            # Posting discharges every question outstanding against you. Answering
            # in the body of a normal reply is how people actually respond, so
            # requiring a special "answer" call would leave stale asks forever.
            c.execute(
                """UPDATE mentions SET answered_by = ?
                   WHERE topic_id = ? AND target = ? AND answered_by IS NULL""",
                (msg_id, topic_id, author),
            )
            # System notes quote what people said -- a pause reason repeats the very
            # question that caused it. Parsing @names out of that would ping the
            # person a second time on the board's own behalf, so scanning is
            # skipped unless a target was named explicitly.
            mentioned = (
                self._record_mentions(c, topic_id, msg_id, author, body, mention_targets, body)
                if kind != "system" or mention_targets
                else []
            )

            # The body lives in another table, so the chain would not notice it
            # being rewritten. Its digest travels in the event that announces it.
            ev = self._emit(c, topic_id, "message", author,
                            {"message_id": msg_id, "kind": kind, "preview": body[:280],
                             "digest": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                             "mentions": mentioned})
            # The author has by definition seen everything up to its own message.
            c.execute("UPDATE seats SET last_seen = MAX(last_seen, ?) WHERE topic_id = ? AND agent = ?",
                      (ev, topic_id, author))
        return msg_id

    # ----------------------------------------------------------------- mentions

    def _record_mentions(
        self,
        conn: sqlite3.Connection,
        topic_id: int,
        message_id: int,
        asker: str,
        body: str,
        explicit: Iterable[str] | None,
        question: str,
    ) -> list[str]:
        """Turn `@name` into a directed ask.

        Only seats on this topic count. An `@` naming someone who is not seated is
        left as plain text rather than silently pulling a stranger into the debate
        -- adding a seat costs money on someone's subscription, so it stays a
        human's decision.
        """
        seated = {s["agent"] for s in self.q(
            "SELECT agent FROM seats WHERE topic_id = ?", (topic_id,))}
        # An explicit target came through `ask`; the rest were found by reading
        # the prose. Only the first is a question somebody is waiting on -- a
        # seat writing "final takeaway for @you" is addressing you, not asking,
        # and used to stop the council all the same.
        asked = {t for t in (explicit or []) if t in seated}
        named = {m for m in MENTION_RE.findall(body) if m in seated}
        targets = asked | named
        targets.discard(asker)                      # @-ing yourself is a no-op
        for target in sorted(targets):
            conn.execute(
                """INSERT INTO mentions
                       (topic_id, message_id, asker, target, question, asking)
                   VALUES (?,?,?,?,?,?)""",
                (topic_id, message_id, asker, target, question[:2000],
                 1 if target in asked else 0),
            )
        return sorted(targets)

    def open_mentions(self, topic_id: int, target: str | None = None, *,
                      only_asks: bool = False) -> list[sqlite3.Row]:
        """Outstanding mentions. `only_asks` keeps just the ones somebody is
        actually waiting on, which is what may stop a council."""
        sql = "SELECT * FROM mentions WHERE topic_id = ? AND answered_by IS NULL"
        if only_asks:
            sql += " AND asking = 1"
        args: list[Any] = [topic_id]
        if target:
            sql += " AND target = ?"
            args.append(target)
        return self.q(sql + " ORDER BY id", args)

    def quoted(self, message_id: int):
        """The message a reply is attached to, if any."""
        return self.q1("SELECT * FROM messages WHERE id = ?", (message_id,))

    def ask(self, topic_id: int, asker: str, target: str, question: str) -> int:
        """Explicit @: post the question and direct it at one seat."""
        if not self.seat(topic_id, target):
            raise StoreError(f"{target!r} holds no seat on this topic")
        return self.post(
            topic_id, asker, f"@{target} {question}",
            count_turn=not self.is_human(asker),
            mention_targets=[target],
        )

    def transcript(self, topic_id: int, after: int = 0, limit: int | None = 500,
                   newest: bool = False) -> list[sqlite3.Row]:
        """Messages after `after`, oldest first. `limit=None` for all of them.

        `newest=True` takes the last `limit` rather than the first. Callers that
        wanted the tail used to slice the returned list, which is the *oldest*
        500 messages of the topic -- so once a council passed 500 messages every
        one of them was showing the tail of the wrong window.
        """
        cap = -1 if limit is None else limit
        if newest:
            rows = self.q(
                "SELECT * FROM messages WHERE topic_id = ? AND id > ? ORDER BY id DESC LIMIT ?",
                (topic_id, after, cap),
            )
            return list(reversed(rows))
        return self.q(
            "SELECT * FROM messages WHERE topic_id = ? AND id > ? ORDER BY id LIMIT ?",
            (topic_id, after, cap),
        )

    def messages_by_id(self, ids: Iterable[int | None]) -> list[sqlite3.Row]:
        """The named messages, oldest first.

        Resolving ids by scanning a `transcript` window meant a message outside
        that window silently had no body, which is how a seat came to be handed
        an event it could not read.
        """
        wanted = sorted({int(i) for i in ids if i is not None})
        if not wanted:
            return []
        marks = ",".join("?" * len(wanted))
        return self.q(f"SELECT * FROM messages WHERE id IN ({marks}) ORDER BY id", wanted)

    def message_count(self, topic_id: int) -> int:
        """How many messages a topic holds. Counting a `transcript` stopped at 500."""
        return int(self.q1("SELECT COUNT(*) c FROM messages WHERE topic_id = ?",
                           (topic_id,))["c"])

    # ---------------------------------------------------------------- proposals

    def propose(self, topic_id: int, author: str, title: str, body: str) -> int:
        title, body = clean_text(title, "the title"), clean_text(body, "the body")
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO messages (topic_id, author, kind, body) VALUES (?,?,'propose',?)",
                (topic_id, author, f"{title}\n\n{body}"),
            )
            msg_id = int(cur.lastrowid)
            cur = c.execute(
                """INSERT INTO proposals (topic_id, message_id, author, title, body)
                   VALUES (?,?,?,?,?)""",
                (topic_id, msg_id, author, title, body),
            )
            pid = int(cur.lastrowid)
            c.execute("UPDATE messages SET proposal_id = ? WHERE id = ?", (pid, msg_id))
            c.execute("UPDATE seats SET turns_used = turns_used + 1 WHERE topic_id = ? AND agent = ?",
                      (topic_id, author))
            self._emit(c, topic_id, "proposal", author,
                       {"proposal_id": pid, "title": title, "action": "opened"})
        return pid

    def proposal(self, pid: int) -> sqlite3.Row:
        row = self.q1("SELECT * FROM proposals WHERE id = ?", (pid,))
        if row is None:
            raise StoreError(f"no such proposal: {pid}")
        return row

    def proposals(self, topic_id: int | None = None, status: str | None = None) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM proposals WHERE 1=1", []
        if topic_id is not None:
            sql += " AND topic_id = ?"; args.append(topic_id)
        if status is not None:
            sql += " AND status = ?"; args.append(status)
        return self.q(sql + " ORDER BY id DESC", args)

    def vote(self, pid: int, agent: str, stance: str, rationale: str = "") -> None:
        if stance not in {"support", "object", "abstain"}:
            raise StoreError(f"bad stance {stance!r}")
        p = self.proposal(pid)
        if p["status"] != "open":
            raise StoreError(f"proposal {pid} is {p['status']}; voting closed")
        with self.tx() as c:
            c.execute(
                """INSERT INTO votes (proposal_id, agent, stance, rationale) VALUES (?,?,?,?)
                   ON CONFLICT(proposal_id, agent) DO UPDATE SET
                     stance=excluded.stance, rationale=excluded.rationale""",
                (pid, agent, stance, rationale),
            )
            c.execute(
                "INSERT INTO messages (topic_id, author, kind, body, proposal_id) VALUES (?,?,?,?,?)",
                # An abstention used to be written down as support, which is the
                # opposite of what the seat said.
                (p["topic_id"], agent,
                 {"object": "object", "abstain": "system"}.get(stance, "support"),
                 rationale, pid),
            )
            self._emit(c, p["topic_id"], "proposal", agent,
                       {"proposal_id": pid, "action": "vote", "stance": stance})

    def votes(self, pid: int) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM votes WHERE proposal_id = ? ORDER BY agent", (pid,))

    def executing_now(self) -> list[sqlite3.Row]:
        """Seats holding a shell on this machine right now.

        A seat woken for an approved task runs with its restrictions dropped so
        it can do the work, which means a shell. That is the whole window this
        project has: while it is open, a command typed on this machine cannot be
        told apart from one that seat typed.
        """
        return self.q(
            "SELECT k.id, k.title, k.assignee, t.slug FROM tasks k "
            "JOIN topics t ON t.id = k.topic_id WHERE k.status = 'in_progress'")

    def decide(self, pid: int, decider: str, approve: bool, rationale: str = "",
               via: str = "local") -> None:
        """Close a proposal. Humans only -- this is the whole point of the platform.

        Agents deliberate and vote; votes are advisory. The transition that lets a
        proposal become action is reserved to a human seat, and it is checked here
        rather than asked for in a prompt, because instructions are advice and this
        is the one place that needs a fence.
        """
        if not self.is_human(decider):
            raise NotAuthorised(
                f"{decider!r} is not a human seat; only a human closes a proposal. "
                "Agents may vote (support/object) -- votes are advisory."
            )
        # The two-key rule says a seat edits files only when a person approved
        # the task and it was woken for one. It says nothing about the shell that
        # comes with the editing, and `mooting approve 3` is one command away
        # from there -- identity here is a name, and a seat running as the same
        # user can pass any name. A decision typed on this machine while that is
        # true cannot be attributed, so it is refused rather than trusted.
        #
        # Not a fence around the machine, which is not this project's to build.
        # It closes the one window this project opens itself, and it names the
        # way out: a chat account is something the seat does not hold.
        # Anything reachable from this machine, which is HTTP as well as the
        # terminal: a seat with a shell can read a token off the board and make
        # the same request. Only a chat is genuinely elsewhere -- the bot token
        # lets a seat speak *as the bot*, not as a paired person, and the sender
        # is what the decide path checks.
        running = [] if via in ON_ANOTHER_MACHINE else self.executing_now()
        if running:
            work = running[0]
            raise NotAuthorised(
                f"{work['assignee']} is executing task #{work['id']} "
                f"({work['title']!r}) and holds a shell on this machine, so a "
                f"sign-off typed here cannot be told from one it typed. Sign off "
                f"from a chat, or wait for the task to finish.")
        p = self.proposal(pid)
        # Anybody may call a meeting and argue in it; one person closes it. Without
        # this, approving somebody into a room handed them the same authority as
        # the person who opened it, and "a human decided" stopped identifying who.
        seated = self.chair(int(p["topic_id"]))
        if seated and decider != seated:
            raise NotAuthorised(
                f"{decider!r} is not the chair of this meeting — {seated} signs off "
                f"here. Anybody may argue; one person closes it.")
        if p["status"] != "open":
            raise StoreError(f"proposal {pid} already {p['status']}")
        status = "approved" if approve else "rejected"
        with self.tx() as c:
            c.execute(
                """UPDATE proposals SET status = ?, decided_by = ?, rationale = ?,
                   decided_at = datetime('now') WHERE id = ?""",
                (status, decider, rationale, pid),
            )
            c.execute(
                "INSERT INTO messages (topic_id, author, kind, body, proposal_id) VALUES (?,?,'ruling',?,?)",
                (p["topic_id"], decider, f"[{status}] {rationale}".strip(), pid),
            )
            self._emit(c, p["topic_id"], "decision", decider,
                       {"proposal_id": pid, "status": status, "rationale": rationale})
            if approve:
                # In the same transaction as the ruling. Split across two, a crash
                # in between left a proposal approved with its tasks stuck as
                # drafts -- which the work loop would then put up as a second,
                # disconnected plan.
                released = c.execute(
                    "UPDATE tasks SET status = 'assigned', updated_at = datetime('now') "
                    "WHERE proposal_id = ? AND status = 'draft'", (pid,)).rowcount
            else:
                released = 0
        if released:
            self.post(int(p["topic_id"]), "mooting",
                      f"plan approved - {released} task(s) assigned",
                      kind="system", count_turn=False)

    def delete_topic(self, topic_id: int) -> dict[str, int]:
        """Remove a topic and everything hanging off it.

        Returns what was removed, because a delete that reports nothing leaves
        you unsure whether it did anything. `wakes` is cleared by hand -- it
        carries a topic_id but no foreign key, so the cascade does not reach it.
        """
        counts = {
            "messages": self.message_count(topic_id),
            "tasks": len(self.tasks(topic_id)),
            "proposals": len(self.proposals(topic_id)),
        }
        with self.tx() as c:
            c.execute("DELETE FROM wakes WHERE topic_id = ?", (topic_id,))
            c.execute("DELETE FROM topics WHERE id = ?", (topic_id,))
        return counts

    def orphan_worktrees(self, topic_id: int) -> list[str]:
        """Task worktrees that deleting this topic would leave behind on disk.

        Deleting the row does not delete the checkout, and silently orphaning a
        directory full of someone's work would be a poor trade for tidiness.
        """
        return [t["worktree"] for t in self.tasks(topic_id)
                if t["worktree"] and t["branch"]]

    def clear_topics(self) -> int:
        """Every topic and its contents. Seats registry survives."""
        ids = [int(t["id"]) for t in self.topics()]
        for tid in ids:
            self.delete_topic(tid)
        return len(ids)

    # -------------------------------------------------------------------- tasks

    def is_manager(self, topic_id: int, agent: str) -> bool:
        row = self.seat(topic_id, agent)
        return bool(row) and row["role"] == "manager"

    def draft_task(self, topic_id: int, manager: str, assignee: str, title: str,
                   body: str = "", acceptance: str = "",
                   depends_on: int | None = None) -> int:
        """A manager writes a task. It is a *draft*: nothing runs until a human
        approves the plan. Assign-rights are checked here rather than asked for in
        a prompt, for the same reason `decide` checks humanity -- an instruction is
        advice, and this is a place that needs a fence."""
        if self.topic(topic_id)["mode"] != "work":
            raise StoreError("tasks only exist on a work topic")
        if not self.is_manager(topic_id, manager):
            raise NotAuthorised(f"{manager!r} is not the manager of this topic")
        if not self.seat(topic_id, assignee):
            raise StoreError(f"{assignee!r} holds no seat on this topic")
        with self.tx() as c:
            cur = c.execute(
                """INSERT INTO tasks (topic_id, title, body, acceptance, assignee,
                                        created_by, depends_on)
                   VALUES (?,?,?,?,?,?,?)""",
                (topic_id, title, body, acceptance, assignee, manager, depends_on),
            )
            tid = int(cur.lastrowid)
            self._emit(c, topic_id, "task", manager,
                       {"task_id": tid, "action": "drafted", "assignee": assignee})
        return tid

    def tasks(self, topic_id: int, status: str | None = None,
              assignee: str | None = None) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM tasks WHERE topic_id = ?", [topic_id]
        if status:
            sql += " AND status = ?"
            args.append(status)
        if assignee:
            sql += " AND assignee = ?"
            args.append(assignee)
        return self.q(sql + " ORDER BY id", args)

    def task(self, task_id: int) -> sqlite3.Row:
        row = self.q1("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise StoreError(f"no such task: {task_id}")
        return row

    def submit_plan(self, topic_id: int, manager: str) -> int:
        """Put every draft task to the human as one proposal.

        One proposal for the whole plan, not one per task: the human is approving
        *who does what*, and approving piecemeal would let work start on half a
        plan while the other half is still being argued about.
        """
        drafts = self.tasks(topic_id, status="draft")
        if not drafts:
            raise StoreError("no draft tasks to put to a human")
        lines = []
        for t in drafts:
            lines.append(f"- **{t['assignee']}** - {t['title']}")
            if t["acceptance"]:
                lines.append(f"  done when: {t['acceptance']}")
        pid = self.propose(topic_id, manager, f"Work plan: {len(drafts)} task(s)",
                           "\n".join(lines))
        with self.tx() as c:
            c.execute("UPDATE tasks SET proposal_id = ? WHERE topic_id = ? AND status = 'draft'",
                      (pid, topic_id))
        return pid

    def update_task(self, task_id: int, agent: str, status: str, result: str = "") -> None:
        """A worker reports on its own task; a manager rules on a finished one."""
        t = self.task(task_id)
        worker_states = {"in_progress", "done", "blocked"}
        # `assigned` puts it back in the queue. Without it the verdict a manager
        # most needs after a blocked task whose cause has been fixed -- do it
        # again -- was the one thing the state machine could not say, and using
        # `rejected` for it dropped the task out of every code path: neither
        # `_runnable_tasks` nor the review step reads that state.
        manager_states = {"accepted", "rejected", "assigned"}
        if status not in worker_states | manager_states:
            raise StoreError(f"bad task status {status!r}")
        if status in worker_states and agent != t["assignee"]:
            raise NotAuthorised(f"{agent!r} is not the assignee of task {task_id}")
        # The chair as well as the manager, because a work topic managed by an
        # agent otherwise had no route for the person who signed off the plan to
        # accept what came back.
        if status in manager_states and not (
                self.is_manager(int(t["topic_id"]), agent)
                or agent == self.chair(int(t["topic_id"]))):
            raise NotAuthorised(
                f"only the manager or the chair accepts or rejects task {task_id}")
        if t["status"] == "draft":
            raise StoreError(f"task {task_id} is still a draft; the plan needs approving")
        note = f"task #{task_id} [{t['title']}] -> {status}"
        if result:
            note += "\n" + result
        with self.tx() as c:
            c.execute("UPDATE tasks SET status = ?, result = COALESCE(NULLIF(?,''), result), "
                      "updated_at = datetime('now') WHERE id = ?", (status, result, task_id))
            c.execute("INSERT INTO messages (topic_id, author, kind, body) VALUES (?,?,'system',?)",
                      (t["topic_id"], agent, note))
            self._emit(c, int(t["topic_id"]), "task", agent,
                       {"task_id": task_id, "action": status})

    def set_task_workspace(self, task_id: int, branch: str, worktree: str,
                           base_sha: str = "") -> None:
        with self.tx() as c:
            c.execute("UPDATE tasks SET branch = ?, worktree = ?, base_sha = ? WHERE id = ?",
                      (branch, worktree, base_sha, task_id))

    # -------------------------------------------------------------------- wakes

    def record_wake(self, topic_id: int, agent: str) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO wakes (topic_id, agent, outcome) VALUES (?,?,'pending')",
                (topic_id, agent),
            )
            return int(cur.lastrowid)

    def finish_wake(self, wake_id: int, outcome: str, detail: str = "",
                    usage: dict | None = None) -> None:
        """Close a wake, with whatever the CLI said the turn cost.

        `usage` is what the vendor reported, not what we counted: only some CLIs
        say, so the columns stay NULL rather than zero when they do not.
        """
        u = usage or {}
        with self.tx() as c:
            c.execute(
                "UPDATE wakes SET outcome = ?, detail = ?, ended_at = datetime('now'), "
                "tokens_in = ?, tokens_out = ?, cost_usd = ? WHERE id = ?",
                (outcome, detail[:2000], u.get("tokens_in"), u.get("tokens_out"),
                 u.get("cost_usd"), wake_id),
            )

    def sweep_stale_wakes(self, older_than_s: int = 1800) -> int:
        """Close wakes that nothing is going to close.

        A killed process leaves its wake `pending` forever, and the seat panel
        reads pending as "thinking" -- so a council that was stopped an hour ago
        still looks busy. Anything older than the longest a turn could legitimately
        take is not running.
        """
        with self.tx() as c:
            cur = c.execute(
                "UPDATE wakes SET outcome = 'abandoned', ended_at = datetime('now'), "
                "detail = 'no result recorded; process gone' "
                "WHERE outcome = 'pending' "
                "AND started_at < datetime('now', ?)", (f"-{older_than_s} seconds",))
            return cur.rowcount

    def active_wakes(self, topic_id: int) -> list[sqlite3.Row]:
        """Seats currently mid-turn, for a live status line. Reads the same ledger
        the cost caps use, so it cannot disagree with what actually ran."""
        return self.q(
            """SELECT agent, started_at,
                      CAST((julianday('now') - julianday(started_at)) * 86400 AS INTEGER) AS secs
               FROM wakes WHERE topic_id = ? AND outcome = 'pending' ORDER BY id""",
            (topic_id,),
        )

    def usage(self, hours: float | None = None) -> list[dict]:
        """What each seat has actually spent, from the wake ledger.

        Wakes rather than turns, because a wake is what a metered CLI charges
        for: one that fails or produces nothing still cost a request, and
        `turns_used` counts neither. The ledger has been recording this since the
        beginning and nothing ever showed it.
        """
        window = ("AND started_at > datetime('now', ?)" if hours else "")
        args = [f"-{hours} hours"] if hours else []
        rows = self.q(
            f"""SELECT agent,
                       COUNT(*)                                   AS wakes,
                       SUM(outcome = 'ok')                        AS ok,
                       SUM(outcome NOT IN ('ok', 'pending'))      AS failed,
                       -- Integer epoch, not julianday: the float arithmetic
                       -- lost seconds and reported 43 for 45.
                       SUM(CAST(strftime('%s', ended_at) AS INTEGER)
                           - CAST(strftime('%s', started_at) AS INTEGER))
                                                                  AS seconds,
                       SUM(tokens_in)                             AS tokens_in,
                       SUM(tokens_out)                            AS tokens_out,
                       SUM(cost_usd)                              AS cost_usd,
                       MAX(started_at)                            AS last
                FROM wakes WHERE 1=1 {window} GROUP BY agent
                ORDER BY wakes DESC""", args)
        return [{"agent": r["agent"], "wakes": r["wakes"], "ok": r["ok"] or 0,
                 "failed": r["failed"] or 0, "seconds": r["seconds"] or 0,
                 "tokens_in": r["tokens_in"], "tokens_out": r["tokens_out"],
                 "cost_usd": r["cost_usd"], "last": r["last"]} for r in rows]

    def wakes_in_last_hour(self, agent: str) -> int:
        """Metered CLIs charge for a failed wake too, so this counts attempts,
        not successes."""
        row = self.q1(
            "SELECT COUNT(*) AS n FROM wakes WHERE agent = ? AND started_at > datetime('now','-1 hour')",
            (agent,),
        )
        return int(row["n"]) if row else 0


def connect(path: Path | str | None = None, *, init: bool = False) -> Store:
    """Open the board, creating or migrating it as needed.

    `init_schema` runs every time, not only for a new file. It is idempotent
    (CREATE TABLE IF NOT EXISTS plus guarded ALTERs), and running it only on
    creation is what makes a migration silently never happen: the column is added
    to schema.sql, every existing board keeps working without it, and the failure
    surfaces later as `table topics has no column named ...`.
    """
    target = Path(path) if path else default_db_path()
    if not init and not target.exists():
        # Silently creating a board turned a wrong --db or a wrong working
        # directory into "nothing set up yet", which reads like a fresh install
        # rather than a mistake.
        if path is not None or os.environ.get("MOOTING_DB"):
            # A path somebody named and got wrong. The working directory is not
            # the story, and naming it just sends them looking in the wrong place.
            raise StoreError(f"no board at {target} — you asked for that path")
        raise StoreError(
            f"no board yet for {Path.cwd()}\n"
            f"  it would live at {target}\n"
            f"  `mooting tui` opens one; `mooting init` creates it without opening")
    store = Store(path)
    store.init_schema()
    return store


def _proposal_event_id(store: Store, pid: int) -> int:
    """Event id at which a proposal became visible to the council.

    Seats track an event cursor, so "has this seat had a chance to respond to
    proposal N" is a cursor comparison -- which works whether or not the agent
    cooperated by calling the vote tool.
    """
    row = store.q1(
        """SELECT id FROM events
           WHERE kind = 'proposal' AND json_extract(payload, '$.proposal_id') = ?
             AND json_extract(payload, '$.action') = 'opened'
           ORDER BY id LIMIT 1""",
        (pid,),
    )
    return int(row["id"]) if row else 0


Store.proposal_event_id = lambda self, pid: _proposal_event_id(self, pid)
