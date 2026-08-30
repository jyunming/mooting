"""A council in a chat — the parts that do not need a bot token.

The renderer and the pairing fence are where this either works or quietly
ruins a council, so they are what is tested here. The transport is a thin
wrapper around them.
"""

from __future__ import annotations

import pytest

from mooting.store import NotAuthorised, StoreError, connect
from mooting.telegram import (LIMIT, Throttle, blocks, chunks, event_text,
                              plain)


# ---------------------------------------------------------------- rendering

def test_escapes_the_three_characters_html_mode_cares_about():
    """HTML mode was chosen over MarkdownV2 precisely because it is three
    characters and not eighteen. If they are not escaped, Telegram rejects the
    whole message and the seat's turn is simply lost."""
    out = " ".join(blocks("compare a < b & c > d"))
    assert "&lt;" in out and "&gt;" in out and "&amp;" in out
    assert "a < b" not in out


def test_a_table_survives_as_monospace():
    """There are no tables in Telegram. Agents produce them unprompted -- one
    appeared in the first real debate on this board -- and the alignment is the
    part that carried the meaning."""
    md = "| Option | Annual |\n|---|---|\n| Oil | 1750 |\n| Air | 1435 |"
    out = blocks(md)
    assert len(out) == 1 and out[0].startswith("<pre>")
    assert "| Oil | 1750 |" in out[0]


def test_fenced_code_keeps_its_language_and_is_not_re_read_as_markdown():
    md = "```python\nx = a_*_b  # **not bold**\n```"
    out = blocks(md)[0]
    assert 'class="language-python"' in out
    assert "<b>" not in out and "<i>" not in out, "markdown inside code was rendered"


def test_inline_markdown_becomes_tags():
    out = " ".join(blocks("**bold** and *italic* and `code`"))
    assert "<b>bold</b>" in out
    assert "<i>italic</i>" in out
    assert "<code>code</code>" in out


def test_bullets_and_quotes_survive():
    out = " ".join(blocks("- first\n- second\n\n> I withdraw one thing."))
    assert "• first" in out
    assert "<blockquote>I withdraw one thing.</blockquote>" in out


# ----------------------------------------------------------------- splitting

def test_nothing_exceeds_the_message_limit():
    """4096 is a hard ceiling; a longer message is refused outright."""
    md = "\n\n".join(f"paragraph {i} " + "word " * 200 for i in range(30))
    for piece in chunks(md):
        assert len(piece) <= LIMIT


def test_an_oversized_code_block_is_split_but_every_piece_stays_closed():
    """Half a `<pre>` is not a message. This is the split most likely to be got
    wrong, because it is the one a long log triggers."""
    pieces = chunks("```\n" + ("x" * 9000) + "\n```", limit=500)
    assert len(pieces) > 1
    for p in pieces:
        assert p.startswith("<pre>") and p.endswith("</pre>"), p[:60]
        assert len(p) <= 500


def test_splitting_happens_between_blocks_not_inside_tags():
    md = "**one**\n\n**two**\n\n**three**"
    for p in chunks(md, limit=20):
        assert p.count("<b>") == p.count("</b>"), p


def test_plain_is_a_real_fallback():
    """A reply that cannot be formatted must still arrive."""
    assert plain("<b>Direct answer</b> and <code>a &lt; b</code>") == \
        "Direct answer and a < b"


# ---------------------------------------------------------------- throttling

def test_the_throttle_respects_both_ceilings():
    """One per second per chat, twenty per minute in a group. A ten-round
    council with three seats is thirty replies, so this is load-bearing."""
    t = Throttle()
    assert t.delay(now=100.0) == 0.0
    t.record(100.0)
    assert t.delay(100.2) == pytest.approx(0.8, abs=0.01), "sent two in one second"

    burst = Throttle()
    for i in range(20):
        burst.record(200.0 + i * 0.001)
    assert burst.delay(200.5) > 50.0, "twenty in a minute was not enforced"


# ------------------------------------------------------------------- pairing

@pytest.fixture()
def board(tmp_path):
    s = connect(tmp_path / "board.db", init=True)
    s.add_agent("jeremy", "human")
    s.add_agent("santa", "claude", driver="spawn")
    yield s
    s.close()


def test_an_unknown_sender_can_do_nothing_until_approved(board):
    """openclaw's rule, and the right one: an unknown sender is inert until
    somebody who already has authority approves them. A chat has no operating
    system to ask who is speaking."""
    assert board.seat_for_chat("-100", "42") is None

    pid = board.pair_request("-100", "42", "Someone")
    assert board.seat_for_chat("-100", "42") is None, "a request granted access"

    board.pair_approve(pid, "jeremy", "jeremy")
    assert board.seat_for_chat("-100", "42") == "jeremy"


