"""SQLite-backed council board.

Every process in Moot -- the MCP server each agent CLI spawns for itself, the
supervisor, the human UI -- talks to one file through this module. There is no
server-of-record and no daemon requirement: if nothing else is running, the board
is still readable and writable, which is what lets a failed wake degrade to
catch-up-on-next-turn instead of deadlocking a topic.

Encoding: this runs on a machine whose console codepage is cp950 and whose
council will carry Chinese text from day one. Every text boundary in this project
is pinned to UTF-8 explicitly. Mojibake at a protocol boundary reads as protocol
corruption and costs a day to diagnose.
"""

from __future__ import annotations

import json
import os
import re
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

    Non-alphanumerics become separators and everything else is kept, so a Chinese
    title keeps its characters instead of slugifying to nothing.
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


class StoreError(RuntimeError):
    pass


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


def default_db_path() -> Path:
    """Board location: the env override, then `.moot/`, then a legacy `.agora/`.

    The project was called agora first. Someone with a board already on disk
    should not lose their councils to a rename, so an existing `.agora/board.db`
    is still opened when there is no `.moot/` one.
    """
    env = os.environ.get("MOOT_DB")
    if env:
        return Path(env)
    here = Path.cwd() / ".moot" / "board.db"
    legacy = Path.cwd() / ".agora" / "board.db"
    if not here.exists() and legacy.exists():
        return legacy
    return here


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
        for table, column, ddl in (("topics", "mode", "TEXT NOT NULL DEFAULT 'debate'"),
                                   ("topics", "effort", "TEXT"),
                                   ("tasks", "base_sha", "TEXT")):
            cols = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    # ------------------------------------------------------------------- events

    def _emit(
        self,
        conn: sqlite3.Connection,
        topic_id: int | None,
        kind: str,
        actor: str,
        payload: dict[str, Any] | None = None,
    ) -> int:
        cur = conn.execute(
            "INSERT INTO events (topic_id, kind, actor, payload) VALUES (?,?,?,?)",
            (topic_id, kind, actor, json.dumps(payload or {}, ensure_ascii=False)),
        )
        return int(cur.lastrowid)

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
        ("topics", "opened_by"),
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
        with self.tx() as c:
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
        if mode not in TOPIC_MODES:
            raise StoreError(f"unknown mode {mode!r}; expected one of {sorted(TOPIC_MODES)}")
        with self.tx() as c:
            cur = c.execute(
                """INSERT INTO topics (slug, title, brief, opened_by, max_rounds, mode, effort)
                   VALUES (?,?,?,?,?,?,?)""",
                (slug, title, brief, opened_by, max_rounds, mode, effort),
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

    def grant_rounds(self, topic_id: int, n: int) -> None:
        """More rounds, and the per-seat turns to use them.

        Raising one without the other is the trap: a seat that has spent its turns
        stays capped however many rounds you add, so the council looks alive and
        says nothing.
        """
        with self.tx() as c:
            c.execute("UPDATE topics SET max_rounds = max_rounds + ? WHERE id = ?",
                      (n, topic_id))
            c.execute("UPDATE seats SET max_turns = max_turns + ? WHERE topic_id = ?",
                      (n, topic_id))

    def conclude(self, topic_id: int, by: str, note: str = "") -> int:
        """Close the meeting, on the record.

        A meeting that just stops is not the same as one that concluded, and the
        difference matters later: minutes of an abandoned discussion read exactly
        like minutes of a settled one unless somebody said which it was.

        Reserved to a human for the same reason a ruling is: an agent deciding the
        meeting is over would be deciding the outcome.
        """
        if not self.is_human(by):
            raise NotAuthorised(f"{by!r} is not a human seat; only a human closes a meeting")
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
        with self.tx() as c:
            closed = "datetime('now')" if status in {"resolved", "aborted"} else "NULL"
            c.execute(f"UPDATE topics SET status = ?, closed_at = {closed} WHERE id = ?", (status, topic_id))
            self._emit(c, topic_id, "topic", actor, {"action": status, "note": note})

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
        # moment it happens. `moot` is the board itself and holds no seat.
        if author != "moot" and self.seat(topic_id, author) is None:
            raise StoreError(
                f"{author!r} holds no seat on `{topic['slug']}`, so this post would "
                f"be attributed to the wrong councillor. If this seat runs codex, "
                f"gemini or agy, its MCP server needs registering under its own "
                f"name: moot install {author}")
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

            ev = self._emit(c, topic_id, "message", author,
                            {"message_id": msg_id, "kind": kind, "preview": body[:280],
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
        targets = {t for t in (explicit or []) if t in seated}
        targets |= {m for m in MENTION_RE.findall(body) if m in seated}
        targets.discard(asker)                      # @-ing yourself is a no-op
        for target in sorted(targets):
            conn.execute(
                """INSERT INTO mentions (topic_id, message_id, asker, target, question)
                   VALUES (?,?,?,?,?)""",
                (topic_id, message_id, asker, target, question[:2000]),
            )
        return sorted(targets)

    def open_mentions(self, topic_id: int, target: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM mentions WHERE topic_id = ? AND answered_by IS NULL"
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

    def transcript(self, topic_id: int, after: int = 0, limit: int = 500) -> list[sqlite3.Row]:
        return self.q(
            "SELECT * FROM messages WHERE topic_id = ? AND id > ? ORDER BY id LIMIT ?",
            (topic_id, after, limit),
        )

    # ---------------------------------------------------------------- proposals

    def propose(self, topic_id: int, author: str, title: str, body: str) -> int:
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

    def decide(self, pid: int, decider: str, approve: bool, rationale: str = "") -> None:
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
        p = self.proposal(pid)
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
            self.post(int(p["topic_id"]), "moot",
                      f"plan approved - {released} task(s) assigned",
                      kind="system", count_turn=False)

    def delete_topic(self, topic_id: int) -> dict[str, int]:
        """Remove a topic and everything hanging off it.

        Returns what was removed, because a delete that reports nothing leaves
        you unsure whether it did anything. `wakes` is cleared by hand -- it
        carries a topic_id but no foreign key, so the cascade does not reach it.
        """
        counts = {
            "messages": len(self.transcript(topic_id)),
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
                   body: str = "", acceptance: str = "") -> int:
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
                """INSERT INTO tasks (topic_id, title, body, acceptance, assignee, created_by)
                   VALUES (?,?,?,?,?,?)""",
                (topic_id, title, body, acceptance, assignee, manager),
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

    def release_plan(self, proposal_id: int) -> int:
        """Turn approved drafts into assigned work. Reached only from `decide`."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE tasks SET status = 'assigned', updated_at = datetime('now') "
                "WHERE proposal_id = ? AND status = 'draft'", (proposal_id,))
            return cur.rowcount

    def update_task(self, task_id: int, agent: str, status: str, result: str = "") -> None:
        """A worker reports on its own task; a manager rules on a finished one."""
        t = self.task(task_id)
        worker_states = {"in_progress", "done", "blocked"}
        manager_states = {"accepted", "rejected"}
        if status not in worker_states | manager_states:
            raise StoreError(f"bad task status {status!r}")
        if status in worker_states and agent != t["assignee"]:
            raise NotAuthorised(f"{agent!r} is not the assignee of task {task_id}")
        if status in manager_states and not self.is_manager(int(t["topic_id"]), agent):
            raise NotAuthorised(f"only the manager accepts or rejects task {task_id}")
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

    def finish_wake(self, wake_id: int, outcome: str, detail: str = "") -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE wakes SET outcome = ?, detail = ?, ended_at = datetime('now') WHERE id = ?",
                (outcome, detail[:2000], wake_id),
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
        raise StoreError(
            f"no board at {target}. Run `moot init` here, or point --db / "
            f"$MOOT_DB at an existing one.")
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
