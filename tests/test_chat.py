"""A council in a chat, driven through a transport that only records.

Everything `telegram.run` did lived in one closure no test could reach, and
five bugs in it were found by a person on a phone. `ChatHost` is that closure
with the transport made a parameter, so the same flows run here against a
`FakeTransport` -- pairing, sign-off by button and by reply, room isolation,
running a council -- without a token or a network.
"""

from __future__ import annotations

import asyncio

import pytest

from mooting.chat import Button, ChatHost, Incoming, Member, Tap, Transport
from mooting.drivers import FakeDriver
from mooting.store import connect
from mooting.telegram import (WHY_PRESETS, join_callback, pick_callback,
                              rule_callback, why_callback, WHY_OWN)


class FakeTransport(Transport):
    channel = "fake"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, list | None]] = []
        self.answers: list[tuple[str, bool]] = []
        self.files: list[tuple[str, str]] = []
        self.deleted: list = []
        self.edits: list = []
        self._n = 0

    async def send(self, chat_id, markdown, *, buttons=None, desk=False):
        self._n += 1
        self.sent.append((chat_id, markdown, buttons))
        return self._n

    async def edit(self, chat_id, message, markdown, *, buttons=None):
        self.edits.append((chat_id, message, markdown))

    async def delete(self, chat_id, message):
        self.deleted.append((chat_id, message))

    async def answer(self, tap, text="", alert=False):
        self.answers.append((text, alert))

    async def ask_reply(self, chat_id, text):
        return await self.send(chat_id, text)

    async def send_file(self, chat_id, name, data, caption):
        self.files.append((chat_id, name))

    def addressed(self, text):
        from mooting.telegram import addressed_here
        return addressed_here(text, "our_bot")

    # -- what a test reads

    def said(self, chat_id=None) -> list[str]:
        return [m for c, m, _ in self.sent if chat_id is None or c == chat_id]

    def buttons(self, chat_id=None) -> list[Button]:
        return [b for c, _, rows in self.sent if rows and (chat_id is None or c == chat_id)
                for row in rows for b in row]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "board.db"
    s = connect(path, init=True)
    s.add_agent("me", "human")
    for n in ("claude", "codex"):
        s.add_agent(n, n, driver="spawn")
    s.close()
    return path


@pytest.fixture
def host(db):
    store = connect(db)
    h = ChatHost(db, store, FakeTransport(), human="me")
    yield h
    store.close()


def msg(text, chat="100", user="1", name="Jeremy", **kw) -> Incoming:
    return Incoming(chat_id=chat, user_id=user, name=name, text=text, **kw)


def paired(host, chat="100", user="1") -> None:
    """Redeem the first-run code, which is how a real room starts."""
    run(host.route(msg(f"/pair {host.claim}", chat, user)))
    assert host.seat_of(chat, user) == "me"


# ------------------------------------------------------------------ pairing

def test_a_room_nobody_was_let_into_hears_nothing(host):
    run(host.route(msg("hello")))
    run(host.route(msg("/help")))
    assert host.t.sent == []


def test_the_first_run_code_pairs_the_host_and_binds_the_account(host):
    code = host.claim
    assert code
    paired(host)
    assert "You speak as **me** and host this room." in host.t.said("100")[-1]
    assert host.store.seat_for_identity("1", "fake") == "me"
    assert host.store.room_host(host.room("100")) == "me"
    # The code works once.
    run(host.route(msg(f"/pair {code}", chat="200", user="2")))
    assert host.seat_of("200", "2") is None


def test_a_stranger_is_asked_for_and_the_host_lets_them_in(host):
    paired(host)
    run(host.route(msg("hi all", user="2", name="Guest")))
    assert "You are not paired here yet." in host.t.said("100")
    let_in = next(b for b in host.t.buttons() if b.text.startswith("✓ Let"))

    # Only the host answers a join request.
    run(host.on_tap(Tap("100", "2", let_in.data)))
    assert host.t.answers[-1] == ("Only me can answer that.", True)

    run(host.on_tap(Tap("100", "1", let_in.data)))
    assert host.seat_of("100", "2") == "Guest"


def test_a_join_tap_for_another_rooms_request_is_refused(host):
    """A callback carries a pairing id, and a modified client can send any id.
    Answering a request that belongs to another room let one host seat people
    in a room they do not run."""
    paired(host)
    paired_elsewhere = host.store.pair_request("999", "7", "Mallory", "fake")
    run(host.on_tap(Tap("100", "1", join_callback("ok", paired_elsewhere))))
    assert host.t.answers[-1] == ("that request is gone", True)
    assert host.seat_of("999", "7") is None


