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
import pathlib
import re
import shutil
import sys
import time
from dataclasses import dataclass, field

log = logging.getLogger("mooting.telegram")

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

    def __init__(self, db, topic, me: str):
        from .console import Console
        self.console = Console(db, topic, me)
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
    ("topic", "new <question> · agenda <a; b> · switch <slug> · list"),
    ("run", "wake the seats and hold a round"),
    ("stop", "stop after the turn in flight"),
    ("seats", "who is here, and how many turns they have left"),
    ("proposals", "what is waiting on your sign-off"),
    ("asks", "questions the council has put to you"),
    ("attach", "feed a document to the council"),
    ("minutes", "the meeting as a file; `minutes decisions` for the decisions"),
    ("help", "all of the above, with examples"),
]


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
        return f"**{row['author']}**\n{row['body']}"
    if ev.kind == "proposal" and ev.payload.get("action") == "opened":
        return (f"**proposal #{ev.payload['proposal_id']}** "
                f"{ev.payload.get('title', '')}\n"
                f"by {ev.actor} — rule on it from a session; chat rulings are "
                f"not available yet.")
    return None


HELP = (
    "<b>mooting</b> — a council in this chat\n\n"
    "<b>talk</b>\n"
    "  any message posts as you, and answers anything asked of you\n"
    "  <code>@Santa what about the windows?</code> asks one seat\n\n"
    "<b>run it</b>\n"
    "  <code>/topic new should we cap retries?</code>\n"
    "  <code>/topic agenda cap; jitter; who owns the runbook</code>\n"
    "  <code>/run</code> · <code>/stop</code> · <code>/seats</code> "
    "· <code>/proposals</code>\n\n"
    "<b>who may speak</b>\n"
    "  <code>/pair list</code> · <code>/pair approve &lt;id&gt;</code>\n\n"
    "<b>rule on it</b>\n"
    "  a proposal arrives with Approve / Reject buttons; the reason\n"
    "  is the reply it asks you for"
)


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

    from .store import NotAuthorised, StoreError, connect

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
    claim = {"code": None if store.pairings("approved") else secrets.token_hex(3)}
    #: Which topic each chat is standing on; a council spans many messages.
    where: dict[str, str] = {}
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
                await bot.send_message(chat_id, piece, parse_mode=ParseMode.HTML)
            except Exception as exc:
                # A reply that cannot be formatted must still arrive. Losing a
                # seat's argument to one stray character is the worst outcome.
                await bot.send_message(chat_id, plain(piece))
                log.warning("fell back to plain text: %s", exc)

    def allowed(chat_id) -> bool:
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
        return where.get(str(chat_id), topic)

    @dp.message(Command("start", "help"))
    async def on_help(msg: Message):
        if not allowed(msg.chat.id):
            return
        if store.seat_for_chat(msg.chat.id, msg.from_user.id):
            return await bot.send_message(msg.chat.id, HELP,
                                          parse_mode=ParseMode.HTML)
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
        if not allowed(msg.chat.id):
            return
        args = (msg.text or "").split()[1:]
        seat = store.seat_for_chat(msg.chat.id, msg.from_user.id)

        if args[:1] == ["list"]:
            if not seat:
                return await say(msg.chat.id, "You are not paired here.")
            rows = store.pairings("pending")
            if not rows:
                return await say(msg.chat.id, "No pending requests.")
            return await say(msg.chat.id, "\n".join(
                f"- `{r['id']}` {r['display'] or r['user_id']}" for r in rows))

        if args[:1] == ["approve"]:
            if not seat:
                return await say(msg.chat.id,
                                 "Only a paired member can approve. Ask one.")
            try:
                pid = int(args[1])
            except (IndexError, ValueError):
                return await say(msg.chat.id, "Usage: `/pair approve <id>`")
            want = store.q1("SELECT * FROM pairings WHERE id = ?", (pid,))
            if want is None:
                return await say(msg.chat.id, f"No pairing request `{pid}`.")
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
                             f"**{row['seat']}**.")

        if args[:1] == ["deny"]:
            if not seat:
                return await say(msg.chat.id, "Only a paired member can do that.")
            try:
                store.pair_deny(int(args[1]), seat)
            except (IndexError, ValueError):
                return await say(msg.chat.id, "Usage: `/pair deny <id>`")
            return await say(msg.chat.id, f"Request `{args[1]}` denied.")

        if seat:
            return await say(msg.chat.id, f"You already speak as **{seat}**.")

        # Bootstrap. The first person has nobody to approve them, so the code
        # printed at startup stands in for the authority they do not have yet --
        # holding it proves they can see the machine the bot runs on.
        if claim["code"] and args[:1] == [claim["code"]]:
            pid = store.pair_request(msg.chat.id, msg.from_user.id,
                                     msg.from_user.full_name or "")
            target = store.seat_name_for(msg.from_user.full_name or "",
                                         fallback=human)
            row = store.pair_approve(pid, target, target)
            claim["code"] = None                 # one use, then it is gone
            return await say(
                msg.chat.id,
                f"Paired. You speak as **{row['seat']}**.\n\n"
                f"Next: `/topic new <your question>`, then `/run`.\n\n"
                f"This chat is `{msg.chat.id}` — pass `--chat {msg.chat.id}` "
                f"when starting the bot to keep it to this room only.")

        pid = store.pair_request(msg.chat.id, msg.from_user.id,
                                 msg.from_user.full_name or "")
        await say(msg.chat.id,
                  f"Pairing request `{pid}` recorded. A paired member approves "
                  f"it with `/pair approve {pid}`.")

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
        board = ChatBoard(db, slug, seat)
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

        from .drivers.registry import build_drivers
        from .supervisor import Caps, Supervisor

        budget = max((s["max_turns"] for s in store.seats(tid)),
                     default=Caps.max_turns_per_seat)
        sup = Supervisor(store, build_drivers(store),
                         Caps(effort=t["effort"] or "low",
                              max_turns_per_seat=budget))

        async def drive():
            try:
                if t["status"] == "paused":
                    store.set_topic_status(tid, "open", seat, "resumed from chat")
                reason = await sup.run_topic(tid)
                await say(msg.chat.id, f"_council stopped: {reason}_")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("council on %s failed", slug)
                await say(msg.chat.id, f"council failed: {exc}")

        running[tid] = asyncio.create_task(drive())
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

    @dp.callback_query()
    async def on_rule(call):
        """A button press. The presser's own seat is what rules.

        Whoever tapped it is not necessarily whoever the bot was started as, and
        a ruling recorded under the wrong name is worse than no ruling.
        """
        from aiogram.types import ForceReply

        parsed = parse_rule(call.data or "")
        if parsed is None:
            return await call.answer()
        what, pid = parsed
        chat_id = call.message.chat.id
        if not allowed(chat_id):
            return await call.answer("not this chat", show_alert=True)

        seat = store.seat_for_chat(chat_id, call.from_user.id)
        if not seat:
            # Pairing is the fence, and a button does not get to skip it.
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
            store.decide(pid, seat, approve=approve, rationale=why)
        except (StoreError, NotAuthorised) as exc:
            await say(msg.chat.id, str(exc))
            return True
        store.audit(seat, "decide", {"proposal_id": pid, "approve": approve,
                                     "via": "telegram"},
                    topic_id=int(store.proposal(pid)["topic_id"]))
        await say(msg.chat.id,
                  f"proposal #{pid} **{store.proposal(pid)['status']}** by "
                  f"{seat} — {why}")
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
            pid = store.pair_request(msg.chat.id, msg.from_user.id,
                                     msg.from_user.full_name or "")
            return await say(msg.chat.id,
                             f"You are not paired here. Request `{pid}` is "
                             f"waiting for a member to approve it.")
        slug = topic_here(msg.chat.id)
        if slug:
            # Pairing says they may take part; taking part needs a seat. Without
            # this a second person in the room could rule on a plan and not be
            # able to say why, which is exactly the wrong way round.
            try:
                if store.seat_human(int(store.topic(slug)["id"]), seat):
                    await say(msg.chat.id, f"_{seat} joined the council_")
            except StoreError:
                pass
        board = ChatBoard(db, slug, seat)
        try:
            out = board.handle(msg.text.strip())
            # Remember where this chat is standing, so the next message from
            # anybody in the room lands on the same topic.
            if board.topic:
                where[str(msg.chat.id)] = board.topic
        finally:
            board.close()
        if out:
            await say(msg.chat.id, out)

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
                    if (ev.kind == "proposal"
                            and ev.payload.get("action") == "opened"):
                        # A proposal is the one thing only a human closes, so it
                        # arrives with the way to close it attached.
                        try:
                            pr = store.proposal(int(ev.payload["proposal_id"]))
                            for chat_id in targets:
                                await say_proposal(chat_id, pr)
                        except StoreError:
                            pass
                        cursor = ev.id
                        continue
                    text = event_text(store, ev)
                    if text:
                        for chat_id in targets:
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
        print("  pair    an existing member approves with /pair approve <id>")
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
