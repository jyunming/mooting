"""A council in a Telegram chat — milestone C of docs/REMOTE.md.

The adapter itself is small, because the session is already two surfaces over
one dispatch: `Board(Console)` in the TUI swaps `emit` for a widget, and
`ChatBoard` below swaps it for a message. Everything a person can type in the
terminal works here the day the transport does.

What is *not* small is getting an agent's reply into a chat intact, and three
measured facts decide the design:

**HTML, not MarkdownV2.** MarkdownV2 requires escaping eighteen characters --
`_ * [ ] ( ) ~ ` > # + - = | { } . !` -- which includes the full stop, the
hyphen and the exclamation mark. Every sentence contains one, every bullet list
contains one, and an unescaped character does not degrade: Telegram rejects the
whole message. HTML mode escapes three (`<`, `>`, `&`) and supports `<b>`,
`<i>`, `<code>`, `<pre>` and `<blockquote>`. The failure surface is an order of
magnitude smaller.

**4096 characters per message.** A real reply on this project's own board ran
past 2000, and a concurrent three-seat round produces three at once. Splitting
happens on block boundaries, before rendering, so a chunk can never end inside a
tag.

**One message per second per chat, twenty per minute in a group.** A ten-round
council with three seats is thirty replies; posting them as they arrive hits the
group ceiling around round seven. The queue below is not an optimisation.

Nothing here can rule on a proposal yet. That arrives with the pairing checks,
not before them.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import pathlib
import re
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field

log = logging.getLogger("mooting.telegram")

#: This bot process, to the board's drive claim. The `running` table below is
#: this process only, and a board can have a bot and a console on it at once.
SESSION = f"chat-{os.getpid()}-{uuid.uuid4().hex[:6]}"

#: Telegram hard ceiling for one message.
LIMIT = 4096

#: What a bot may send. Measured from the Bot API FAQ, not guessed: exceeding
#: either of these earns a 429 and, eventually, a slower bot.
PER_CHAT_SECONDS = 1.0
PER_GROUP_MINUTE = 20


# --------------------------------------------------------------- rendering

_FENCE = re.compile(r"```([A-Za-z0-9_+-]*)\n(.*?)```", re.S)
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")

#: Terminal colour codes, which mean nothing in a chat.
ANSI = re.compile(r"\[[0-9;]*m")


def _inline(text: str) -> str:
    """Inline markdown to Telegram HTML, on already-escaped text."""
    # `code` first: whatever is inside it must not be read as emphasis.
    holes: list[str] = []

    def stash(m):
        holes.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(holes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", stash, text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"(?<![A-Za-z0-9])__(.+?)__(?![A-Za-z0-9])", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![_\w])_([^_\n]+)_(?![_\w])", r"<i>\1</i>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)
    return re.sub(r"\x00(\d+)\x00", lambda m: holes[int(m.group(1))], text)


def blocks(md: str) -> list[str]:
    """Markdown into Telegram-HTML blocks, each independently sendable.

    Blocks rather than one string because splitting happens between them: a
    chunk that ends inside `<pre>` is a message Telegram refuses, and finding
    that out in production is expensive.
    """
    out: list[str] = []
    pos = 0
    for m in _FENCE.finditer(md):
        out += _prose(md[pos:m.start()])
        lang = f' class="language-{m.group(1)}"' if m.group(1) else ""
        out.append(f"<pre><code{lang}>{html.escape(m.group(2))}</code></pre>")
        pos = m.end()
    out += _prose(md[pos:])
    return [b for b in out if b.strip()]


def _prose(md: str) -> list[str]:
    """Everything that is not a fenced block, paragraph by paragraph."""
    out: list[str] = []
    table: list[str] = []

    def flush_table():
        if table:
            # There are no tables in Telegram. Monospace keeps the columns
            # aligned, which is the part that carried the meaning.
            out.append("<pre>" + html.escape("\n".join(table)) + "</pre>")
            table.clear()

    for para in re.split(r"\n\s*\n", md):
        if not para.strip():
            continue
        lines = para.splitlines()
        if all(_TABLE_ROW.match(ln) for ln in lines if ln.strip()):
            table.extend(lines)
            flush_table()
            continue
        flush_table()

        rendered = []
        for ln in lines:
            esc = html.escape(ln)
            heading = re.match(r"^\s*(#{1,6})\s+(.*)$", esc)
            if heading:
                rendered.append(f"<b>{_inline(heading.group(2))}</b>")
                continue
            quote = re.match(r"^\s*&gt;\s?(.*)$", esc)
            if quote:
                rendered.append(f"<blockquote>{_inline(quote.group(1))}</blockquote>")
                continue
            bullet = re.match(r"^(\s*)[-*+]\s+(.*)$", esc)
            if bullet:
                rendered.append(f"{bullet.group(1)}• {_inline(bullet.group(2))}")
                continue
            rendered.append(_inline(esc))
        out.append("\n".join(rendered))
    flush_table()
    return out


def chunks(md: str, limit: int = LIMIT) -> list[str]:
    """Renderable messages, each within Telegram's ceiling.

    A block longer than the limit on its own -- a large fenced log, usually --
    is cut on line boundaries and each piece closed properly, because half a
    `<pre>` is not a message.
    """
    out: list[str] = []
    current = ""
    for block in blocks(md):
        if len(block) > limit:
            if current:
                out.append(current)
                current = ""
            out.extend(_cut(block, limit))
            continue
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit:
            out.append(current)
            current = block
        else:
            current = candidate
    if current:
        out.append(current)
    return out


def _cut(block: str, limit: int) -> list[str]:
    """Split one oversized block, keeping `<pre>` blocks closed."""
    pre = block.startswith("<pre>")
    body = block[len("<pre><code>"):-len("</code></pre>")] if pre and \
        block.startswith("<pre><code>") else (
            block[len("<pre>"):-len("</pre>")] if pre else block)
    wrap = (lambda s: f"<pre>{s}</pre>") if pre else (lambda s: s)
    room = limit - (len(wrap("")) if pre else 0)

    out, current = [], ""
    for line in body.splitlines(keepends=True):
        while len(line) > room:                 # a single monstrous line
            out.append(wrap(current + line[:room - len(current)]))
            line = line[room - len(current):]
            current = ""
        if len(current) + len(line) > room:
            out.append(wrap(current))
            current = ""
        current += line
    if current:
        out.append(wrap(current))
    return out


# ---------------------------------------------------------------- throttling

@dataclass
class Throttle:
    """Keeps a chat inside Telegram's limits.

    Not an optimisation: a council that ignores these gets 429s, and a 429 in
    the middle of a round means a seat's argument is the one that goes missing.
    """
    per_second: float = PER_CHAT_SECONDS
    per_minute: int = PER_GROUP_MINUTE
    _sent: list[float] = field(default_factory=list)
    _last: float = 0.0

    def delay(self, now: float) -> float:
        """Seconds to wait before the next send. Pure, so it can be tested."""
        wait = max(0.0, self._last + self.per_second - now)
        recent = [t for t in self._sent if now - t < 60.0]
        if len(recent) >= self.per_minute:
            wait = max(wait, 60.0 - (now - recent[0]))
        return wait

    def record(self, now: float) -> None:
        self._last = now
        self._sent = [t for t in self._sent if now - t < 60.0] + [now]

    async def wait(self) -> None:
        now = time.monotonic()
        pause = self.delay(now)
        if pause > 0:
            await asyncio.sleep(pause)
        self.record(time.monotonic())


# ------------------------------------------------------------------ the board

class ChatBoard:
    """`Console`'s command set, wired to a chat instead of a terminal.

    Built per (chat, seat) so two people in one room each act as themselves --
    every ask, ruling and turn on the board is attributed by name already, and
    this is where that pays.
    """

    def __init__(self, db, topic, me: str, room: tuple[str, str] | None = None):
        from .console import Console
        from .store import StoreError
        try:
            self.console = Console(db, topic, me, room=room)
        except StoreError:
            # Second line for the same failure `topic_here` guards: a session
            # with no topic still takes `/topic new`, and one that cannot be
            # built takes nothing at all.
            self.console = Console(db, None, me, room=room)
        self.console.auto = False        # the chat drives explicitly, with /run
        self.lines: list[str] = []
        self.console.emit = self.lines.append

    def handle(self, line: str) -> str:
        """One line through the shared dispatch; returns what it printed."""
        self.lines.clear()
        try:
            self.console.handle(line)
        except Exception as exc:                    # never kill the poller
            return f"error: {exc}"
        out = "\n".join(str(x) for x in self.lines if str(x).strip())
        # The console writes for a terminal, where a colour code is invisible.
        # In a chat it is literal noise: `[2mnow on ...` is what arrives.
        return ANSI.sub("", out)

    @property
    def topic(self) -> str | None:
        """The slug this chat is standing on.

        A council spans many messages. Rebuilding the board for each one forgot
        the last, so `/topic agenda` answered "no topic yet" about a topic that
        had just been created.
        """
        tid = self.console.topic_id
        if tid is None:
            return None
        return self.console.store.topic(tid)["slug"]

    def close(self) -> None:
        self.console.store.close()


HELP = (
    "<b>mooting</b> — a council in this chat\n\n"
    "<b>talk</b>\n"
    "  any message posts as you, and answers anything asked of you\n"
    "  <code>@Santa what about the windows?</code> asks one seat\n\n"
    "<b>move around</b>\n"
    "  <code>/topics</code> — every council as buttons; tap one to come here\n\n"
    "<b>run it</b>\n"
    "  <code>/topic new should we cap retries?</code>\n"
    "  <code>/topic agenda cap; jitter; who owns the runbook</code>\n"
    "  <code>/run</code> · <code>/stop</code> · <code>/seats</code> "
    "· <code>/proposals</code>\n\n"
    "<b>who may speak</b>\n"
    "  <code>/pair</code> — ask to join; an existing member approves\n"
    "  <code>/pair list</code> · <code>/pair approve &lt;id&gt; &lt;seat&gt;</code>\n\n"
    "<b>rule on it</b>\n"
    "  a proposal arrives with Approve / Reject buttons; the reason\n"
    "  is the reply it asks you for"
)


#: What Telegram offers when somebody types `/`. Without registering these the
#: client shows nothing at all, and every command has to be remembered.
#: Descriptions are what appears beside each one, so they are written for
#: somebody who has not read any of this.
MENU = [
    ("pair", "join this council, or approve someone who asked"),
    ("topics", "every council, as buttons — tap one to move this chat to it"),
    ("topic", "new <question> · agenda <a; b> · chair <name> · list"),
    ("seats", "who is here, and how many turns they have left"),
    ("team", "the seats a new meeting here starts with; `team <a> <b>` sets it"),
    ("rooms", "this room: its team, its topic, and its chat id"),
    ("usage", "what each seat has spent; `usage hour` for the last hour"),
    ("me", "<name> — what the council calls you"),
    ("run", "wake the seats and hold a round"),
    ("stop", "stop after the turn in flight"),
    ("nudge", "<seat> — wake one of them by hand"),
    ("effort", "low · medium · high — how long they think before answering"),
    ("rounds", "<n> — grant the council more rounds on this topic"),
    ("proposals", "what is waiting on your sign-off"),
    ("asks", "questions the council has put to you"),
    ("tasks", "the work plan and where each task has got to"),
    ("attach", "feed a document to the council"),
    ("show", "<id> — a message in full, however far back it scrolled"),
    ("minutes", "the meeting as a file; `minutes decisions` for the decisions"),
    ("conclude", "<closing words> — close the meeting and write it up"),
    ("reopen", "resume a meeting you concluded"),
    ("help", "all of the above, with examples"),
]

#: Always above the keyboard, so the things done most often are one tap and
#: never a menu. Two rows of three: more than that and the chat is squeezed off
#: a phone screen, which costs more than it saves.
DESK = (("/run", "/stop", "/proposals"),
        ("/topics", "/seats", "/usage"))


def desk_keyboard():
    """The standing set of actions, as a keyboard that does not go away."""
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=label) for label in row] for row in DESK],
        resize_keyboard=True, is_persistent=True,
        input_field_placeholder="say something to the council")


#: Deliberately absent from the menu, though both still work when typed.
#: `reset` clears every topic on the board and must not be one tap from a thumb
#: — it has already been run by accident here. `capability` hands a seat the
#: right to edit files, which is the one escalation in this project and should
#: be a considered gesture rather than a menu item. `approve` and `reject` are
#: absent for a different reason: the buttons on a proposal carry its id, and
#: typing `/approve 3` from memory is how a sign-off lands on the wrong one.
OFF_MENU = ("reset", "capability", "approve", "reject", "quit")


def explain_start_failure(exc) -> list[str] | None:
    """Why the bot could not start, in words. `None` if this is not ours.

    A pure function so it can be tested without faking aiogram. Every failure
    here is something Telegram does, and the only thing worth asserting is that
    each produces a sentence rather than a stack trace through somebody else's
    internals -- which is the least useful possible answer to "my token is
    wrong", and that is the commonest first run there is.
    """
    from aiogram.exceptions import (TelegramAPIError, TelegramNetworkError,
                                    TelegramNotFound, TelegramUnauthorizedError)
    from aiogram.utils.token import TokenValidationError

    if isinstance(exc, TokenValidationError):
        # Raised before any request; the shape a truncated paste gives.
        return [
            "That does not look like a bot token.",
            "@BotFather issues them as `8123456789:AAF...` — digits, a colon,",
            "then a long string. Check nothing was cut off when copying.",
        ]
    if isinstance(exc, (TelegramUnauthorizedError, TelegramNotFound)):
        # 401 for a wrong token, 404 for one revoked or malformed. Both mean the
        # same thing to a person, and catching only the first left the second to
        # print a traceback.
        return [
            "Telegram will not accept that token.",
            "It is either mistyped, or it was revoked — revoking issues a new",
            "one and instantly kills the old.",
            "",
            "Get the current one from @BotFather: /mybots -> your bot -> API Token.",
        ]
    if isinstance(exc, TelegramNetworkError):
        return [
            f"Could not reach Telegram: {exc}",
            "Check the machine is online, and any proxy or firewall in the way.",
        ]
    if isinstance(exc, TelegramAPIError):
        return [f"Telegram refused the request: {exc}"]
    return None


#: A button press has to say which proposal it meant, because a chat scrolls
#: and `/approve <id>` typed from memory is how a ruling lands on the wrong one.
#: Telegram caps `callback_data` at 64 bytes; these are nowhere near it.
RULE_PREFIX = "rule"


def proposal_ref(text: str) -> int | None:
    """`/proposals 3` typed in a chat, or None when it is not that.

    Buttons only reach a chat through the pump, and the pump starts at the
    board's head so it never replays. A proposal opened before the bot was
    started -- or during a council held at the terminal -- therefore had no way
    to get its buttons, and `/proposals 3` rendered as flat text like every
    other command. This is the way back to them.

    Pure, so it can be tested without faking aiogram.
    """
    m = re.fullmatch(r"/proposals?(?:@\S+)?\s+#?(\d+)", text.strip(), re.I)
    return int(m.group(1)) if m else None


#: Switching topics is the other gesture a thumb gets wrong. `/topic switch
#: <slug>` asks somebody to retype an identifier from memory on a phone
#: keyboard, and a near miss moves the whole room somewhere nobody meant.
PICK_PREFIX = "pick"


def pick_callback(topic_id: int) -> str:
    data = f"{PICK_PREFIX}:{int(topic_id)}"
    if len(data.encode("utf-8")) > 64:
        raise ValueError("callback_data over Telegram's 64-byte limit")
    return data


def parse_pick(data: str) -> int | None:
    """Topic id behind a picker button, or None if this is not one of ours."""
    parts = (data or "").split(":")
    if len(parts) != 2 or parts[0] != PICK_PREFIX or not parts[1].isdigit():
        return None
    return int(parts[1])


def addressed_here(text: str, username: str | None) -> str | None:
    """The command with our own `@mention` removed, or None if it is not ours.

    A group can hold several bots, so Telegram addresses a tapped command to one
    of them: `/seats` becomes `/seats@jeremy_mooting_bot`. Commands with their
    own aiogram handler had the mention stripped for them and worked; everything
    that goes through the shared dispatch arrived with it attached and came back
    "unknown /seats@jeremy_mooting_bot". Invisible in a one-to-one chat, where
    Telegram appends nothing, which is where all of this was tested.
    """
    body = (text or "").strip()
    if not body.startswith("/"):
        return body
    head, sep, rest = body.partition(" ")
    name, at, target = head.partition("@")
    if not at:
        return body
    if username and target.lower() != username.lower():
        return None                     # somebody else's bot was asked
    return f"{name}{sep}{rest}" if sep else name


def wants_picker(text: str) -> bool:
    """`/topic` or `/topics` with nothing after it.

    A verb after it still means what it always did, so `/topic new ...` and
    `/topic agenda ...` keep working and go to the same console dispatch as
    every other command.
    """
    return bool(re.fullmatch(r"/topics?(?:@\S+)?", (text or "").strip(), re.I))


#: One topic per row: a thumb misses a shared row, and switching to the wrong
#: council is the mistake this exists to prevent.
PICKER_LIMIT = 12


def picker_rows(topics, current: str | None) -> list[tuple[str, int]]:
    """`(button label, topic id)` for a tap-to-switch list.

    Pure, so it can be tested without faking aiogram.
    """
    marks = {"paused": "⏸", "resolved": "✓", "aborted": "✕"}
    rows = []
    for t in topics[:PICKER_LIMIT]:
        here = "● " if t["slug"] == current else ""
        title = " ".join((t["title"] or t["slug"]).split())
        if len(title) > 34:
            title = title[:34].rsplit(" ", 1)[0] + "…"
        label = " ".join(bit for bit in (here.strip(), marks.get(t["status"], ""),
                                         title) if bit)
        rows.append((label, int(t["id"])))
    return rows


#: A request to join arrives with the answer attached. `/pair approve 3` asks
#: somebody to read a number off an earlier message and retype it, which is the
#: same gesture `/approve 3` was replaced for -- and the number is meaningless to
#: the person being asked to trust somebody.
JOIN_PREFIX = "join"


def join_callback(action: str, pid: int) -> str:
    if action not in {"ok", "no"}:
        raise ValueError(f"unknown join action {action!r}")
    data = f"{JOIN_PREFIX}:{action}:{int(pid)}"
    if len(data.encode("utf-8")) > 64:
        raise ValueError("callback_data over Telegram's 64-byte limit")
    return data


def parse_join(data: str) -> tuple[str, int] | None:
    """`(action, pairing id)`, or None if this is not one of ours."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != JOIN_PREFIX:
        return None
    if parts[1] not in {"ok", "no"} or not parts[2].isdigit():
        return None
    return parts[1], int(parts[2])


