"""The Discord transport, without Discord.

The host's behaviour is covered in `test_chat.py` and is the same here. What
is Discord's own is the rendering (2000 characters, fences that must close,
no tables), the five-by-five button grid, the DM channel that is not the
person's id, and which taps a person alone should see.
"""

from __future__ import annotations

import asyncio

import pytest

discord = pytest.importorskip("discord")

from mooting.chat import Button, ChatHost, Incoming, Tap          # noqa: E402
from mooting.discord_bot import (LIMIT, DiscordTransport, chunks,  # noqa: E402
                                 incoming, pack, with_mentions)
from mooting.store import connect                                  # noqa: E402
from mooting.telegram import rule_callback, why_callback            # noqa: E402


def run(coro):
    return asyncio.run(coro)


# -------------------------------------------------------------- rendering

def test_a_long_reply_is_split_under_the_limit_between_paragraphs():
    md = "\n\n".join(f"paragraph {i}: " + "word " * 80 for i in range(12))
    parts = chunks(md)
    assert len(parts) > 1
    assert all(len(p) <= LIMIT for p in parts)
    assert "".join(parts).count("paragraph") == 12


def test_a_cut_code_block_is_closed_and_reopened_every_time():
    """Half a fence turns everything after it into code."""
    code = "```python\n" + "\n".join(f"line_{i} = {i}" for i in range(400)) + "\n```"
    parts = chunks(code)
    assert len(parts) > 1
    for p in parts:
        assert len(p) <= LIMIT
        assert p.startswith("```python\n") and p.endswith("\n```")


def test_a_table_arrives_as_a_code_block():
    md = "before\n\n| seat | stance |\n|---|---|\n| Algae | object |\n\nafter"
    assert chunks(md) == ["before\n\n```\n| seat | stance |\n|---|---|\n"
                          "| Algae | object |\n```\n\nafter"]


def test_one_huge_line_is_cut_at_a_word():
    parts = chunks("word " * 1000)
    assert all(len(p) <= LIMIT for p in parts)
    assert all(not p.endswith("wor") for p in parts)


def test_a_picker_drawn_one_per_row_is_packed_five_across():
    rows = [[Button(f"topic {i}", f"p:{i}")] for i in range(12)]
    packed = pack(rows)
    assert [len(r) for r in packed] == [5, 5, 2]


def test_more_than_twenty_five_buttons_are_dropped_not_refused():
    packed = pack([[Button(str(i), str(i))] for i in range(40)])
    assert sum(len(r) for r in packed) == 25 and len(packed) == 5


def test_a_bound_account_is_mentioned_and_nobody_else_is(tmp_path):
    store = connect(tmp_path / "b.db", init=True)
    try:
        store.add_agent("jeremy", "human")
        store.bind_identity("jeremy", "4242", "discord")
        out = with_mentions(store, "@jeremy what is the timeout? cc @Santa")
        assert out == "<@4242> what is the timeout? cc @Santa"
    finally:
        store.close()


# ------------------------------------------------------------- transport

class FakeMessage:
    def __init__(self, id):
        self.id = id


class FakeChannel:
    def __init__(self, id):
        self.id, self.sent, self.guild = id, [], None

    async def send(self, content, **kw):
        self.sent.append((content, kw))
        return FakeMessage(1000 + len(self.sent))

    async def typing(self):
        return None


class FakeUser:
    def __init__(self, id):
        self.id = id
        self.dm = FakeChannel(99_000 + id)

    async def create_dm(self):
        return self.dm


class FakeClient:
    user = None

    def __init__(self, channels=(), users=()):
        self.channels = {c.id: c for c in channels}
        self.users = {u.id: u for u in users}

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def fetch_channel(self, cid):
        raise discord.NotFound(type("R", (), {"status": 404, "reason": "nf"})(), "no")

    async def fetch_user(self, uid):
        return self.users[uid]


@pytest.fixture
def store(tmp_path):
    s = connect(tmp_path / "board.db", init=True)
    s.add_agent("me", "human")
    s.add_agent("claude", "claude", driver="spawn")
    yield s
    s.close()