def test_a_command_addressed_to_another_bot_is_left_alone(host):
    paired(host)
    before = len(host.t.sent)
    run(host.route(msg("/help@someone_elses_bot")))
    assert len(host.t.sent) == before
    run(host.route(msg("/help@our_bot")))
    assert "a council in this chat" in host.t.said()[-1]


def test_the_same_chat_id_on_two_channels_is_two_rooms(db):
    """Telegram and Discord ids are unrelated numbers. Filed under one name, a
    Discord channel numbered like a paired Telegram group would inherit it."""
    store = connect(db)
    try:
        tg = ChatHost(db, store, FakeTransport(), human="me")
        tg.t.channel = tg.channel = "telegram"
        paired(tg)
        other = FakeTransport()
        dc = ChatHost(db, store, other, human="me")
        run(dc.route(msg("hello")))
        assert other.sent == [], "a room paired on one channel answered on another"
    finally:
        store.close()


# ----------------------------------------------------------------- talking

def test_a_paired_person_opens_a_topic_and_talks(host):
    paired(host)
    run(host.route(msg("/topic new should we cap retries?")))
    slug = host.topic_here("100")
    assert slug
    run(host.route(msg("the gateway uses a fixed 30s")))
    bodies = [m["body"] for m in host.store.transcript(host.store.topic(slug)["id"])]
    assert "the gateway uses a fixed 30s" in bodies


# ---------------------------------------------------------------- sign-off

def open_proposal(host) -> int:
    paired(host)
    run(host.route(msg("/topic new cap retries?")))
    tid = int(host.store.topic(host.topic_here("100"))["id"])
    host.store.seat_human(tid, "me")
    assert {s["agent"] for s in host.store.seats(tid)} >= {"claude", "codex"}
    return host.store.propose(tid, "claude", "Cap at 6", "Stop after 6 attempts.")


def test_a_proposal_arrives_with_its_buttons_and_a_preset_signs_it_off(host):
    cursor = host.store.head()
    pid = open_proposal(host)
    run(host.pump_once(cursor))
    labels = [b.text for b in host.t.buttons("100")]
    assert "✓ Approve" in labels and "✗ Reject" in labels

    run(host.on_tap(Tap("100", "1", rule_callback("ok", pid))))
    presets = [b for b in host.t.buttons("100") if b.data.startswith(why_callback(True, pid, 0)[:4])]
    assert presets, "no reasons were offered as buttons"

    run(host.on_tap(Tap("100", "1", why_callback(True, pid, 0))))
    p = host.store.proposal(pid)
    assert (p["status"], p["decided_by"]) == ("approved", "me")
    assert p["rationale"] == WHY_PRESETS[True][0]


def test_writing_your_own_reason_waits_for_the_reply_and_only_yours(host):
    pid = open_proposal(host)
    host.store.add_agent("sam", "human")
    sam = host.store.pair_request("100", "2", "Sam", "fake")
    host.store.pair_approve(sam, "sam", "me")

    run(host.on_tap(Tap("100", "1", why_callback(False, pid, WHY_OWN))))
    prompt = host.t.sent[-1]
    assert "Reply to this with why." in prompt[1]
    prompt_ref = len(host.t.sent)

    # Somebody else replying does not complete my sign-off.
    run(host.route(msg("because I say so", user="2", name="Sam", reply_to=prompt_ref)))
    assert host.store.proposal(pid)["status"] == "open"
    assert "started by somebody else" in host.t.said()[-1]

    run(host.route(msg("six is past the consumer window", reply_to=prompt_ref)))
    p = host.store.proposal(pid)
    assert (p["status"], p["decided_by"], p["rationale"]) == (
        "rejected", "me", "six is past the consumer window")


def test_a_sign_off_from_the_chat_is_recorded_as_from_the_chat(host):
    pid = open_proposal(host)
    run(host.on_tap(Tap("100", "1", why_callback(True, pid, 1))))
    ev = [e for e in host.store.events_since(0, None)
          if e.kind == "remote" and e.payload.get("action") == "decide"][-1]
    assert (ev.actor, ev.payload["via"]) == ("me", "fake")


def test_a_stranger_cannot_sign_off_by_button(host):
    pid = open_proposal(host)
    run(host.on_tap(Tap("100", "9", why_callback(True, pid, 0))))
    assert host.t.answers[-1][1] is True
    assert host.store.proposal(pid)["status"] == "open"


# -------------------------------------------------------------- isolation