#: A value a person picks rather than types. `low`, `3`, a seat's name -- short
#: enough that the 64-byte callback is never in question.
SET_PREFIX = "set"


def set_callback(what: str, value: str) -> str:
    data = f"{SET_PREFIX}:{what}:{value}"
    if len(data.encode("utf-8")) > 64:
        raise ValueError("callback_data over Telegram's 64-byte limit")
    return data


def parse_set(data: str) -> tuple[str, str] | None:
    """`(what, value)` behind a picker button, or None if this is not one."""
    parts = (data or "").split(":", 2)
    if len(parts) != 3 or parts[0] != SET_PREFIX:
        return None
    if parts[1] not in {"effort", "rounds", "chair", "wake"} or not parts[2]:
        return None
    return parts[1], parts[2]


#: Bare commands that should offer their answers instead of asking for typing.
#: The verb alone is the question; the buttons are the answer.
def wants_choices(text: str) -> str | None:
    """Which chooser a bare command is asking for, if any."""
    import re as _re

    m = _re.fullmatch(r"/(effort|rounds|nudge|chair)(?:@\S+)?", (text or "").strip(),
                      _re.I)
    return m.group(1).lower() if m else None


def rule_callback(action: str, pid: int) -> str:
    if action not in {"ok", "no", "full"}:
        raise ValueError(f"unknown ruling action {action!r}")
    data = f"{RULE_PREFIX}:{action}:{pid}"
    if len(data.encode("utf-8")) > 64:
        raise ValueError("callback_data over Telegram's 64-byte limit")
    return data