def test_buttons_hang_off_the_last_piece_and_never_ping_everyone(store):
    chan = FakeChannel(10)
    t = DiscordTransport(FakeClient([chan]), store)
    body = "\n\n".join("x" * 1500 for _ in range(3)) + " @everyone"
    run(t.send("10", body, buttons=[[Button("✓ Approve", "r:ok:1"),
                                     Button("✗ Reject", "r:no:1")]]))
    assert len(chan.sent) == 3
    assert [("view" in kw) for _, kw in chan.sent] == [False, False, True]
    view = chan.sent[-1][1]["view"]
    styles = [item.style for item in view.children]
    assert styles == [discord.ButtonStyle.success, discord.ButtonStyle.danger]
    assert all(kw["allowed_mentions"].everyone is False for _, kw in chan.sent)


def test_a_private_room_filed_under_the_person_reaches_their_dm(store):
    """The board files a private room under the account id. In Discord that
    id is not a channel, so sending to it has to find the DM."""
    person = FakeUser(7)
    t = DiscordTransport(FakeClient(users=[person]), store)
    run(t.send("7", "only for you"))
    assert person.dm.sent[0][0] == "only for you"


def test_a_refusal_is_shown_only_to_whoever_pressed(store):
    calls = []

    class Response:
        def is_done(self):
            return False

        async def send_message(self, text, ephemeral=False):
            calls.append((text, ephemeral))

        async def defer(self):
            calls.append(("defer", None))

    handle = type("I", (), {"response": Response()})()
    t = DiscordTransport(FakeClient(), store)
    run(t.answer(Tap("1", "2", "x", handle=handle), "Only me can answer that.", alert=True))
    run(t.answer(Tap("1", "2", "x", handle=handle)))
    assert calls == [("Only me can answer that.", True), ("defer", None)]


def test_a_dm_arrives_filed_under_the_person_with_its_reply(store):
    class DM(discord.DMChannel):
        def __init__(self):
            pass

    author = type("A", (), {"id": 7, "display_name": "Jeremy", "name": "jeremy"})()
    msg = type("M", (), {"author": author, "channel": DM(), "content": "why not",
                         "attachments": [],
                         "reference": type("R", (), {"message_id": 55})()})()
    got = incoming(msg)
    assert (got.chat_id, got.user_id, got.private, got.reply_to) == ("7", "7", True, 55)


# ------------------------------------------------- the host, over Discord

def test_a_council_signs_off_from_discord_as_from_discord(tmp_path):
    db = tmp_path / "board.db"
    s = connect(db, init=True)
    s.add_agent("me", "human")
    s.add_agent("claude", "claude", driver="spawn")
    s.close()
    store = connect(db)
    try:
        chan = FakeChannel(10)
        t = DiscordTransport(FakeClient([chan]), store)
        host = ChatHost(db, store, t, human="me")
        me = dict(chat_id="10", user_id="1", name="Jeremy")
        run(host.route(Incoming(text=f"/pair {host.claim}", **me)))
        assert store.seat_for_identity("1", "discord") == "me"
        assert store.seat_for_identity("1", "telegram") is None

        run(host.route(Incoming(text="/topic new cap retries?", **me)))
        tid = int(store.topic(host.topic_here("10"))["id"])
        pid = store.propose(tid, "claude", "Cap at 6", "Stop after 6.")

        class Response:
            def is_done(self):
                return False

            async def send_message(self, *a, **k):
                pass

            async def defer(self):
                pass

        handle = type("I", (), {"response": Response()})()
        run(host.on_tap(Tap("10", "1", rule_callback("ok", pid), handle=handle)))
        run(host.on_tap(Tap("10", "1", why_callback(True, pid, 0), handle=handle)))
        p = store.proposal(pid)
        assert (p["status"], p["decided_by"]) == ("approved", "me")
        ev = [e for e in store.events_since(0, None)
              if e.kind == "remote" and e.payload.get("action") == "decide"][-1]
        assert ev.payload["via"] == "discord"
    finally:
        store.close()


def test_discord_counts_as_another_machine_for_the_execute_window():
    """J1: a sign-off typed on this machine while a seat executes is refused,
    and a chat account is the way out because a seat does not hold one."""
    from mooting.store import ON_ANOTHER_MACHINE
    assert "discord" in ON_ANOTHER_MACHINE
