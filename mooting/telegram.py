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
import re
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


#: Markdown, like everything a chat is sent; each transport renders it.
HELP = (
    "**mooting** — a council in this chat\n\n"
    "**talk**\n"
    "  any message posts as you, and answers anything asked of you\n"
    "  `@Santa what about the windows?` asks one seat\n\n"
    "**move around**\n"
    "  `/topics` — every council as buttons; tap one to come here\n\n"
    "**run it**\n"
    "  `/topic new should we cap retries?`\n"
    "  `/topic agenda cap; jitter; who owns the runbook`\n"
    "  `/run` · `/stop` · `/seats` · `/proposals`\n"
    "  `/proposals 3` — re-read one that has scrolled away\n\n"
    "**who may speak**\n"
    "  `/pair` — ask to join; an existing member approves\n"
    "  `/pair list` · `/pair approve <id> <seat>`\n"
    "  `/pair revoke <who>` — the host takes a seat back\n\n"
    "**sign it off**\n"
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
    ("rounds", "<n> sets the total · +<n> adds more"),
    ("proposals", "what is waiting on your sign-off"),
    ("asks", "questions the council has put to you"),
    ("tasks", "the plan · accept <id> · reject <id> · again <id>"),
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
#: Reasons that actually recur, offered as buttons because typing one is the
#: slowest step in the single gesture this whole project exists for. Each is a
#: sentence rather than a label: the reason is part of the record, and "ok" in
#: a decision column tells a later reader nothing about why.
WHY_PRESETS = {
    True: ("Agreed - the objections were answered.",
           "Agreed - smallest change that works.",
           "Going with it; the remaining risk is acceptable."),
    False: ("The objection stands and was not answered.",
            "Not now - the cost outweighs it.",
            "Needs a smaller first step."),
}

WHY_PREFIX = "why"


def why_callback(approve: bool, pid: int, idx: int) -> str:
    data = f"{WHY_PREFIX}:{'ok' if approve else 'no'}:{int(pid)}:{int(idx)}"
    if len(data.encode("utf-8")) > 64:
        raise ValueError("callback_data over Telegram's 64-byte limit")
    return data


#: The index that means "none of these -- I will type it".
WHY_OWN = 99


def parse_why(data: str) -> tuple[bool, int, str | None] | None:
    """`(approve, proposal_id, reason)` behind a preset button, or None.

    A reason of `None` is the write-my-own button, which falls back to the
    typed reply rather than deciding anything.

    The text is looked up here rather than carried in the callback: 64 bytes
    does not hold a sentence, and a reason that arrived truncated would be
    worse than one that had to be typed.
    """
    parts = (data or "").split(":")
    if len(parts) != 4 or parts[0] != WHY_PREFIX or parts[1] not in {"ok", "no"}:
        return None
    if not parts[2].isdigit() or not parts[3].isdigit():
        return None
    approve = parts[1] == "ok"
    idx = int(parts[3])
    if idx == WHY_OWN:
        return approve, int(parts[2]), None
    presets = WHY_PRESETS[approve]
    if idx >= len(presets):
        return None
    return approve, int(parts[2]), presets[idx]


#: How a stance is marked, matching the full-screen view so two surfaces do not
#: teach different symbols for the same thing.
STANCE_MARKS = {"object": "!", "support": "+", "abstain": "~"}


def stance_lines(store, pid: int) -> str:
    """Who objected and who agreed, to travel with the proposal itself.

    One person deciding does not scale past a couple of seats if the objections
    are somewhere else, and on a phone "somewhere else" means unread. Objections
    come first: a sign-off does not turn on who agreed.
    """
    votes = sorted(store.votes(pid),
                   key=lambda v: {"object": 0, "support": 1}.get(v["stance"], 2))
    if not votes:
        return ""
    lines = []
    for v in votes:
        why = " ".join((v["rationale"] or "").split())
        if len(why) > 140:                  # a chat is not a terminal
            why = why[:140].rsplit(" ", 1)[0] + "…"
        lines.append(f"{STANCE_MARKS.get(v['stance'], '?')} **{v['agent']}** "
                     f"{v['stance']}" + (f" — {why}" if why else ""))
    return "\n\n" + "\n".join(lines)


SHIFT_PREFIX = "moved"


def shift_callback(moved: bool, topic_id: int) -> str:
    data = f"{SHIFT_PREFIX}:{'y' if moved else 'n'}:{int(topic_id)}"
    if len(data.encode("utf-8")) > 64:
        raise ValueError("callback_data over Telegram's 64-byte limit")
    return data


def parse_shift(data: str) -> tuple[bool, int] | None:
    """`(moved, topic_id)` behind the after-sign-off question, or None."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != SHIFT_PREFIX or parts[1] not in {"y", "n"}:
        return None
    if not parts[2].isdigit():
        return None
    return parts[1] == "y", int(parts[2])


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
    if parts[1] not in {"effort", "rounds", "chair", "wake", "team"} or not parts[2]:
        return None
    return parts[1], parts[2]


#: Bare commands that should offer their answers instead of asking for typing.
#: The verb alone is the question; the buttons are the answer.
def wants_choices(text: str) -> str | None:
    """Which chooser a bare command is asking for, if any."""
    import re as _re

    m = _re.fullmatch(r"/(effort|rounds|nudge|chair|team)(?:@\S+)?", (text or "").strip(),
                      _re.I)
    return m.group(1).lower() if m else None


def command_for(what: str, value: str) -> str:
    """The command a chooser button stands for.

    `+` on the rounds line is the whole point: the button is labelled "+5" and
    the chooser asks "how many more". Sending `/rounds 5` *sets* the total, so
    on a topic already at five the button did nothing and reported the number
    it had not changed as though it had worked.
    """
    return {"effort": f"/effort {value}",
            "rounds": f"/rounds +{value}",
            "chair": f"/topic chair {value}",
            "wake": f"/nudge {value}"}[what]


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


#: Everything a phone can put in a chat, and what to call it once it is a
#: file on disk. Only `document` was handled, so attaching worked for a file
#: picked out of Files and did nothing at all for a photo -- which is what
#: the share sheet sends, and the most likely thing anybody attaches from a
#: phone. Silence looked like a broken bot rather than an unsupported kind.
def file_in(msg):
    """The attachment on this message as (object, filename), or (None, None)."""
    doc = getattr(msg, "document", None)
    if doc is not None:
        return doc, doc.file_name or f"attachment-{doc.file_unique_id}"
    shots = getattr(msg, "photo", None)
    if shots:
        # A list of sizes, smallest first. The last one is the original.
        return shots[-1], f"photo-{shots[-1].file_unique_id}.jpg"
    for attr, ext in (("video", "mp4"), ("audio", "mp3"), ("voice", "ogg"),
                      ("animation", "gif"), ("video_note", "mp4")):
        got = getattr(msg, attr, None)
        if got is not None:
            named = getattr(got, "file_name", None)
            return got, named or f"{attr}-{got.file_unique_id}.{ext}"
    return None, None


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
    if ev.kind == "task":
        # A finished task arrives as a system message, which the filter above
        # drops, so from a phone work was done and reported into silence. The
        # chair then had nothing to accept and the topic could not complete.
        # Only the two ends: `in_progress` every round is the noise that rule
        # exists to keep out.
        action = ev.payload.get("action")
        if action not in {"done", "blocked", "accepted", "rejected"}:
            return None
        row = store.q1("SELECT * FROM tasks WHERE id = ?",
                       (ev.payload.get("task_id"),))
        if row is None:
            return None
        head = f"**task #{row['id']} {action}** {row['title']} — {ev.actor}"
        if action == "done":
            head += f"\n/tasks accept {row['id']} <why> · /tasks again {row['id']} <why>"
        return head
    return None




# --------------------------------------------------------------- transport

class TelegramTransport:
    """`chat.Transport` for Telegram, over aiogram.

    Everything Telegram-shaped is here: HTML rendering and its plain-text
    fallback, the send throttle, inline keyboards, ForceReply, and asking
    Telegram who created a group. `chat.ChatHost` holds everything else.
    """

    channel = "telegram"
    pin_hint = "--chat"

    def __init__(self, bot, store) -> None:
        self.bot, self.store = bot, store
        self.throttles: dict[str, Throttle] = {}
        #: Filled in at startup. Until then no mention is stripped, which is the
        #: safe direction: a command nobody claims beats one answered twice.
        self.username: str | None = None

    def _keys(self, buttons):
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=b.text, callback_data=b.data) for b in row]
            for row in buttons])

    async def _one(self, chat_id, html_text, markup=None):
        from aiogram.enums import ParseMode
        t = self.throttles.setdefault(str(chat_id), Throttle())
        await t.wait()
        try:
            return await self.bot.send_message(
                chat_id, with_mentions(self.store, html_text),
                parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception as exc:
            # A reply that cannot be formatted must still arrive. Losing a
            # seat's argument to one stray character is the worst outcome.
            log.warning("fell back to plain text: %s", exc)
            return await self.bot.send_message(chat_id, plain(html_text),
                                               reply_markup=markup)

    async def send(self, chat_id, markdown, *, buttons=None, desk=False):
        pieces = chunks(markdown) or [""]
        for piece in pieces[:-1]:
            await self._one(chat_id, piece)
        markup = (self._keys(buttons) if buttons
                  else desk_keyboard() if desk else None)
        sent = await self._one(chat_id, pieces[-1], markup)
        return getattr(sent, "message_id", None)

    async def edit(self, chat_id, message, markdown, *, buttons=None):
        from aiogram.enums import ParseMode
        await self.bot.edit_message_text(
            chunks(markdown)[0], chat_id=chat_id, message_id=message,
            reply_markup=self._keys(buttons) if buttons else None,
            parse_mode=ParseMode.HTML)

    async def delete(self, chat_id, message):
        await self.bot.delete_message(chat_id, message)

    async def answer(self, tap, text="", alert=False):
        await tap.handle.answer(text or None, show_alert=alert)

    async def ask_reply(self, chat_id, text):
        from aiogram.types import ForceReply
        sent = await self.bot.send_message(
            chat_id, text, reply_markup=ForceReply(force_reply=True, selective=True))
        return sent.message_id

    async def typing(self, chat_id):
        await self.bot.send_chat_action(chat_id, "typing")

    async def send_file(self, chat_id, name, data, caption):
        from aiogram.enums import ParseMode
        from aiogram.types import BufferedInputFile
        await self.bot.send_document(
            chat_id, BufferedInputFile(data, filename=name),
            caption=chunks(caption)[0][:1024], parse_mode=ParseMode.HTML)

    async def owns_group(self, chat_id, user_id):
        """Whether Telegram says this account created the group. False when the
        call fails: a request that cannot be checked is put up for somebody to
        answer, not waved through."""
        try:
            member = await self.bot.get_chat_member(chat_id, user_id)
        except Exception as exc:
            log.warning("could not ask who owns %s: %s", chat_id, exc)
            return False
        return getattr(member, "status", None) == "creator"

    def addressed(self, text):
        return addressed_here(text, self.username)


def incoming(msg, bot):
    """An aiogram message, as the host sees one."""
    from .chat import Incoming, Member

    user = msg.from_user
    doc, name = file_in(msg)

    async def fetch():
        # Bots can only fetch files up to 20 MB; a bigger one fails here.
        return (await bot.download(doc)).read()
    return Incoming(
        chat_id=str(msg.chat.id), user_id=str(user.id),
        name=user.full_name or str(user.id), text=msg.text or "",
        private=msg.chat.type == "private",
        reply_to=(msg.reply_to_message.message_id
                  if msg.reply_to_message else None),
        file=(name, fetch) if doc is not None else None,
        caption=msg.caption or "",
        new_members=[Member(str(u.id), u.full_name or str(u.id),
                            bool(getattr(u, "is_bot", False)))
                     for u in (getattr(msg, "new_chat_members", None) or [])])


def run(db, *, bot_token: str, chats, human: str, topic=None,
        remember: bool = False) -> int:        # pragma: no cover - needs a token
    """Long-poll Telegram and drive a council from a chat.

    Long polling rather than webhooks: a webhook needs a public HTTPS endpoint,
    which is a deployment problem rather than a first milestone. Everything
    past receiving a message is `chat.ChatHost`.
    """
    from aiogram import Bot, Dispatcher

    from .chat import ChatHost, Tap
    from .store import connect

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
    transport = TelegramTransport(bot, store)
    host = ChatHost(db, store, transport, human=human, chats=chats, topic=topic)

    @dp.message()
    async def on_message(msg):
        await host.route(incoming(msg, bot))

    @dp.callback_query()
    async def on_tap(call):
        await host.on_tap(Tap(chat_id=str(call.message.chat.id),
                              user_id=str(call.from_user.id), data=call.data or "",
                              message=call.message.message_id, handle=call))

    async def main() -> None:
        from aiogram.types import BotCommand
        # Telegram shows a `/` menu only for commands the bot has registered.
        try:
            transport.username = (await bot.get_me()).username
            await bot.set_my_commands([BotCommand(command=c, description=d)
                                       for c, d in MENU])
            print(f"  menu    {len(MENU)} commands registered — type / in the "
                  f"chat to see them")
            if remember:
                # Only now: saving before the first call remembered a token
                # Telegram had rejected.
                store.set_setting("telegram.token", bot_token)
                print(f"  token   saved to {store.path} — you will not be asked "
                      f"again")
        except Exception as exc:
            print(f"  menu    could NOT register commands: {exc}", file=sys.stderr)
        asyncio.create_task(host.pump())
        await dp.start_polling(bot, handle_signals=False)

    print(f"  board   {store.path}")
    if host.claim:
        print(f"  pair    send  /pair {host.claim}  to the bot to claim the "
              f"first seat")
    else:
        print("  pair    the host approves in the chat; `mooting claim` prints a "
              "code for a new room")
    chats_ = sorted(host.chats)
    print(f"  chats   {', '.join(chats_) if chats_ else 'ANY (use --chat)'}")
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