def test_a_person_cannot_be_paired_onto_an_agent_seat(board):
    """It would let them speak as an agent, and put a non-human name against
    something only a human may do."""
    pid = board.pair_request("-100", "43", "Sneaky")
    with pytest.raises(NotAuthorised):
        board.pair_approve(pid, "santa", "jeremy")
    assert board.seat_for_chat("-100", "43") is None


def test_a_denied_request_stays_denied(board):
    pid = board.pair_request("-100", "44", "Nope")
    board.pair_deny(pid, "jeremy")
    assert board.seat_for_chat("-100", "44") is None


def test_the_same_person_in_another_chat_is_a_separate_question(board):
    """Approval is per room. Being trusted in one council is not being trusted
    in another."""
    pid = board.pair_request("-100", "42", "Someone")
    board.pair_approve(pid, "jeremy", "jeremy")
    assert board.seat_for_chat("-999", "42") is None


def test_approving_something_that_was_never_requested_is_refused(board):
    with pytest.raises(StoreError):
        board.pair_approve(9999, "jeremy", "jeremy")


# --------------------------------------------------------------- the pump

def test_system_noise_stays_out_of_the_chat(board):
    """Round markers and pause notices are terminal furniture. In a chat they
    are twenty messages a council nobody wanted, against a 20/minute ceiling."""
    tid = board.open_topic("t", "T", "T", "jeremy", seats=("santa", "jeremy"))
    board.post(tid, "mooting", "--- round 2 of 10 ---", kind="system",
               count_turn=False)
    board.post(tid, "santa", "**a real argument**", count_turn=False)

    said = [event_text(board, e) for e in board.events_since(0, tid)]
    kept = [s for s in said if s]
    assert any("a real argument" in s for s in kept)
    assert not any("round 2 of 10" in s for s in kept), "system noise reached the chat"


def test_a_seat_is_derived_from_the_persons_own_name(board):
    """Approving somebody should not also mean inventing a name for them."""
    fresh = board.seat_name_for("Wilhelmina Vasquez")
    assert fresh == "Wilhelmina"
    assert board.seat_name_for("Wilhelmina Vasquez") == fresh, "made a second seat"

    # and somebody whose name already has a human seat gets that seat, whatever
    # the capitalisation -- `Jeremy` beside `jeremy` would be two of one person
    assert board.seat_name_for("Jeremy Chen") == "jeremy"


def test_a_derived_seat_never_collides_with_an_agent(board):
    """`Santa` beside the agent `santa` is two seats one letter apart, and an
    @mention could mean either."""
    got = board.seat_name_for("Santa Claus")
    assert got.lower() != "santa", got
    assert board.agent("santa")["kind"] == "claude", "the agent seat was taken over"
    assert board.agent(got)["kind"] == "human"


def test_a_derived_seat_is_a_human_seat_so_it_may_rule(board):
    """The whole point of pairing is to produce someone who can rule. A seat
    that is not human cannot, and `pair_approve` would refuse it."""
    name = board.seat_name_for("A Colleague")
    assert board.is_human(name)
    pid = board.pair_request("-100", "77", "A Colleague")
    assert board.pair_approve(pid, name, "jeremy")["seat"] == name


def test_a_seat_name_is_never_one_letter(board):
    """"A Colleague" gave the seat `A`, which is nobody."""
    assert board.seat_name_for("A Colleague") == "AColleague"
    assert board.seat_name_for("Bo Li") == "BoLi"


def test_a_non_latin_name_stays_a_name(board):
    """`slugify` keeps non-Latin scripts on purpose, and a name is no different.
    An ASCII-only filter turns such a name into `guest`."""
    assert board.seat_name_for("Ясна Ковач") == "Ясна"
    assert board.seat_name_for("Δ Κ") == "ΔΚ"


def test_terminal_colour_never_reaches_a_chat(tmp_path):
    """The console writes for a terminal. In Telegram an escape code is not
    invisible, it is literal noise: `[2mnow on ...` is what arrives."""
    from mooting.telegram import ANSI, ChatBoard

    s = connect(tmp_path / "board.db", init=True)
    s.add_agent("me", "human")
    s.close()

    b = ChatBoard(tmp_path / "board.db", None, "me")
    try:
        out = b.handle("/topic new should we cap retries?")
    finally:
        b.close()
    assert ANSI.search(out) is None, f"escape codes reached the chat: {out!r}"
    # not only the bracket form: a bare ESC left behind is still a stray byte
    assert chr(27) not in out, f"an escape byte survived: {out!r}"