def parse_rule(data: str) -> tuple[str, int] | None:
    """`(action, proposal id)`, or None if this is not one of ours."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != RULE_PREFIX:
        return None
    if parts[1] not in {"ok", "no", "full"} or not parts[2].isdigit():
        return None
    return parts[1], int(parts[2])


def plain(rendered: str) -> str:
    """Tags stripped, for when Telegram rejects the formatted version."""
    return html.unescape(re.sub(r"<[^>]+>", "", rendered))


#: A chat has no colour, so a seat gets a mark instead. Allocated by sorted
#: position among the seats on the board rather than hashed, for the reason the
#: full-screen view does the same: hashing put two of this project's own seats on
#: one colour, and distinctness is the entire point.
SEAT_MARKS = ("\U0001f535", "\U0001f7e2", "\U0001f7e1", "\U0001f7e3",
              "\U0001f534", "\U0001f7e0", "\U0001f7e4", "\u26aa")


def mark_for(store, name: str) -> str:
    """The seat's mark, stable for as long as the roster is."""
    names = sorted(a["name"] for a in store.agents())
    if name not in names:
        return ""
    return SEAT_MARKS[names.index(name) % len(SEAT_MARKS)]


def mention(store, name: str) -> str:
    """A seat's name, as a real Telegram mention when the account is known.

    A person whose account was bound by a claim code gets pinged rather than
    merely written about -- which is the difference between being asked a
    question and finding out later that one was asked.
    """
    row = store.q1("SELECT tg_user_id FROM agents WHERE name = ?", (name,))
    if row and row["tg_user_id"]:
        return f'<a href="tg://user?id={row["tg_user_id"]}">{html.escape(name)}</a>'
    return html.escape(name)


def with_mentions(store, body: str) -> str:
    """Turn `@name` into a real mention for anybody whose account is known."""
    import re as _re

    def swap(m):
        link = mention(store, m.group(1))
        return link if link.startswith("<a ") else m.group(0)

    return _re.sub(r"@([A-Za-z0-9_-]{2,32})", swap, body)


def event_text(store, ev) -> str | None:
    """One board event as something worth putting in a chat, or nothing.

    System messages stay out: round markers and pause notices are terminal
    furniture, and in a chat they are twenty messages nobody wanted against a
    twenty-a-minute ceiling.
    """
    if ev.kind == "message":
        row = store.q1("SELECT * FROM messages WHERE id = ?",
                       (ev.payload.get("message_id"),))
        if row is None or row["kind"] == "system":
            return None
        head = f"{mark_for(store, row['author'])} **{row['author']}**".strip()
        return f"{head}\n{row['body']}"
    if ev.kind == "proposal" and ev.payload.get("action") == "opened":
        # The pump normally sends these through `say_proposal`, so they arrive
        # with their buttons. This is the fallback when that could not render,
        # and it says how to get them back rather than claiming, as it used to,
        # that chat rulings do not exist.
        pid = ev.payload['proposal_id']
        return (f"**proposal #{pid}** {ev.payload.get('title', '')}\n"
                f"by {ev.actor} — /proposals {pid} for the buttons.")
    if ev.kind == "decision":
        # Without this a decision taken at the terminal never reached the chat:
        # somebody following from a phone watched a proposal arrive and never
        # learned what happened to it.
        why = (ev.payload.get('rationale') or '').strip()
        return (f"**proposal #{ev.payload['proposal_id']} "
                f"{ev.payload.get('status', 'decided')}** by {ev.actor}"
                + (f" — {why}" if why else ""))
    return None