def test_a_room_hears_its_own_meetings_and_not_anothers(host):
    paired(host, chat="100")
    host.store.pair_approve(host.store.pair_request("200", "1", "Jeremy", "fake"),
                            "me", "me")
    run(host.route(msg("/topic new room one only", chat="100")))
    tid = int(host.store.topic(host.topic_here("100"))["id"])
    cursor = host.store.head()
    host.store.post(tid, "claude", "an argument for room one")
    run(host.pump_once(cursor))
    assert any("an argument for room one" in m for m in host.t.said("100"))
    assert not any("room one" in m for m in host.t.said("200"))


def test_the_picker_will_not_move_a_room_onto_anothers_topic(host):
    paired(host, chat="100")
    host.store.pair_approve(host.store.pair_request("200", "1", "Jeremy", "fake"),
                            "me", "me")
    run(host.route(msg("/topic new private to room two", chat="200")))
    theirs = int(host.store.topic(host.topic_here("200"))["id"])
    run(host.on_tap(Tap("100", "1", pick_callback(theirs))))
    assert host.t.answers[-1] == ("that topic is gone", True)
    assert host.topic_here("100") != host.topic_here("200")


# --------------------------------------------------------------- running

def test_run_drives_the_council_and_reports_why_it_stopped(db):
    store = connect(db)
    try:
        fake = FakeTransport()
        host = ChatHost(db, store, fake, human="me",
                        build_drivers=lambda s: {"claude": FakeDriver(s),
                                                 "codex": FakeDriver(s)})
        paired(host)

        async def go():
            await host.route(msg("/topic new cap retries?"))
            tid = int(store.topic(host.topic_here("100"))["id"])
            assert {s["agent"] for s in store.seats(tid)} >= {"claude", "codex"}
            store.set_rounds(tid, 1, "me")
            await host.route(msg("/run"))
            await asyncio.wait_for(host.running[tid], 10)
            return tid

        tid = run(go())
        said = [m["author"] for m in store.transcript(tid) if m["kind"] == "say"]
        assert {"claude", "codex"} <= set(said)
        assert any(m.startswith("_council stopped:") for m in fake.said("100"))
        assert not store.setting(f"{store.DRIVE_KEY}.{tid}"), "the drive claim was kept"
    finally:
        store.close()


def test_a_file_sent_to_the_chat_is_attached_to_the_topic(host):
    paired(host)
    run(host.route(msg("/topic new read this")))

    async def fetch():
        return b"retry budget: 6 attempts\n"

    run(host.route(msg("", file=("notes.txt", fetch), caption="the runbook")))
    assert "Attached **notes.txt**" in host.t.said()[-1]


def test_minutes_come_back_as_a_file(host):
    paired(host)
    run(host.route(msg("/topic new write it up")))
    run(host.route(msg("/minutes")))
    assert host.t.files and host.t.files[-1][1].endswith("-minutes.md")


def test_someone_added_by_the_host_is_let_in(host):
    paired(host)
    run(host.route(msg("", new_members=[Member("5", "Ana")])))
    assert host.seat_of("100", "5") == "Ana"


# ------------------------------------------------------- Telegram transport

class FakeBot:
    def __init__(self, refuse_html=False):
        self.calls, self.refuse_html = [], refuse_html

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        if self.refuse_html and parse_mode:
            raise ValueError("can't parse entities")
        self.calls.append((text, parse_mode, reply_markup))

        class Sent:
            message_id = len(self.calls)
        return Sent()


@pytest.fixture
def no_throttle(monkeypatch):
    from mooting import telegram

    async def instant(self):
        return None
    monkeypatch.setattr(telegram.Throttle, "wait", instant)


def test_telegram_splits_a_long_message_and_hangs_buttons_off_the_last(db, no_throttle):
    pytest.importorskip("aiogram")
    from mooting.telegram import LIMIT, TelegramTransport

    store = connect(db)
    try:
        bot = FakeBot()
        t = TelegramTransport(bot, store)
        body = "\n\n".join(f"paragraph {i} " + "x" * 400 for i in range(20))
        run(t.send("1", body, buttons=[[Button("✓ Approve", "r:ok:1")]]))
        assert len(bot.calls) > 1
        assert all(len(text) <= LIMIT for text, _, _ in bot.calls)
        assert [m is not None for _, _, m in bot.calls] == [False] * (len(bot.calls) - 1) + [True]
        assert bot.calls[-1][2].inline_keyboard[0][0].callback_data == "r:ok:1"
    finally:
        store.close()


def test_telegram_falls_back_to_plain_text_rather_than_losing_a_message(db, no_throttle):
    pytest.importorskip("aiogram")
    from mooting.telegram import TelegramTransport

    store = connect(db)
    try:
        bot = FakeBot(refuse_html=True)
        run(TelegramTransport(bot, store).send("1", "**bold** and `code`"))
        assert bot.calls == [("bold and code", None, None)]
    finally:
        store.close()