def test_the_chat_stays_on_its_topic_between_messages(tmp_path):
    """A council spans many messages. Rebuilding per message forgot the last
    one, so `/topic agenda` answered "no topic yet" about a topic just made."""
    from mooting.telegram import ChatBoard

    s = connect(tmp_path / "board.db", init=True)
    s.add_agent("me", "human")
    s.close()

    first = ChatBoard(tmp_path / "board.db", None, "me")
    try:
        first.handle("/topic new should we cap retries?")
        slug = first.topic
    finally:
        first.close()
    assert slug, "the topic it just opened was not remembered"

    # the next message arrives on a fresh board, as it does in the bot
    second = ChatBoard(tmp_path / "board.db", slug, "me")
    try:
        out = second.handle("/topic agenda cap the retries; jitter")
    finally:
        second.close()
    assert "no topic" not in out.lower(), out
    assert "cap the retries" in out


# ------------------------------------------------------- failing to start

def test_every_way_a_token_can_fail_gets_a_sentence():
    """A stack trace through aiogram's internals is the least useful possible
    answer to "my token is wrong", and that is the commonest first run there
    is. Telegram answers 401 for a wrong token and 404 for a revoked one, and
    a token that is not even shaped like one fails earlier still, inside
    `Bot()`. All three used to traceback; only the first was handled."""
    from aiogram.exceptions import (TelegramNetworkError, TelegramNotFound,
                                    TelegramUnauthorizedError)
    from aiogram.utils.token import TokenValidationError

    from mooting.telegram import explain_start_failure as explain

    said = explain(TokenValidationError("Token is invalid!"))
    assert said and "does not look like a bot token" in said[0]

    for exc in (TelegramUnauthorizedError(method=None, message="Unauthorized"),
                TelegramNotFound(method=None, message="Not Found")):
        said = explain(exc)
        assert said, f"{type(exc).__name__} produced no explanation"
        assert "will not accept that token" in said[0], said

    said = explain(TelegramNetworkError(method=None, message="down"))
    assert said and "Could not reach Telegram" in said[0]

    # and anything that is not Telegram's fault is left to raise
    assert explain(ValueError("something else")) is None


# ------------------------------------------------------- remembering a token

def test_the_token_is_asked_for_once(tmp_path, monkeypatch, capsys):
    """A bot you have to re-authorise every time is a bot you stop using."""
    from mooting.cli import main

    db = tmp_path / "board.db"
    s = connect(db, init=True)
    s.add_agent("me", "human")
    s.close()

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("MOOTING_TELEGRAM_TOKEN", raising=False)

    # nothing saved: it says so, and says it will only ask once
    assert main(["--db", str(db), "--as", "me", "telegram"]) == 1
    err = capsys.readouterr().err
    assert "need a bot token, once" in err
    assert "@BotFather" in err

    # a token Telegram has accepted is remembered by `run`, not by the CLI --
    # so simulate what run does, then check the second call needs no token
    b = connect(db)
    b.set_setting("telegram.token", "8123:AAFpretend-this-one-worked")
    b.close()

    seen = {}

    def fake_run(db_, *, bot_token, chats, human, topic, remember):
        seen.update(token=bot_token, remember=remember, chats=chats)
        return 0

    import mooting.telegram as tg
    monkeypatch.setattr(tg, "run", fake_run)
    assert main(["--db", str(db), "--as", "me", "telegram"]) == 0
    assert seen["token"] == "8123:AAFpretend-this-one-worked"
    assert seen["remember"] is False, "re-saved a token it had just read back"
    assert "remembered from a previous run" in capsys.readouterr().out


def test_a_token_telegram_rejected_is_not_remembered(tmp_path, monkeypatch):
    """Saving before the first call meant a bad token was kept, and every later
    run failed the same way with nothing to explain it."""
    from mooting.cli import main

    db = tmp_path / "board.db"
    s = connect(db, init=True)
    s.add_agent("me", "human")
    s.close()

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("MOOTING_TELEGRAM_TOKEN", raising=False)

    import mooting.telegram as tg
    # `run` returning non-zero is what a refused token looks like; it saves
    # nothing, because saving happens inside `run` after Telegram accepts.
    monkeypatch.setattr(tg, "run", lambda *a, **k: 1)
    assert main(["--db", str(db), "--as", "me", "telegram", "--token", "1:bad"]) == 1

    b = connect(db)
    try:
        assert b.setting("telegram.token") is None, "a refused token was kept"
    finally:
        b.close()