def run(db, *, bot_token: str, chats, human: str, topic=None,
        remember: bool = False) -> int:        # pragma: no cover - needs a token
    """Long-poll Telegram and drive a council from a chat.

    Long polling rather than webhooks: a webhook needs a public HTTPS endpoint,
    which is a deployment problem rather than a first milestone.
    """
    import secrets

    from aiogram import Bot, Dispatcher
    from aiogram.enums import ParseMode
    from aiogram.filters import Command
    from aiogram.types import Message

    from .store import HUMAN_KINDS, NotAuthorised, StoreError, connect

    try:
        bot = Bot(token=bot_token)
    except Exception as exc:
        lines = explain_start_failure(exc)
        if lines is None:
            raise
        print("", *[f"  {ln}" for ln in lines], sep="\n", file=sys.stderr)
        return 1

    dp = Dispatcher()
    store = connect(db)
    throttles: dict[str, Throttle] = {}
    chats = {str(c) for c in (chats or [])}
    #: A one-time code, only while nobody is paired. Once somebody is, approving
    #: is their job.
    # One mechanism, not two. This used to mint its own code that only the
    # bootstrap branch honoured; it is now an ordinary claim, so `mooting claim`
    # and a first run produce the same thing and are redeemed the same way.
    def _first_code() -> str | None:
        if store.pairings("approved"):
            return None
        try:
            return store.new_claim(human)
        except (StoreError, NotAuthorised):
            return None

    claim = {"code": _first_code()}
    #: Filled in at startup. Until then no mention is stripped, which is the safe
    #: direction: a command nobody claims is better than one answered twice.
    me: dict[str, str | None] = {"username": None}
    #: Which topic each chat is standing on; a council spans many messages.
    #: Where each chat is standing. `None` means the topic it was on has gone,
    #: which is different from never having had one only in how it got here.
    where: dict[str, str | None] = {}
    #: One council per topic. Two people pressing /run must not wake every seat
    #: twice on one budget.
    running: dict[int, "asyncio.Task"] = {}
    seen: set[str] = set()
    #: A ruling whose reason has been asked for but not given.
    #: Keyed by the prompt it must be a reply to, so two people
    #: ruling at once cannot pick up each other's answers.
    pending: dict[tuple, tuple] = {}

    async def say(chat_id, markdown: str) -> None:
        t = throttles.setdefault(str(chat_id), Throttle())
        for piece in chunks(markdown):
            await t.wait()
            try:
                await bot.send_message(chat_id, with_mentions(store, piece),
                                       parse_mode=ParseMode.HTML)
            except Exception as exc:
                # A reply that cannot be formatted must still arrive. Losing a
                # seat's argument to one stray character is the worst outcome.
                await bot.send_message(chat_id, plain(piece))
                log.warning("fell back to plain text: %s", exc)

    def allowed(chat_id) -> bool:
        # Default deny. Without an allowlist this answered anywhere it was
        # added, so anybody who knew the bot's name could stand up a group and
        # start talking to somebody else's board. A room is known once a code
        # from the machine has been redeemed in it, or once somebody has been
        # approved there; `--chat` still names them outright.
        if not chats and not store.pairings("approved", chat_id=chat_id):
            if str(chat_id) not in seen:
                seen.add(str(chat_id))
                print(f"  ignored a message from chat {chat_id} — `mooting claim`"
                      f" prints a code that lets somebody in there")
            return False
        if chats and str(chat_id) not in chats:
            # The id is the thing the operator needs and cannot otherwise get.
            # Ignoring the message silently leaves them no way to find it.
            if str(chat_id) not in seen:
                seen.add(str(chat_id))
                print(f"  ignored a message from chat {chat_id} — add "
                      f"--chat {chat_id} to allow it")
            return False
        return True

    def listeners() -> set[str]:
        """Chats a reply should reach.

        Not `chats`: that is the *allowlist*, and it is empty when nobody passed
        `--chat` -- which left the pump with nowhere to send, so a council ran to
        completion with the chat showing nothing at all. The rooms that want
        replies are the ones with somebody paired in them, narrowed by the
        allowlist when there is one.
        """
        paired = {str(r["chat_id"]) for r in store.pairings("approved")}
        return (paired & chats) if chats else paired

    def topic_here(chat_id):
        """The slug this chat is standing on, if it is still there.

        A topic can go away under a chat -- `/reset`, or `/topic rm` from the
        terminal. The chat went on pointing at it, and building the session for
        the next message then raised before any command was dispatched, so every
        message died in the constructor and the room answered nothing at all.
        Not even `/topic new`, which was the one way out.
        """
        slug = where.get(str(chat_id))
        if slug is None:
            slug = store.room_topic("telegram", str(chat_id)) or topic
        if slug is None:
            return None
        try:
            store.topic(slug)
        except StoreError:
            where[str(chat_id)] = None
            return None
        return slug

    @dp.message(Command("start", "help"))
    async def on_help(msg: Message):
        if not allowed(msg.chat.id):
            return
        if store.seat_for_chat(msg.chat.id, msg.from_user.id):
            return await bot.send_message(msg.chat.id, HELP,
                                          parse_mode=ParseMode.HTML,
                                          reply_markup=desk_keyboard())
        # Not paired, so everything in HELP is unreachable and listing it is
        # noise. Say the one thing that is possible from here.
        if claim["code"]:
            text = (
                "<b>mooting</b> — a council in this chat\n\n"
                "You are not paired yet, so nothing else will work.\n\n"
                "<b>Do this:</b> the terminal running the bot printed a line "
                "like\n\n"
                "<code>pair    send  /pair abc123  to the bot to claim the "
                "first seat</code>\n\n"
                "Send that here. It works once.\n\n"
                "No terminal to hand? Send <code>/pair</code> and have somebody "
                "who has one run <code>mooting pair --approve &lt;id&gt;</code>."
            )
        else:
            text = (
                "<b>mooting</b> — a council in this chat\n\n"
                "You are not paired here, so nothing else will work.\n\n"
                "<b>Do this:</b> send <code>/pair</code>. It records a request "
                "and gives you a number.\n\n"
                "Somebody already in this council approves it with "
                "<code>/pair approve &lt;that number&gt;</code>."
            )
        await bot.send_message(msg.chat.id, text, parse_mode=ParseMode.HTML)

    @dp.message(Command("pair"))
    async def on_pair(msg: Message):
        args = (msg.text or "").split()[1:]
        # Checked before the allowlist on purpose: a code read off the terminal
        # is how a room nobody has been approved in becomes a room at all.
        if len(args) == 1 and args[0] not in {"list", "approve", "deny"}:
            got = store.redeem_claim(args[0])
            if got:
                pid = store.pair_request(msg.chat.id, msg.from_user.id,
                                         msg.from_user.full_name or "")
                store.pair_approve(pid, got, got)
                store.bind_identity(got, msg.from_user.id)
                store.claim_room(store.ensure_room("telegram", str(msg.chat.id)), got)
                await bot.send_message(
                    msg.chat.id, "Paired.", reply_markup=desk_keyboard())
                return await say(
                    msg.chat.id,
                    f"You speak as **{got}** and host this room."
                    f"\n\nThis chat is `{msg.chat.id}` — pass "
                    f"`--chat {msg.chat.id}` when starting the bot to keep it to "
                    f"this room only.")
        if not allowed(msg.chat.id):
            return
        seat = store.seat_for_chat(msg.chat.id, msg.from_user.id)

        if args[:1] == ["list"]:
            if not seat:
                return await say(msg.chat.id, "You are not paired here.")
            rows = store.pairings("pending", chat_id=msg.chat.id)
            if not rows:
                return await say(msg.chat.id, "No pending requests here.")
            return await say(msg.chat.id, "\n".join(
                f"- `{r['ref'] or r['id']}` {r['display'] or r['user_id']}"
                for r in rows))

        if args[:1] == ["approve"]:
            answers = (store.room_host(store.ensure_room("telegram", str(msg.chat.id)))
                       or human)
            if not seat or seat != answers:
                return await say(msg.chat.id,
                                 f"Only {answers} can let somebody into this "
                                 f"council.")
            if len(args) < 2:
                return await say(msg.chat.id, "Usage: `/pair approve <id>`")
            # Scoped to this chat: a request from another room is not this
            # room's to answer, and a small integer invited exactly that.
            want = store.pairing_by_ref(args[1], chat_id=msg.chat.id)
            if want is None:
                return await say(msg.chat.id,
                                 f"No request `{args[1]}` waiting in this chat.")
            pid = int(want["id"])
            # Naming them should not be part of approving them: their own
            # display name is the name they already answer to.
            target = (args[2] if len(args) > 2 else
                      store.seat_name_for(want["display"], fallback=f"guest{pid}"))
            try:
                row = store.pair_approve(pid, target, seat)
            except (StoreError, NotAuthorised) as exc:
                return await say(msg.chat.id, str(exc))
            return await say(msg.chat.id,
                             f"{row['display'] or row['user_id']} now speaks as "
                             f"**{row['seat']}**.\n\nThis chat is `{msg.chat.id}`.")

        if args[:1] == ["deny"]:
            if not seat:
                return await say(msg.chat.id, "Only a paired member can do that.")
            if len(args) < 2:
                return await say(msg.chat.id, "Usage: `/pair deny <id>`")
            want = store.pairing_by_ref(args[1], chat_id=msg.chat.id)
            if want is None:
                return await say(msg.chat.id,
                                 f"No request `{args[1]}` waiting in this chat.")
            store.pair_deny(int(want["id"]), seat)
            return await say(msg.chat.id, f"Request `{args[1]}` denied.")

        if seat:
            return await say(msg.chat.id, f"You already speak as **{seat}**.")

        # The person running the bot holds the token, and a room they added it to
        # is a room they authorised. Recognising them saves a trip to a terminal
        # to approve themselves into their own group -- which was the one case
        # where per-room approval had nobody to ask. Only this account, and only
        # into the seat it already holds: everybody else still needs a member.
        if store.seat_for_user(msg.from_user.id) == human:
            pid = store.pair_request(msg.chat.id, msg.from_user.id,
                                     msg.from_user.full_name or "")
            store.pair_approve(pid, human, human)
            store.claim_room(store.ensure_room("telegram", str(msg.chat.id)), human)
            return await say(
                msg.chat.id,
                f"Paired. You speak as **{human}**, the seat you already hold."
                f"\n\nThis chat is `{msg.chat.id}`.")

        who = msg.from_user.full_name or str(msg.from_user.id)
        pid = store.pair_request(msg.chat.id, msg.from_user.id, who)
        await say_join_request(msg.chat.id, pid, who)

    @dp.message(Command("minutes"))
    async def on_minutes(msg: Message):
        """Hand the minutes over, in the chat.

        `Console._minutes` writes a file and prints where it put it, which is
        the right answer at a terminal and no answer at all on a phone -- the
        file is on a machine you are not sitting at. So the document itself
        comes back, and the decisions come back as text you can read without
        opening anything.
        """
        if not allowed(msg.chat.id):
            return
        if not store.seat_for_chat(msg.chat.id, msg.from_user.id):
            return await say(msg.chat.id, "You are not paired here.")
        slug = topic_here(msg.chat.id)
        if not slug:
            return await say(msg.chat.id, "No topic here.")

        args = (msg.text or "").split()[1:]
        # "decisions" first: what you usually want to hand somebody is what
        # was ruled. The transcript is the evidence behind it, not the thing.
        brief = bool(args[:1]) and args[0] in {"decisions", "decision", "-d"}
        await deliver_minutes(msg.chat.id, slug, brief=brief)

    @dp.message(Command("conclude"))
    async def on_conclude(msg: Message):
        """End the meeting and hand back the write-up.

        Same reason as `/minutes`: the console closes a topic and tells you
        where it put the file, which on a phone names a place you cannot reach.
        """
        if not allowed(msg.chat.id):
            return
        seat = store.seat_for_chat(msg.chat.id, msg.from_user.id)
        if not seat:
            return await say(msg.chat.id, "You are not paired here.")
        slug = topic_here(msg.chat.id)
        if not slug:
            return await say(msg.chat.id, "No topic here.")

        note = (msg.text or "").partition(" ")[2].strip()
        board = ChatBoard(db, slug, seat, room=("telegram", str(msg.chat.id)))
        try:
            out = board.handle(f"/conclude {note}".strip())
        finally:
            board.close()
        if out:
            await say(msg.chat.id, out)
        # Whatever it said, the meeting itself is the thing worth having.
        await deliver_minutes(msg.chat.id, slug, brief=False)

    async def deliver_minutes(chat_id, slug: str, brief: bool) -> None:
        from .minutes import render

        t = store.topic(slug)
        tid = int(t["id"])
        text = render(store, tid, transcript=not brief)
        decided = [p for p in store.proposals(tid) if p["status"] != "open"]
        open_ = [p for p in store.proposals(tid) if p["status"] == "open"]
        head = (f"**{t['title'].strip()}** — {len(decided)} decision(s)"
                + (f", {len(open_)} still open" if open_ else ""))

        if brief:
            return await say(chat_id, head + "\n\n" + text)

        from aiogram.types import BufferedInputFile

        try:
            await bot.send_document(
                chat_id,
                BufferedInputFile(text.encode("utf-8"),
                                  filename=f"{t['slug']}-minutes.md"),
                caption=head[:1024], parse_mode=ParseMode.MARKDOWN)
        except Exception as exc:
            # If the upload is refused the meeting still has to arrive.
            log.warning("could not send minutes as a document: %s", exc)
            await say(chat_id, head + "\n\n" + text)

    async def owns_this_group(chat_id, user_id) -> bool:
        """Whether Telegram says this account created the group.

        The room's own `host` is a name claimed by whoever paired first, which is
        an inference about ordering rather than a fact about the group. Telegram
        holds the fact, so ask it: the person who made the group is the host of
        it, whatever order people were let in.

        False for a one-to-one chat, which has no creator, and false when the
        call fails -- a request that cannot be checked is one to put up for
        somebody to answer, not one to wave through.
        """
        try:
            member = await bot.get_chat_member(chat_id, user_id)
        except Exception as exc:
            log.warning("could not ask who owns %s: %s", chat_id, exc)
            return False
        return getattr(member, "status", None) == "creator"

    @dp.message(lambda m: bool(getattr(m, "new_chat_members", None)))
    async def on_join(msg: Message):
        """Somebody was added to the group.

        Telegram says who added them, and that is the fact this needs: an invite
        from the host of the room is the host deciding, and anybody else adding
        somebody is not. Without this the bot never saw a person arrive at all,
        so joining did nothing and the newcomer had to know to type `/pair`.
        """
        if not allowed(msg.chat.id):
            return
        room_id = store.ensure_room("telegram", str(msg.chat.id))
        host = store.room_host(room_id)
        added_by = store.seat_for_chat(msg.chat.id, msg.from_user.id)

        for member in msg.new_chat_members:
            if getattr(member, "is_bot", False):
                continue
            if store.seat_for_chat(msg.chat.id, member.id):
                continue                        # already one of us
            who = member.full_name or str(member.id)
            pid = store.pair_request(msg.chat.id, member.id, who)
            # Owning the group is only ever a confirmation about somebody this
            # board already knows. Taken on its own it is authority anybody can
            # mint: make a group, add this bot, and every person you add is let
            # onto a board that is not yours. `added_by` must be a seat here
            # first -- the Telegram fact then says which seat is the host.
            by_owner = bool(added_by) and await owns_this_group(
                msg.chat.id, msg.from_user.id)
            if added_by and (by_owner or (host and added_by == host)):
                if by_owner:
                    host = store.claim_room(room_id, added_by)
                seat = store.seat_name_for(who, fallback=f"guest{pid}")
                try:
                    row = store.pair_approve(pid, seat, added_by or host or seat)
                except (StoreError, NotAuthorised) as exc:
                    await say(msg.chat.id, str(exc))
                    continue
                await say(msg.chat.id,
                          f"{who} was added by {added_by or 'the group owner'} "
                          f"and speaks as **{row['seat']}**.")
                continue
            await say_join_request(
                msg.chat.id, pid,
                f"{who}" + (f", added by {added_by}" if added_by else ""))

    @dp.message(lambda m: m.document is not None)
    async def on_document(msg: Message):
        """A file sent to the chat becomes an attachment on the topic.

        `/attach <path>` names a file on the machine running the bot, which is
        not the machine you are holding. Sending the document *is* the gesture
        on a phone, so it is the one that works.
        """
        if not allowed(msg.chat.id):
            return
        seat = store.seat_for_chat(msg.chat.id, msg.from_user.id)
        if not seat:
            return await say(msg.chat.id, "You are not paired here.")
        slug = topic_here(msg.chat.id)
        if not slug:
            return await say(msg.chat.id,
                             "No topic yet — `/topic new <your question>` first.")

        import tempfile

        doc = msg.document
        name = doc.file_name or f"attachment-{doc.file_unique_id}"
        try:
            buf = await bot.download(doc)
        except Exception as exc:
            # Bots can only fetch files up to 20 MB; a bigger one fails here.
            return await say(msg.chat.id, f"Could not fetch that file: {exc}")

        tmp = pathlib.Path(tempfile.mkdtemp()) / name
        tmp.write_bytes(buf.read())
        try:
            aid = store.attach(int(store.topic(slug)["id"]), tmp, seat,
                               note=(msg.caption or "").strip())
        except StoreError as exc:
            return await say(msg.chat.id, str(exc))
        finally:
            shutil.rmtree(tmp.parent, ignore_errors=True)

        row = store.q1("SELECT * FROM attachments WHERE id = ?", (aid,))
        how = ("its text goes into every seat's next prompt" if row["is_text"]
               else "binary — the seats get its name and path, not its contents")
        await say(msg.chat.id,
                  f"Attached **{row['name']}** ({row['bytes']:,} bytes) — {how}.")

    @dp.message(Command("run"))
    async def on_run(msg: Message):
        """Drive the council from the bot's own loop.

        Not through `Console._run`: that starts a daemon thread and returns, and
        `ChatBoard` closes its store the moment the message is handled -- so the
        thread lost the board underneath it and the council died in silence,
        having just said it was thinking. The supervisor has to outlive the
        message that started it, so the bot owns it, one task per topic.
        """
        if not allowed(msg.chat.id):
            return
        seat = store.seat_for_chat(msg.chat.id, msg.from_user.id)
        if not seat:
            return await say(msg.chat.id, "You are not paired here.")
        slug = topic_here(msg.chat.id)
        if not slug:
            return await say(msg.chat.id,
                             "No topic yet — `/topic new <your question>`.")
        try:
            t = store.topic(slug)
        except StoreError as exc:
            return await say(msg.chat.id, str(exc))
        tid = int(t["id"])

        task = running.get(tid)
        if task is not None and not task.done():
            return await say(msg.chat.id, "Already running. `/stop` to stop it.")
        holder = store.take_drive(tid, SESSION)
        if holder is not None:
            return await say(msg.chat.id,
                             "Already being driven from another session.")

        from .drivers.registry import build_drivers
        from .supervisor import Caps, Supervisor

        budget = max((s["max_turns"] for s in store.seats(tid)),
                     default=Caps.max_turns_per_seat)
        sup = Supervisor(store, build_drivers(store),
                         Caps(effort=t["effort"] or "low",
                              max_turns_per_seat=budget))

        async def typing_while(task):
            """Telegram's own thinking indicator, for as long as a round runs.

            A turn takes tens of seconds. Without this the chat is silent and
            indistinguishable from a bot that has died -- which is exactly what
            it looked like this afternoon.
            """
            while not task.done():
                try:
                    await bot.send_chat_action(msg.chat.id, "typing")
                except Exception:
                    return                      # never let the indicator kill a round
                await asyncio.sleep(4.0)        # Telegram clears it after ~5s

        async def drive():
            try:
                if t["status"] == "paused":
                    store.set_topic_status(tid, "open", seat, "resumed from chat")
                reason = await sup.run_topic(tid)
                # One line, or the italics straddle two paragraphs and
                # arrive as literal underscores.
                flat = " ".join(str(reason).split())
                await say(msg.chat.id, f"_council stopped: {flat}_")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("council on %s failed", slug)
                await say(msg.chat.id, f"council failed: {exc}")
            finally:
                store.release_drive(tid, SESSION)

        running[tid] = asyncio.create_task(drive())
        asyncio.create_task(typing_while(running[tid]))
        await say(msg.chat.id,
                  f"Thinking at effort **{t['effort'] or 'low'}**. Replies "
                  f"arrive as each seat finishes — about 30 seconds a turn at "
                  f"`low`.")

    @dp.message(Command("stop"))
    async def on_stop(msg: Message):
        if not allowed(msg.chat.id):
            return
        if not store.seat_for_chat(msg.chat.id, msg.from_user.id):
            return await say(msg.chat.id, "You are not paired here.")
        slug = topic_here(msg.chat.id)
        if not slug:
            return await say(msg.chat.id, "No topic here.")
        tid = int(store.topic(slug)["id"])
        task = running.get(tid)
        if task is None or task.done():
            return await say(msg.chat.id, "Nothing is running.")
        task.cancel()
        await say(msg.chat.id, "Stopping after the turn in flight.")

    async def say_proposal(chat_id, pr) -> None:
        """A proposal, with the ruling attached to it.

        `/approve 3 because ...` typed on a phone is the worst version of the
        one gesture that matters. A button carries the proposal id in its own
        callback, so a ruling cannot land on the wrong proposal however far the
        chat has scrolled -- which is the failure `/approve <id>` invites.
        """
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        pid = int(pr["id"])
        body = (pr["body"] or "").strip()
        preview = body if len(body) < 600 else body[:600].rstrip() + "…"
        text = (f"**proposal #{pid}** {pr['title']}\n"
                f"_by {pr['author']}_\n\n{preview}")
        # ids are short; Telegram caps callback_data at 64 bytes and these are
        # nowhere near it.
        keys = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✓ Approve",
                                 callback_data=rule_callback("ok", pid)),
            InlineKeyboardButton(text="✗ Reject",
                                 callback_data=rule_callback("no", pid)),
        ], [
            InlineKeyboardButton(text="Read it all",
                                 callback_data=rule_callback("full", pid)),
        ]])
        rendered = chunks(text)
        for piece in rendered[:-1]:
            await say(chat_id, piece)
        t = throttles.setdefault(str(chat_id), Throttle())
        await t.wait()
        try:
            await bot.send_message(chat_id, rendered[-1], reply_markup=keys,
                                   parse_mode=ParseMode.HTML)
        except Exception as exc:
            log.warning("proposal keyboard failed: %s", exc)
            await say(chat_id, text)

    async def say_join_request(chat_id, pid: int, who: str) -> None:
        """A request to join, with the answer attached."""
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        keys = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"\u2713 Let {who} in",
                                 callback_data=join_callback("ok", pid)),
            InlineKeyboardButton(text="\u2717 No",
                                 callback_data=join_callback("no", pid)),
        ]])
        try:
            await bot.send_message(
                chat_id,
                f"<b>{html.escape(who)}</b> asks to join this council.",
                reply_markup=keys, parse_mode=ParseMode.HTML)
        except Exception as exc:
            log.warning("join keyboard failed: %s", exc)
            row = store.q1("SELECT ref FROM pairings WHERE id = ?", (pid,))
            handle = (row["ref"] if row and row["ref"] else pid)
            await say(chat_id, f"{who} asks to join. `/pair approve {handle}` to "
                               f"let them in.")

    async def send_choices(chat_id, which: str, slug: str | None) -> None:
        """The answers to a bare command, as buttons.

        Typing on a phone is the cost this whole surface exists to avoid, and a
        command whose answers are a short fixed list should never ask for them.
        """
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        rows, head = [], ""
        if which == "effort":
            head = "How long should they think?"
            rows = [[InlineKeyboardButton(text=e, callback_data=set_callback("effort", e))
                     for e in ("low", "medium", "high")]]
        elif which == "rounds":
            head = "How many more rounds?"
            rows = [[InlineKeyboardButton(text=f"+{n}",
                                          callback_data=set_callback("rounds", str(n)))
                     for n in (1, 3, 5)]]
        elif which in {"nudge", "chair"}:
            if not slug:
                return await say(chat_id, "No topic here yet.")
            seats = store.seats(int(store.topic(slug)["id"]))
            want_people = which == "chair"
            names = [r["agent"] for r in seats
                     if (r["kind"] in HUMAN_KINDS) == want_people]
            if not names:
                return await say(chat_id, "Nobody here to choose from.")
            head = ("Who chairs this meeting?" if want_people
                    else "Which seat should wake?")
            rows = [[InlineKeyboardButton(text=n, callback_data=set_callback(which, n))]
                    for n in names]
        try:
            await bot.send_message(chat_id, head,
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except Exception as exc:
            log.warning("chooser %s failed: %s", which, exc)

    async def send_picker(chat_id, *, message_id: int | None = None) -> None:
        """The topic list, as one button per row.

        Sent fresh, or edited in place after a tap so the same message keeps
        working. A chat scrolls, and hunting back up for the picker is the
        thing a phone is worst at.
        """
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        rows = picker_rows(store.topics_for_room(
            store.ensure_room("telegram", str(chat_id))), topic_here(chat_id))
        if not rows:
            return await say(chat_id, "No topics yet — `/topic new <question>`.")
        keys = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=pick_callback(tid))]
            for label, tid in rows
        ])
        here = topic_here(chat_id)
        text = (f"<b>This chat is on</b> <code>{html.escape(here)}</code>"
                if here else "<b>This chat is not on a topic yet</b>")
        text += "\n\nTap one to move the room to it."
        try:
            if message_id is not None:
                return await bot.edit_message_text(
                    text, chat_id=chat_id, message_id=message_id,
                    reply_markup=keys, parse_mode=ParseMode.HTML)
            await bot.send_message(chat_id, text, reply_markup=keys,
                                   parse_mode=ParseMode.HTML)
        except Exception as exc:
            # Telegram refuses an edit that changes nothing, and a picker that
            # cannot redraw must not take the tap down with it.
            log.warning("topic picker: %s", exc)

    @dp.callback_query()
    async def on_rule(call):
        """A button press. The presser's own seat is what rules.

        Whoever tapped it is not necessarily whoever the bot was started as, and
        a ruling recorded under the wrong name is worse than no ruling.
        """
        from aiogram.types import ForceReply

        chat_id = call.message.chat.id
        joining = parse_join(call.data or "")
        if joining is not None:
            action, pid = joining
            if not allowed(chat_id):
                return await call.answer("not this chat", show_alert=True)
            presser = store.seat_for_chat(chat_id, call.from_user.id)
            room_id = store.ensure_room("telegram", str(chat_id))
            # No host yet means nobody has been established here, and "any paired
            # member" would let the first person through the door hold it open
            # for everybody behind them. Falls back to the person running the
            # bot, who is the only one whose authority does not depend on this
            # room being trustworthy.
            answers = store.room_host(room_id) or human
            if not presser or presser != answers:
                return await call.answer(
                    f"Only {answers} can answer that.", show_alert=True)
            want = store.q1("SELECT * FROM pairings WHERE id = ?", (pid,))
            if want is None:
                return await call.answer("that request is gone", show_alert=True)
            if want["status"] != "pending":
                return await call.answer(f"already {want['status']}", show_alert=True)
            try:
                if action == "no":
                    store.pair_deny(pid, presser)
                    await call.answer("refused")
                    return await say(chat_id, f"{want['display'] or pid} was not "
                                              f"let in.")
                seat = store.seat_name_for(want["display"], fallback=f"guest{pid}")
                row = store.pair_approve(pid, seat, presser)
                store.claim_room(store.ensure_room("telegram", str(chat_id)), presser)
            except (StoreError, NotAuthorised) as exc:
                return await call.answer(str(exc)[:180], show_alert=True)
            await call.answer(f"{row['seat']} is in")
            return await say(chat_id,
                             f"{row['display'] or row['user_id']} now speaks as "
                             f"**{row['seat']}**, let in by {presser}.")

        chosen = parse_set(call.data or "")
        if chosen is not None:
            what, value = chosen
            if not allowed(chat_id):
                return await call.answer("not this chat", show_alert=True)
            seat = store.seat_for_chat(chat_id, call.from_user.id)
            if not seat:
                return await call.answer("You are not paired here.", show_alert=True)
            slug = topic_here(chat_id)
            line = {"effort": f"/effort {value}", "rounds": f"/rounds {value}",
                    "chair": f"/topic chair {value}", "wake": f"/nudge {value}"}[what]
            board = ChatBoard(db, slug, seat, room=("telegram", str(chat_id)))
            try:
                out = board.handle(line)
            finally:
                board.close()
            await call.answer(value)
            return await say(chat_id, out or f"{what} → {value}")

        picked = parse_pick(call.data or "")
        if picked is not None:
            if not allowed(chat_id):
                return await call.answer("not this chat", show_alert=True)
            if not store.seat_for_chat(chat_id, call.from_user.id):
                return await call.answer(
                    "You are not paired here — send /pair first.", show_alert=True)
            try:
                t = store.topic(picked)
            except StoreError:
                return await call.answer("that topic is gone", show_alert=True)
            where[str(chat_id)] = t["slug"]
            store.set_room_topic(store.ensure_room("telegram", str(chat_id)), t["slug"])
            await call.answer(f"now on {t['slug']}")
            return await send_picker(chat_id, message_id=call.message.message_id)

        parsed = parse_rule(call.data or "")
        if parsed is None:
            return await call.answer()
        what, pid = parsed
        if not allowed(chat_id):
            return await call.answer("not this chat", show_alert=True)

        seat = store.seat_for_chat(chat_id, call.from_user.id)
        if not seat:
            # Pairing says who may take part, and a button does not get to skip it.
            return await call.answer(
                "You are not paired here — send /pair first.", show_alert=True)

        try:
            pr = store.proposal(pid)
        except StoreError:
            return await call.answer("that proposal is gone", show_alert=True)

        if what == "full":
            await call.answer()
            return await say(chat_id, f"**proposal #{pid}** {pr['title']}\n\n"
                                      f"{pr['body']}")

        if pr["status"] != "open":
            return await call.answer(f"already {pr['status']}", show_alert=True)

        await call.answer("noted — say why")
        prompt = await bot.send_message(
            chat_id,
            f"{'Approving' if what == 'ok' else 'Rejecting'} #{pid}. "
            f"Reply to this with why.",
            reply_markup=ForceReply(force_reply=True, selective=True))
        # The reason is part of the record, so the ruling waits for it rather
        # than landing bare and being explained afterwards.
        pending[(str(chat_id), prompt.message_id)] = (pid, what == "ok", seat)

    async def finish_ruling(msg: Message) -> bool:
        """Complete a ruling whose reason has just arrived. True if it was one."""
        ref = msg.reply_to_message
        key = (str(msg.chat.id), ref.message_id) if ref else None
        if key not in pending:
            return False
        pid, approve, seat = pending.pop(key)
        why = (msg.text or "").strip()
        if store.seat_for_chat(msg.chat.id, msg.from_user.id) != seat:
            await say(msg.chat.id,
                      "That ruling was started by somebody else; it still needs "
                      "their reason.")
            pending[key] = (pid, approve, seat)
            return True
        try:
            store.decide(pid, seat, approve=approve, rationale=why, via="telegram")
        except (StoreError, NotAuthorised) as exc:
            await say(msg.chat.id, str(exc))
            return True
        store.audit(seat, "decide", {"proposal_id": pid, "approve": approve,
                                     "via": "telegram"},
                    topic_id=int(store.proposal(pid)["topic_id"]))
        # The pump announces every decision, wherever it was taken, so saying
        # it here as well would deliver a chat ruling twice.
        return True

    @dp.message()
    async def on_message(msg: Message):
        if not allowed(msg.chat.id) or not (msg.text or "").strip():
            return
        if await finish_ruling(msg):
            return
        seat = store.seat_for_chat(msg.chat.id, msg.from_user.id)
        if not seat:
            # Inert on purpose. An unknown sender in a group must not be able to
            # open topics or spend anybody's subscription.
            who = msg.from_user.full_name or str(msg.from_user.id)
            pid = store.pair_request(msg.chat.id, msg.from_user.id, who)
            await say(msg.chat.id, "You are not paired here yet.")
            return await say_join_request(msg.chat.id, pid, who)
        line = addressed_here(msg.text, me["username"])
        if line is None:
            return                      # addressed to another bot in this group
        # `/topic` with no verb is somebody asking where they are and where
        # else they could be. That is a list to tap, not a slug to retype.
        if wants_picker(line):
            return await send_picker(msg.chat.id)
        chooser = wants_choices(line)
        if chooser:
            return await send_choices(msg.chat.id, chooser, topic_here(msg.chat.id))
        # Ask for a proposal by number and it comes back with its buttons,
        # whenever it was opened.
        want = proposal_ref(line)
        if want is not None:
            try:
                pr = store.proposal(want)
            except StoreError as exc:
                return await say(msg.chat.id, str(exc))
            return await say_proposal(msg.chat.id, pr)

        slug = topic_here(msg.chat.id)
        if (not slug and not line.startswith("/")
                and store.topics_for_room(
                    store.ensure_room("telegram", str(msg.chat.id)))):
            # Something to post and nowhere to post it. Offering the councils
            # that exist beats an error that asks for a slug somebody has to
            # type -- but only for talk. A command answers for itself: hijacking
            # `/team` into the topic list is how this looked broken rather than
            # empty, and every command that does not need a topic was caught by
            # it after a restart forgot where the room was standing.
            return await send_picker(msg.chat.id)
        if slug:
            # Pairing says they may take part; taking part needs a seat. Without
            # this a second person in the room could rule on a plan and not be
            # able to say why, which is exactly the wrong way round.
            try:
                if store.seat_human(int(store.topic(slug)["id"]), seat):
                    await say(msg.chat.id, f"_{seat} joined the council_")
            except StoreError:
                pass
        board = ChatBoard(db, slug, seat, room=("telegram", str(msg.chat.id)))
        try:
            out = board.handle(line)
            # Remember where this chat is standing, so the next message from
            # anybody in the room lands on the same topic.
            if board.topic:
                where[str(msg.chat.id)] = board.topic
                store.set_room_topic(store.ensure_room("telegram", str(msg.chat.id)),
                                     board.topic)
        finally:
            board.close()
        if out:
            await say(msg.chat.id, out)

    def seats_in(chat_id) -> set[str]:
        """The seats held by people in this chat."""
        return {r["seat"] for r in store.pairings("approved", chat_id=chat_id)
                if r["seat"]}

    async def pump() -> None:
        """Board events into the chat.

        The cursor advances only after a send, so a bot that falls over resumes
        where it stopped instead of replaying a whole council.
        """
        cursor = store.head()
        while True:
            try:
                targets = listeners()
                for ev in store.events_since(cursor, None):
                    # Every event went to every paired chat, so a second group
                    # read the first group's council live. A room hears about its
                    # own meetings and the unbound ones, and nothing else.
                    allowed_here = {
                        chat_id for chat_id in targets
                        if store.topic_visible_in(
                            ev.topic_id,
                            store.ensure_room("telegram", str(chat_id)))
                        # A person's own words are already in the room they typed
                        # them in -- Telegram put them there. Sending them back
                        # made every message appear twice, once from them and
                        # once from the bot quoting them.
                        and ev.actor not in seats_in(chat_id)
                    }
                    if (ev.kind == "proposal"
                            and ev.payload.get("action") == "opened"):
                        # A proposal is the one thing only a human closes, so it
                        # arrives with the way to close it attached.
                        try:
                            pr = store.proposal(int(ev.payload["proposal_id"]))
                            for chat_id in allowed_here:
                                await say_proposal(chat_id, pr)
                        except StoreError:
                            pass
                        cursor = ev.id
                        continue
                    text = event_text(store, ev)
                    if text:
                        for chat_id in allowed_here:
                            await say(chat_id, text)
                    cursor = ev.id
            except Exception:
                log.exception("event pump")
            await asyncio.sleep(2.0)

    async def main() -> None:
        from aiogram.types import BotCommand
        # Telegram shows a `/` menu only for commands the bot has registered.
        # Without this the client offers nothing and every command has to be
        # remembered -- which is what openclaw gets right and we did not.
        try:
            me["username"] = (await bot.get_me()).username
            await bot.set_my_commands([BotCommand(command=c, description=d)
                                       for c, d in MENU])
            print(f"  menu    {len(MENU)} commands registered — type / in the "
                  f"chat to see them")
            if remember:
                # Only now. Saving before the first call meant a token Telegram
                # had rejected was remembered, and every later run failed the
                # same way with nothing to say why.
                store.set_setting("telegram.token", bot_token)
                print(f"  token   saved to {store.path} — you will not be asked "
                      f"again")
        except Exception as exc:
            # Not fatal: the bot works, you just have to remember the commands.
            # But say so, because a silently missing menu looks like a bug in
            # Telegram rather than something that failed here.
            print(f"  menu    could NOT register commands: {exc}",
                  file=sys.stderr)
        asyncio.create_task(pump())
        await dp.start_polling(bot, handle_signals=False)

    print(f"  board   {store.path}")
    if claim["code"]:
        print(f"  pair    send  /pair {claim['code']}  to the bot to claim the "
              f"first seat")
    else:
        print("  pair    the host approves in the chat; `mooting claim` prints a "
              "code for a new room")
    print(f"  chats   {', '.join(sorted(chats)) if chats else 'ANY (use --chat)'}")
    print("  polling; Ctrl-C to stop")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        lines = explain_start_failure(exc)
        if lines is None:
            raise
        print("", *[f"  {ln}" for ln in lines], sep="\n", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0
