"""A council in a chat, whatever the chat is.

Everything a chat surface does -- pairing, sign-off buttons, the topic picker,
running a council, pumping board events into the room -- lives here, written
against `Transport`. A transport is the part that differs between Telegram,
Discord or anything else: how a message is sent, how a button is drawn, how a
tap is acknowledged. It is a small class; this is the large one.

This was all one closure inside `telegram.run`, 1,170 lines with 59 aiogram
calls and no test that could reach any of it. Every bug in it was found by a
person on a phone. `tests/test_chat.py` now drives it through a transport that
records what it was asked to send, so a second channel does not start by
copying code nobody could test.

Messages are written in the board's markdown. A transport renders them for its
own chat -- Telegram to HTML, Discord as it is -- and splits them at its own
length limit.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import shutil
import tempfile
import uuid
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .telegram import (HELP, WHY_OWN, WHY_PRESETS, ChatBoard,
                       command_for, event_text, join_callback, parse_join,
                       parse_pick, parse_rule, parse_set, parse_shift, parse_why,
                       pick_callback, picker_rows, proposal_ref, rule_callback,
                       set_callback, shift_callback, stance_lines, wants_choices,
                       wants_picker, why_callback)

log = logging.getLogger("mooting.chat")

#: This process, to the board's drive claim.
SESSION = f"chat-{os.getpid()}-{uuid.uuid4().hex[:6]}"


# ---------------------------------------------------------------- the seam

@dataclass
class Button:
    text: str
    data: str


@dataclass
class Member:
    id: str
    name: str
    is_bot: bool = False


@dataclass
class Incoming:
    """One message from a chat, in the terms the host needs and no others."""

    chat_id: str
    user_id: str
    name: str
    text: str = ""
    private: bool = False
    #: The transport's reference to the message this one replies to, if any.
    reply_to: Any = None
    #: (filename, fetch) for an attachment; fetch returns its bytes.
    file: tuple[str, Callable[[], Awaitable[bytes]]] | None = None
    caption: str = ""
    new_members: list[Member] = field(default_factory=list)


@dataclass
class Tap:
    """A button pressed."""

    chat_id: str
    user_id: str
    data: str
    #: The transport's reference to the message the button hangs off.
    message: Any = None
    #: Whatever the transport needs to acknowledge the tap.
    handle: Any = None


class Transport:
    """What a chat must be able to do. Subclass per channel.

    `channel` is the name rooms and pairings are filed under on the board, so
    a Telegram chat and a Discord channel with the same number are different
    rooms.
    """

    channel = "chat"
    #: Printed next to a room so the operator can pin the bot to it.
    pin_hint = "--chat"

    async def send(self, chat_id: str, markdown: str, *,
                   buttons: list[list[Button]] | None = None,
                   desk: bool = False) -> Any:
        """Send markdown, split as the chat needs. Returns a reference to the
        last message sent, which is the one any buttons hang off."""
        raise NotImplementedError

    async def edit(self, chat_id: str, message: Any, markdown: str, *,
                   buttons: list[list[Button]] | None = None) -> None:
        raise NotImplementedError

    async def delete(self, chat_id: str, message: Any) -> None:
        raise NotImplementedError

    async def answer(self, tap: Tap, text: str = "", alert: bool = False) -> None:
        """Acknowledge a tap. `alert` means the presser must read it."""
        raise NotImplementedError

    async def ask_reply(self, chat_id: str, text: str) -> Any:
        """Ask for a typed answer; returns what a reply to it will point at."""
        raise NotImplementedError

    async def typing(self, chat_id: str) -> None:
        """Show that something is happening. Optional."""

    async def send_file(self, chat_id: str, name: str, data: bytes,
                        caption: str) -> None:
        raise NotImplementedError

    async def owns_group(self, chat_id: str, user_id: str) -> bool:
        """Whether the chat itself says this account created the room."""
        return False

    def addressed(self, text: str) -> str | None:
        """The text with our own address removed, or None if it is not ours."""
        return (text or "").strip()


# ---------------------------------------------------------------- the host



class ChatHost:
    """A board, served into one or more chats through one transport."""

    def __init__(self, db, store, transport: Transport, *, human: str,
                 chats=(), topic: str | None = None, build_drivers=None) -> None:
        from .store import NotAuthorised, StoreError
        self.NotAuthorised, self.StoreError = NotAuthorised, StoreError

        self.db, self.store, self.t = db, store, transport
        self.channel = transport.channel
        self.human, self.topic = human, topic
        self.chats = {str(c) for c in (chats or [])}
        #: Swappable so a test can seat drivers that spend nothing.
        self._build_drivers = build_drivers
        #: A one-time code, only while nobody is paired.
        self.claim = self._first_code()
        #: Where each chat is standing; None means its topic went away.
        self.where: dict[str, str | None] = {}
        #: One council per topic. Two people pressing /run must not wake every
        #: seat twice on one budget.
        self.running: dict[int, asyncio.Task] = {}
        self.seen: set[str] = set()
        #: A sign-off whose reason has been asked for, keyed by the prompt it
        #: must be a reply to, so two people signing off at once cannot pick
        #: up each other's answers.
        self.pending: dict[tuple, tuple] = {}

    # ------------------------------------------------------------ helpers

    def _first_code(self) -> str | None:
        if self.store.pairings("approved", channel=self.channel):
            return None
        try:
            return self.store.new_claim(self.human)
        except (self.StoreError, self.NotAuthorised):
            return None

    def room(self, chat_id) -> int:
        return self.store.ensure_room(self.channel, str(chat_id))

    def seat_of(self, chat_id, user_id) -> str | None:
        return self.store.seat_for_chat(str(chat_id), str(user_id), self.channel)

    async def say(self, chat_id, markdown: str) -> None:
        await self.t.send(str(chat_id), markdown)

    def board(self, slug, seat, chat_id) -> ChatBoard:
        return ChatBoard(self.db, slug, seat, room=(self.channel, str(chat_id)))

    async def allowed(self, chat_id, user_id=None, private=False) -> bool:
        """Default deny, as `telegram.run` always did.

        A direct message is your own room, open to an account bound by a
        redeemed claim code. A group is known once a code has been redeemed or
        somebody approved in it, or once `--chat` names it.
        """
        store, chat_id = self.store, str(chat_id)
        if private and user_id is not None:
            if store.private_room(str(user_id), self.channel):
                pass
            elif store.seat_for_user(str(user_id), self.channel):
                await self.t.send(
                    chat_id,
                    "You hold a seat in a group I run, but a private room needs "
                    "a code from the machine hosting the board.\n\n"
                    "Ask the host to run `mooting claim` and send you the code, "
                    "then send it here as `/pair <code>`.")
                return False
        if not self.chats and not store.pairings("approved", chat_id=chat_id,
                                                 channel=self.channel):
            if chat_id not in self.seen:
                self.seen.add(chat_id)
                print(f"  ignored a message from {self.channel} chat {chat_id} — "
                      f"`mooting claim` prints a code that lets somebody in there")
            return False
        if self.chats and chat_id not in self.chats:
            if chat_id not in self.seen:
                self.seen.add(chat_id)
                print(f"  ignored a message from {self.channel} chat {chat_id} — "
                      f"add {self.t.pin_hint} {chat_id} to allow it")
            return False
        return True

    def listeners(self) -> set[str]:
        """Chats a board event should reach: the paired ones, narrowed by the
        allowlist when there is one. Not the allowlist itself, which is empty
        when nobody passed one and left the pump with nowhere to send."""
        paired = {str(r["chat_id"])
                  for r in self.store.pairings("approved", channel=self.channel)}
        return (paired & self.chats) if self.chats else paired

    def topic_here(self, chat_id) -> str | None:
        """The slug this chat is standing on, if it is still there.

        A topic can go away under a chat. Building the session for the next
        message then raised before any command ran, so the room answered
        nothing -- not even `/topic new`, the one way out.
        """
        slug = self.where.get(str(chat_id))
        if slug is None:
            slug = self.store.room_topic(self.channel, str(chat_id)) or self.topic
        if slug is None:
            return None
        try:
            self.store.topic(slug)
        except self.StoreError:
            self.where[str(chat_id)] = None
            return None
        return slug

    def seats_in(self, chat_id) -> set[str]:
        return {r["seat"] for r in self.store.pairings("approved", chat_id=str(chat_id),
                                                       channel=self.channel)
                if r["seat"]}

    # ------------------------------------------------------------ routing

    async def route(self, m: Incoming) -> None:
        """One message, to whichever handler it is for."""
        if m.new_members:
            return await self.on_join(m)
        if m.file is not None:
            return await self.on_document(m)
        text = (m.text or "").strip()
        if text.startswith("/"):
            line = self.t.addressed(text)
            if line is None:
                return                  # a command for another bot in this room
            cmd = line.split()[0][1:].lower()
            handler = {"start": self.on_help, "help": self.on_help,
                       "pair": self.on_pair, "minutes": self.on_minutes,
                       "conclude": self.on_conclude, "run": self.on_run,
                       "stop": self.on_stop}.get(cmd)
            if handler is not None:
                m.text = line
                return await handler(m)
        return await self.on_message(m)

    # ----------------------------------------------------------- commands

    async def on_help(self, m: Incoming) -> None:
        if not await self.allowed(m.chat_id, m.user_id, m.private):
            return
        if self.seat_of(m.chat_id, m.user_id):
            await self.t.send(m.chat_id, HELP, desk=True)
            return
        if self.claim:
            text = ("**mooting** — a council in this chat\n\n"
                    "You are not paired yet, so nothing else will work.\n\n"
                    "**Do this:** the terminal running the bot printed a line "
                    "like\n\n"
                    "`pair    send  /pair abc123  to the bot to claim the first seat`"
                    "\n\nSend that here. It works once.\n\n"
                    "No terminal to hand? Send `/pair` and have somebody who has "
                    "one run `mooting pair --approve <id>`.")
        else:
            text = ("**mooting** — a council in this chat\n\n"
                    "You are not paired here, so nothing else will work.\n\n"
                    "**Do this:** send `/pair`. It records a request and gives "
                    "you a number.\n\n"
                    "Somebody already in this council approves it with "
                    "`/pair approve <that number>`.")
        await self.say(m.chat_id, text)

    async def on_pair(self, m: Incoming) -> None:
        store, chat, say = self.store, m.chat_id, self.say
        NotAuthorised, StoreError = self.NotAuthorised, self.StoreError
        args = (m.text or "").split()[1:]
        # Before the allowlist on purpose: a code read off the terminal is how
        # a room nobody has been approved in becomes a room at all.
        if len(args) == 1 and args[0] not in {"list", "approve", "deny", "revoke"}:
            got = store.redeem_claim(args[0])
            if got:
                pid = store.pair_request(chat, m.user_id, m.name, self.channel)
                store.pair_approve(pid, got, got)
                store.bind_identity(got, m.user_id, self.channel)
                store.claim_room(self.room(chat), got)
                self.claim = None
                await self.t.send(chat, "Paired.", desk=True)
                return await say(
                    chat,
                    f"You speak as **{got}** and host this room.\n\nThis chat is "
                    f"`{chat}` — pass `{self.t.pin_hint} {chat}` when starting "
                    f"the bot to keep it to this room only.")
        if not await self.allowed(chat, m.user_id, m.private):
            return
        seat = self.seat_of(chat, m.user_id)

        if args[:1] == ["list"]:
            if not seat:
                return await say(chat, "You are not paired here.")
            rows = store.pairings("pending", chat_id=chat, channel=self.channel)
            live = [r for r in rows if not store.pair_expired(r)]
            if not rows:
                return await say(chat, "No pending requests here.")
            out = [f"- `{r['ref'] or r['id']}` {r['display'] or r['user_id']}"
                   for r in live]
            stale = len(rows) - len(live)
            if stale:
                out.append(f"\n_{stale} older request(s) expired. Ask them to "
                           f"send `/pair` again._")
            return await say(chat, "\n".join(out) or "Every request here has expired.")

        if args[:1] == ["approve"]:
            answers = store.room_host(self.room(chat)) or self.human
            if not seat or seat != answers:
                return await say(chat, f"Only {answers} can let somebody into "
                                       f"this council.")
            if len(args) < 2:
                return await say(chat, "Usage: `/pair approve <id>`")
            want = store.pairing_by_ref(args[1], chat_id=chat, channel=self.channel)
            if want is None:
                return await say(chat, f"No request `{args[1]}` waiting in this chat.")
            pid = int(want["id"])
            target = (args[2] if len(args) > 2 else
                      store.seat_name_for(want["display"], fallback=f"guest{pid}"))
            try:
                row = store.pair_approve(pid, target, seat)
            except (StoreError, NotAuthorised) as exc:
                return await say(chat, str(exc))
            return await say(chat, f"{row['display'] or row['user_id']} now speaks "
                                   f"as **{row['seat']}**.\n\nThis chat is `{chat}`.")

        if args[:1] == ["revoke"]:
            if not seat:
                return await say(chat, "Only a paired member can do that.")
            here = store.pairings("approved", chat_id=chat, channel=self.channel)
            if len(args) < 2:
                rows = [r for r in here if r["seat"] != seat]
                if not rows:
                    return await say(chat, "Nobody else is in this room.")
                return await say(chat, "Usage: `/pair revoke <who>`\n\n"
                                 + "\n".join(f"- **{r['seat']}** "
                                             f"({r['display'] or r['user_id']})"
                                             for r in rows))
            who = args[1].lstrip("@")
            want = next((r for r in here
                         if r["seat"] == who or (r["display"] or "") == who
                         or str(r["user_id"]) == who), None)
            if want is None:
                return await say(chat, f"`{who}` holds no seat in this chat.")
            try:
                row = store.pair_revoke(int(want["id"]), seat)
            except (StoreError, NotAuthorised) as exc:
                return await say(chat, str(exc))
            return await say(chat, f"**{row['seat']}** no longer speaks in this "
                                   f"chat. What they already said stays on the board.")

        if args[:1] == ["deny"]:
            if not seat:
                return await say(chat, "Only a paired member can do that.")
            if len(args) < 2:
                return await say(chat, "Usage: `/pair deny <id>`")
            want = store.pairing_by_ref(args[1], chat_id=chat, channel=self.channel)
            if want is None:
                return await say(chat, f"No request `{args[1]}` waiting in this chat.")
            store.pair_deny(int(want["id"]), seat)
            return await say(chat, f"Request `{args[1]}` denied.")

        if seat:
            return await say(chat, f"You already speak as **{seat}**.")

        # The person running the bot, recognised in a room they added it to --
        # only into the seat they already hold.
        if store.seat_for_user(m.user_id, self.channel) == self.human:
            pid = store.pair_request(chat, m.user_id, m.name, self.channel)
            store.pair_approve(pid, self.human, self.human)
            store.claim_room(self.room(chat), self.human)
            return await say(chat, f"Paired. You speak as **{self.human}**, the "
                                   f"seat you already hold.\n\nThis chat is `{chat}`.")

        who = m.name or m.user_id
        pid = store.pair_request(chat, m.user_id, who, self.channel)
        await self.say_join_request(chat, pid, who)

    async def on_minutes(self, m: Incoming) -> None:
        """The minutes as a file in the chat, because the path the console
        prints is on a machine the person holding the phone is not at."""
        if not await self.allowed(m.chat_id, m.user_id, m.private):
            return
        if not self.seat_of(m.chat_id, m.user_id):
            return await self.say(m.chat_id, "You are not paired here.")
        slug = self.topic_here(m.chat_id)
        if not slug:
            return await self.say(m.chat_id, "No topic here.")
        args = (m.text or "").split()[1:]
        brief = bool(args[:1]) and args[0] in {"decisions", "decision", "-d"}
        await self.deliver_minutes(m.chat_id, slug, brief=brief)

    async def on_conclude(self, m: Incoming) -> None:
        if not await self.allowed(m.chat_id, m.user_id, m.private):
            return
        seat = self.seat_of(m.chat_id, m.user_id)
        if not seat:
            return await self.say(m.chat_id, "You are not paired here.")
        slug = self.topic_here(m.chat_id)
        if not slug:
            return await self.say(m.chat_id, "No topic here.")
        note = (m.text or "").partition(" ")[2].strip()
        board = self.board(slug, seat, m.chat_id)
        try:
            out = board.handle(f"/conclude {note}".strip())
        finally:
            board.close()
        if out:
            await self.say(m.chat_id, out)
        await self.deliver_minutes(m.chat_id, slug, brief=False)

    async def deliver_minutes(self, chat_id, slug: str, brief: bool) -> None:
        from .minutes import render

        store = self.store
        t = store.topic(slug)
        tid = int(t["id"])
        text = render(store, tid, transcript=not brief)
        decided = [p for p in store.proposals(tid) if p["status"] != "open"]
        open_ = [p for p in store.proposals(tid) if p["status"] == "open"]
        head = (f"**{t['title'].strip()}** — {len(decided)} decision(s)"
                + (f", {len(open_)} still open" if open_ else ""))
        if brief:
            return await self.say(chat_id, head + "\n\n" + text)
        try:
            await self.t.send_file(str(chat_id), f"{t['slug']}-minutes.md",
                                   text.encode("utf-8"), head)
        except Exception as exc:
            log.warning("could not send minutes as a file: %s", exc)
            await self.say(chat_id, head + "\n\n" + text)

    async def on_join(self, m: Incoming) -> None:
        """Somebody was added to the room. An invite from the host is the host
        deciding; anybody else adding somebody is a request."""
        store, NotAuthorised, StoreError = self.store, self.NotAuthorised, self.StoreError
        if not await self.allowed(m.chat_id, m.user_id, m.private):
            return
        room_id = self.room(m.chat_id)
        host = store.room_host(room_id)
        added_by = self.seat_of(m.chat_id, m.user_id)
        for member in m.new_members:
            if member.is_bot or self.seat_of(m.chat_id, member.id):
                continue
            who = member.name or member.id
            pid = store.pair_request(m.chat_id, member.id, who, self.channel)
            # Owning the room only confirms somebody this board already knows;
            # on its own it is authority anybody can mint.
            by_owner = bool(added_by) and await self.t.owns_group(m.chat_id, m.user_id)
            if added_by and (by_owner or (host and added_by == host)):
                if by_owner:
                    host = store.claim_room(room_id, added_by)
                seat = store.seat_name_for(who, fallback=f"guest{pid}")
                try:
                    row = store.pair_approve(pid, seat, added_by or host or seat)
                except (StoreError, NotAuthorised) as exc:
                    await self.say(m.chat_id, str(exc))
                    continue
                await self.say(m.chat_id, f"{who} was added by "
                                          f"{added_by or 'the group owner'} and "
                                          f"speaks as **{row['seat']}**.")
                continue
            await self.say_join_request(
                m.chat_id, pid, f"{who}" + (f", added by {added_by}" if added_by else ""))

    async def on_document(self, m: Incoming) -> None:
        """A file sent to the chat becomes an attachment on the topic."""
        from .extract import missing_reader

        store = self.store
        if not await self.allowed(m.chat_id, m.user_id, m.private):
            return
        seat = self.seat_of(m.chat_id, m.user_id)
        if not seat:
            return await self.say(m.chat_id, "You are not paired here.")
        slug = self.topic_here(m.chat_id)
        if not slug:
            return await self.say(m.chat_id,
                                  "No topic yet — `/topic new <your question>` first.")
        name, fetch = m.file
        try:
            data = await fetch()
        except Exception as exc:
            return await self.say(m.chat_id, f"Could not fetch that file: {exc}")
        tmp = pathlib.Path(tempfile.mkdtemp()) / name
        tmp.write_bytes(data)
        try:
            aid = store.attach(int(store.topic(slug)["id"]), tmp, seat,
                               note=(m.caption or "").strip())
        except self.StoreError as exc:
            return await self.say(m.chat_id, str(exc))
        finally:
            shutil.rmtree(tmp.parent, ignore_errors=True)
        row = store.q1("SELECT * FROM attachments WHERE id = ?", (aid,))
        if row["is_text"]:
            how = "its text goes into every seat's next prompt"
        else:
            how = (missing_reader(row["name"])
                   or "the seats get its name and path, not its contents")
        await self.say(m.chat_id,
                       f"Attached **{row['name']}** ({row['bytes']:,} bytes) — {how}.")

    async def on_run(self, m: Incoming) -> None:
        """Drive the council from the bot's own loop, one task per topic, so
        the supervisor outlives the message that started it."""
        store, chat = self.store, m.chat_id
        if not await self.allowed(chat, m.user_id, m.private):
            return
        seat = self.seat_of(chat, m.user_id)
        if not seat:
            return await self.say(chat, "You are not paired here.")
        slug = self.topic_here(chat)
        if not slug:
            return await self.say(chat, "No topic yet — `/topic new <your question>`.")
        try:
            t = store.topic(slug)
        except self.StoreError as exc:
            return await self.say(chat, str(exc))
        tid = int(t["id"])
        task = self.running.get(tid)
        if task is not None and not task.done():
            return await self.say(chat, "Already running. `/stop` to stop it.")
        if store.take_drive(tid, SESSION) is not None:
            return await self.say(chat, "Already being driven from another session.")

        from .supervisor import Caps, Supervisor
        if self._build_drivers is None:
            from .drivers.registry import build_drivers
        else:
            build_drivers = self._build_drivers
        budget = max((s["max_turns"] for s in store.seats(tid)),
                     default=Caps.max_turns_per_seat)
        sup = Supervisor(store, build_drivers(store),
                         Caps(effort=t["effort"] or "low", max_turns_per_seat=budget))

        async def typing_while(task):
            # A turn takes tens of seconds; silence reads as a dead bot.
            while not task.done():
                try:
                    await self.t.typing(chat)
                except Exception:
                    return
                await asyncio.sleep(4.0)

        async def drive():
            try:
                if t["status"] == "paused":
                    store.set_topic_status(tid, "open", seat, "resumed from chat")
                reason = await sup.run_topic(tid)
                # One line, or italics straddle two paragraphs and arrive as
                # literal underscores.
                flat = " ".join(str(reason).split())
                await self.say(chat, f"_council stopped: {flat}_")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("council on %s failed", slug)
                await self.say(chat, f"council failed: {exc}")
            finally:
                store.release_drive(tid, SESSION)

        self.running[tid] = asyncio.create_task(drive())
        asyncio.create_task(typing_while(self.running[tid]))
        await self.say(chat, f"Thinking at effort **{t['effort'] or 'low'}**. "
                             f"Replies arrive as each seat finishes — about 30 "
                             f"seconds a turn at `low`.")

    async def on_stop(self, m: Incoming) -> None:
        if not await self.allowed(m.chat_id, m.user_id, m.private):
            return
        if not self.seat_of(m.chat_id, m.user_id):
            return await self.say(m.chat_id, "You are not paired here.")
        slug = self.topic_here(m.chat_id)
        if not slug:
            return await self.say(m.chat_id, "No topic here.")
        task = self.running.get(int(self.store.topic(slug)["id"]))
        if task is None or task.done():
            return await self.say(m.chat_id, "Nothing is running.")
        task.cancel()
        await self.say(m.chat_id, "Stopping after the turn in flight.")

    # ------------------------------------------------------- what is shown

    async def say_proposal(self, chat_id, pr) -> None:
        """A proposal with the sign-off attached: a button carries the id, so a
        sign-off cannot land on the wrong proposal however far the chat has
        scrolled."""
        pid = int(pr["id"])
        body = (pr["body"] or "").strip()
        preview = body if len(body) < 600 else body[:600].rstrip() + "…"
        text = (f"**proposal #{pid}** {pr['title']}\n_by {pr['author']}_\n\n{preview}"
                + stance_lines(self.store, pid))
        keys = [[Button("✓ Approve", rule_callback("ok", pid)),
                 Button("✗ Reject", rule_callback("no", pid))],
                [Button("Read it all", rule_callback("full", pid))]]
        await self.t.send(str(chat_id), text, buttons=keys)

    async def say_join_request(self, chat_id, pid: int, who: str) -> None:
        keys = [[Button(f"✓ Let {who} in", join_callback("ok", pid)),
                 Button("✗ No", join_callback("no", pid))]]
        try:
            await self.t.send(str(chat_id), f"**{who}** asks to join this council.",
                              buttons=keys)
        except Exception as exc:
            log.warning("join buttons failed: %s", exc)
            row = self.store.q1("SELECT ref FROM pairings WHERE id = ?", (pid,))
            handle = row["ref"] if row and row["ref"] else pid
            await self.say(chat_id, f"{who} asks to join. `/pair approve {handle}` "
                                    f"to let them in.")

    async def send_choices(self, chat_id, which: str, slug: str | None) -> None:
        """The answers to a bare command, as buttons."""
        from .store import HUMAN_KINDS

        store, rows, head = self.store, [], ""
        if which == "effort":
            head = "How long should they think?"
            rows = [[Button(e, set_callback("effort", e)) for e in ("low", "medium", "high")]]
        elif which == "rounds":
            head = "How many more rounds?"
            rows = [[Button(f"+{n}", set_callback("rounds", str(n))) for n in (1, 3, 5)]]
        elif which == "team":
            room = self.room(chat_id)
            team = store.room_team(room)
            names = [a["name"] for a in store.agents()
                     if a["kind"] not in HUMAN_KINDS and a["enabled"]]
            if not names:
                return await self.say(chat_id, "No agent seats registered yet.")
            head = ("Who is on the team here? Tap to add or drop.\n"
                    + (f"Now: **{', '.join(team)}**" if team else
                       "_Not set — a new meeting seats whoever is on the one you "
                       "are standing on._"))
            rows = [[Button(("✓ " if n in team else "") + n, set_callback("team", n))]
                    for n in names]
        elif which in {"nudge", "chair"}:
            if not slug:
                return await self.say(chat_id, "No topic here yet.")
            want_people = which == "chair"
            names = [r["agent"] for r in store.seats(int(store.topic(slug)["id"]))
                     if (r["kind"] in HUMAN_KINDS) == want_people]
            if not names:
                return await self.say(chat_id, "Nobody here to choose from.")
            head = "Who chairs this meeting?" if want_people else "Which seat should wake?"
            rows = [[Button(n, set_callback(which, n))] for n in names]
        try:
            await self.t.send(str(chat_id), head, buttons=rows)
        except Exception as exc:
            log.warning("chooser %s failed: %s", which, exc)

    async def send_picker(self, chat_id, *, message: Any = None) -> None:
        """The topic list as one button per row, edited in place after a tap."""
        here = self.topic_here(chat_id)
        rows = picker_rows(self.store.topics_for_room(self.room(chat_id)), here)
        if not rows:
            return await self.say(chat_id, "No topics yet — `/topic new <question>`.")
        keys = [[Button(label, pick_callback(tid))] for label, tid in rows]
        text = (f"**This chat is on** `{here}`" if here
                else "**This chat is not on a topic yet**")
        text += "\n\nTap one to move the room to it."
        try:
            if message is not None:
                return await self.t.edit(str(chat_id), message, text, buttons=keys)
            await self.t.send(str(chat_id), text, buttons=keys)
        except Exception as exc:
            # An edit that changes nothing is refused, and a picker that cannot
            # redraw must not take the tap down with it.
            log.warning("topic picker: %s", exc)

    async def ask_if_moved(self, chat_id, topic_id: int) -> None:
        """One tap, only when the chair wrote a position to compare against."""
        position = self.store.position(topic_id)
        if not position:
            return
        rows = [[Button("It changed my mind", shift_callback(True, topic_id)),
                 Button("I already thought so", shift_callback(False, topic_id))]]
        flat = " ".join(position.split())
        try:
            await self.t.send(str(chat_id), "You wrote a position before this "
                              f"started:\n_{flat}_\n\nDid the council move you?",
                              buttons=rows)
        except Exception as exc:
            log.warning("could not ask whether the council moved you: %s", exc)

    # --------------------------------------------------------------- taps

    async def decide(self, pid: int, seat: str, approve: bool, why: str) -> None:
        store = self.store
        store.decide(pid, seat, approve=approve, rationale=why, via=self.channel)
        store.audit(seat, "decide", {"proposal_id": pid, "approve": approve,
                                     "via": self.channel},
                    topic_id=int(store.proposal(pid)["topic_id"]))

    async def on_tap(self, tap: Tap) -> None:
        """A button press. The presser's own seat is what signs off -- not
        whoever the bot was started as."""
        store, t = self.store, self.t
        NotAuthorised, StoreError = self.NotAuthorised, self.StoreError
        chat, data = str(tap.chat_id), tap.data or ""

        async def refuse(text):
            await t.answer(tap, text[:180], alert=True)

        joining = parse_join(data)
        if joining is not None:
            action, pid = joining
            if not await self.allowed(chat):
                return await refuse("not this chat")
            presser = self.seat_of(chat, tap.user_id)
            # No host yet falls back to the person running the bot, whose
            # authority does not depend on this room being trustworthy.
            answers = store.room_host(self.room(chat)) or self.human
            if not presser or presser != answers:
                return await refuse(f"Only {answers} can answer that.")
            want = store.q1("SELECT * FROM pairings WHERE id = ?", (pid,))
            if want is None or want["channel"] != self.channel or str(want["chat_id"]) != chat:
                return await refuse("that request is gone")
            if want["status"] != "pending":
                return await refuse(f"already {want['status']}")
            try:
                if action == "no":
                    store.pair_deny(pid, presser)
                    await t.answer(tap, "refused")
                    return await self.say(chat, f"{want['display'] or pid} was not let in.")
                seat = store.seat_name_for(want["display"], fallback=f"guest{pid}")
                row = store.pair_approve(pid, seat, presser)
                store.claim_room(self.room(chat), presser)
            except (StoreError, NotAuthorised) as exc:
                return await refuse(str(exc))
            await t.answer(tap, f"{row['seat']} is in")
            return await self.say(chat, f"{row['display'] or row['user_id']} now "
                                        f"speaks as **{row['seat']}**, let in by {presser}.")

        if not await self.allowed(chat):
            return await refuse("not this chat")
        seat = self.seat_of(chat, tap.user_id)
        if not seat:
            return await refuse("You are not paired here — send /pair first.")

        moved = parse_shift(data)
        if moved is not None:
            did, topic_id = moved
            try:
                store.record_shift(topic_id, did, seat)
            except (StoreError, NotAuthorised) as exc:
                return await refuse(str(exc))
            try:
                await t.delete(chat, tap.message)
            except Exception:
                pass
            return await t.answer(tap, "noted")

        why_pick = parse_why(data)
        if why_pick is not None:
            approve, pid, reason = why_pick
            if reason is None:
                await t.answer(tap)
                prompt = await t.ask_reply(
                    chat, f"{'Approving' if approve else 'Rejecting'} #{pid}. "
                          f"Reply to this with why.")
                # The reason is part of the record, so it waits for one rather
                # than landing bare and being explained afterwards.
                self.pending[(chat, prompt)] = (pid, approve, seat)
                return
            try:
                await self.decide(pid, seat, approve, reason)
            except (StoreError, NotAuthorised) as exc:
                return await refuse(str(exc))
            await t.answer(tap, "signed off" if approve else "rejected")
            # The pump announces every decision wherever it was taken.
            return await self.ask_if_moved(chat, int(store.proposal(pid)["topic_id"]))

        chosen = parse_set(data)
        if chosen is not None:
            what, value = chosen
            slug = self.topic_here(chat)
            if what == "team":
                # A toggle computed from what the room holds, so tapping a name
                # twice puts it back.
                room = self.room(chat)
                on = list(store.room_team(room))
                on.remove(value) if value in on else on.append(value)
                try:
                    store.set_room_team(room, on, seat)
                except (StoreError, NotAuthorised) as exc:
                    return await refuse(str(exc))
                await t.answer(tap, ("dropped " if value not in on else "added ") + value)
                try:
                    await t.delete(chat, tap.message)
                except Exception:
                    pass
                return await self.send_choices(chat, "team", slug)
            board = self.board(slug, seat, chat)
            try:
                out = board.handle(command_for(what, value))
            finally:
                board.close()
            await t.answer(tap, value)
            return await self.say(chat, out or f"{what} → {value}")

        picked = parse_pick(data)
        if picked is not None:
            try:
                topic = store.topic(picked)
            except StoreError:
                return await refuse("that topic is gone")
            if not store.topic_visible_in(int(topic["id"]), self.room(chat)):
                return await refuse("that topic is gone")
            self.where[chat] = topic["slug"]
            store.set_room_topic(self.room(chat), topic["slug"])
            await t.answer(tap, f"now on {topic['slug']}")
            return await self.send_picker(chat, message=tap.message)

        parsed = parse_rule(data)
        if parsed is None:
            return await t.answer(tap)
        what, pid = parsed
        try:
            pr = store.proposal(pid)
        except StoreError:
            return await refuse("that proposal is gone")
        if what == "full":
            await t.answer(tap)
            return await self.say(chat, f"**proposal #{pid}** {pr['title']}\n\n{pr['body']}")
        if pr["status"] != "open":
            return await refuse(f"already {pr['status']}")
        await t.answer(tap, "noted — say why")
        approve = what == "ok"
        rows = [[Button(text, why_callback(approve, pid, i))]
                for i, text in enumerate(WHY_PRESETS[approve])]
        rows.append([Button("✎ Write my own", why_callback(approve, pid, WHY_OWN))])
        await t.send(chat, f"{'Approving' if approve else 'Rejecting'} #{pid} — why?",
                     buttons=rows)

    async def finish_ruling(self, m: Incoming) -> bool:
        """Complete a sign-off whose reason has just arrived. True if it was one."""
        key = (m.chat_id, m.reply_to) if m.reply_to is not None else None
        if key not in self.pending:
            return False
        pid, approve, seat = self.pending.pop(key)
        if self.seat_of(m.chat_id, m.user_id) != seat:
            await self.say(m.chat_id, "That sign-off was started by somebody else; "
                                      "it still needs their reason.")
            self.pending[key] = (pid, approve, seat)
            return True
        try:
            await self.decide(pid, seat, approve, (m.text or "").strip())
        except (self.StoreError, self.NotAuthorised) as exc:
            await self.say(m.chat_id, str(exc))
            return True
        await self.ask_if_moved(m.chat_id, int(self.store.proposal(pid)["topic_id"]))
        return True

    # ------------------------------------------------------------ talking

    async def on_message(self, m: Incoming) -> None:
        store, chat = self.store, m.chat_id
        if not (m.text or "").strip() or not await self.allowed(chat, m.user_id, m.private):
            return
        if await self.finish_ruling(m):
            return
        seat = self.seat_of(chat, m.user_id)
        if not seat:
            # An unknown sender must not be able to open topics or spend
            # anybody's subscription.
            who = m.name or m.user_id
            pid = store.pair_request(chat, m.user_id, who, self.channel)
            await self.say(chat, "You are not paired here yet.")
            return await self.say_join_request(chat, pid, who)
        line = self.t.addressed(m.text)
        if line is None:
            return
        if wants_picker(line):
            return await self.send_picker(chat)
        chooser = wants_choices(line)
        if chooser:
            return await self.send_choices(chat, chooser, self.topic_here(chat))
        want = proposal_ref(line)
        if want is not None:
            try:
                pr = store.proposal(want)
            except self.StoreError as exc:
                return await self.say(chat, str(exc))
            return await self.say_proposal(chat, pr)

        slug = self.topic_here(chat)
        if (not slug and not line.startswith("/")
                and store.topics_for_room(self.room(chat))):
            # Something to post and nowhere to post it: offer what exists.
            return await self.send_picker(chat)
        if slug:
            try:
                if store.seat_human(int(store.topic(slug)["id"]), seat):
                    await self.say(chat, f"_{seat} joined the council_")
            except self.StoreError:
                pass
        board = self.board(slug, seat, chat)
        try:
            out = board.handle(line)
            if board.topic:
                self.where[chat] = board.topic
                store.set_room_topic(self.room(chat), board.topic)
        finally:
            board.close()
        if out:
            await self.say(chat, out)

    # --------------------------------------------------------------- pump

    async def pump_once(self, cursor: int) -> int:
        """Send every board event after `cursor` to the rooms that may see it.
        Returns the new cursor, which only advances past an event once sent."""
        store = self.store
        targets = self.listeners()
        for ev in store.events_since(cursor, None):
            # A room hears about its own meetings and the unbound ones. A
            # person's own words are already in the room they typed them in.
            here = {c for c in targets
                    if store.topic_visible_in(ev.topic_id, self.room(c))
                    and ev.actor not in self.seats_in(c)}
            if ev.kind == "proposal" and ev.payload.get("action") == "opened":
                try:
                    pr = store.proposal(int(ev.payload["proposal_id"]))
                    for c in here:
                        await self.say_proposal(c, pr)
                except self.StoreError:
                    pass
            else:
                text = event_text(store, ev)
                if text:
                    for c in here:
                        await self.say(c, text)
            cursor = ev.id
        return cursor

    async def pump(self) -> None:
        """Board events into the chat, from `store.head()`, never replayed."""
        cursor = self.store.head()
        while True:
            try:
                cursor = await self.pump_once(cursor)
            except Exception:
                log.exception("event pump")
            await asyncio.sleep(2.0)