def test_forgetting_the_token_leaves_the_board_alone(tmp_path, monkeypatch):
    from mooting.cli import main

    db = tmp_path / "board.db"
    s = connect(db, init=True)
    s.add_agent("me", "human")
    s.set_setting("telegram.token", "8123:AAFsomething")
    tid = s.open_topic("t", "T", "T", "me", seats=("me",))
    s.close()

    assert main(["--db", str(db), "--as", "me", "telegram", "--forget-token"]) == 0
    b = connect(db)
    try:
        assert b.setting("telegram.token") is None
        assert len(b.topics()) == 1, "forgetting a token touched the council"
    finally:
        b.close()


# ------------------------------------------------------- ruling from a chat

def test_asking_for_a_proposal_by_number_gets_the_buttons_back():
    """The pump starts at the board's head and never replays, so a proposal
    opened before the bot started -- or while the council ran at the terminal --
    could never show its buttons. Asking for it by number is the way back."""
    from mooting.telegram import proposal_ref

    assert proposal_ref("/proposals 3") == 3
    assert proposal_ref("/proposal 3") == 3
    assert proposal_ref("/proposals #3") == 3
    # Telegram appends the bot name to commands in group chats.
    assert proposal_ref("/proposals@mooting_bot 12") == 12
    assert proposal_ref("  /PROPOSALS 7  ") == 7

    # The bare listing keeps its plain-text form, and neighbouring commands are
    # not swallowed.
    assert proposal_ref("/proposals") is None
    assert proposal_ref("/proposals all") is None
    assert proposal_ref("/propose something") is None
    assert proposal_ref("hello") is None


def test_a_button_carries_which_proposal_it_meant():
    """`/approve 3` typed from memory on a phone is how a ruling lands on the
    wrong proposal. The button carries the id, so it cannot."""
    from mooting.telegram import parse_rule, rule_callback

    for action in ("ok", "no", "full"):
        data = rule_callback(action, 1234)
        assert len(data.encode()) <= 64, "over Telegram's callback_data limit"
        assert parse_rule(data) == (action, 1234)

    # anything that is not one of ours is left alone rather than guessed at
    for junk in ("", "rule:", "rule:ok", "rule:maybe:1", "rule:ok:abc",
                 "other:ok:1", "rule:ok:1:2"):
        assert parse_rule(junk) is None, junk

    with pytest.raises(ValueError):
        rule_callback("maybe", 1)


def test_a_ruling_from_chat_is_the_pressers_own(board):
    """Whoever tapped the button is not necessarily whoever the bot runs as, and
    a ruling recorded under the wrong name is worse than no ruling."""
    board.add_agent("alice", "human")
    tid = board.open_topic("t", "T", "T", "jeremy", seats=("santa", "jeremy", "alice"))
    pid = board.propose(tid, "santa", "Cap at 6", "body")

    # both are paired in the same room, as themselves
    for user, seat in (("42", "jeremy"), ("77", "alice")):
        board.pair_approve(board.pair_request("-100", user, seat), seat, "jeremy")
    assert board.seat_for_chat("-100", "42") == "jeremy"
    assert board.seat_for_chat("-100", "77") == "alice"

    # alice presses, so alice rules
    board.decide(pid, board.seat_for_chat("-100", "77"), approve=True,
                 rationale="capped at 6")
    assert board.proposal(pid)["status"] == "approved"
    decisions = [e for e in board.events_since(0, tid) if e.kind == "decision"]
    assert decisions and decisions[-1].actor == "alice", \
        "the ruling was attributed to the wrong person"


def test_an_unpaired_tap_cannot_rule(board):
    """Pairing is the fence, and a button does not get to skip it."""
    tid = board.open_topic("t", "T", "T", "jeremy", seats=("santa", "jeremy"))
    pid = board.propose(tid, "santa", "Cap at 6", "body")

    assert board.seat_for_chat("-100", "9999") is None
    # which is what the callback handler checks before it reaches `decide`
    assert board.proposal(pid)["status"] == "open"


def test_a_second_person_can_speak_not_only_rule(board):
    """`decide` asks only whether you are human; `post` requires a seat on the
    topic. Pairing granted the first and not the second, so the second person in
    a room could approve a plan and not be able to say why."""
    from mooting.store import NotAuthorised

    board.add_agent("alice", "human")
    tid = board.open_topic("t", "T", "T", "jeremy", seats=("santa", "jeremy"))

    assert board.seat(tid, "alice") is None
    assert board.seat_human(tid, "alice") is True, "the seat was not granted"
    assert board.seat_human(tid, "alice") is False, "granted twice"

    board.post(tid, "alice", "what about the windows?")
    assert any(m["author"] == "alice" for m in board.transcript(tid))

    # seating an agent is a decision about whose subscription gets spent
    with pytest.raises(NotAuthorised):
        board.seat_human(tid, "santa")
