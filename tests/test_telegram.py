"""A council in a chat — the parts that do not need a bot token.

The renderer and the pairing fence are where this either works or quietly
ruins a council, so they are what is tested here. The transport is a thin
wrapper around them.
"""

from __future__ import annotations

import pathlib
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

def test_a_decision_reaches_the_chat_however_it_was_taken(board):
    """A proposal approved at the terminal used to be invisible from a phone:
    `event_text` rendered messages and new proposals and dropped decisions, so
    somebody following along watched a proposal arrive and never learned what
    happened to it."""
    from mooting.telegram import event_text

    tid = board.open_topic("t", "T", "b", "jeremy", seats=["jeremy", "santa"])
    pid = board.propose(tid, "santa", "Cap retries at 6", "body")
    head = board.head()
    board.decide(pid, "jeremy", approve=True, rationale="agreed, cap at 6")

    said = [event_text(board, ev) for ev in board.events_since(head, tid)]
    said = [x for x in said if x]
    assert any("approved" in x and "jeremy" in x for x in said), said
    assert any("agreed, cap at 6" in x for x in said), said


def test_a_decision_with_no_reason_still_announces(board):
    """`/approve 4` with no words is legal, and used to render a trailing dash."""
    from mooting.telegram import event_text

    tid = board.open_topic("t2", "T2", "b", "jeremy", seats=["jeremy", "santa"])
    pid = board.propose(tid, "santa", "Something", "body")
    head = board.head()
    board.decide(pid, "jeremy", approve=False, rationale="")

    said = [x for x in (event_text(board, ev)
                        for ev in board.events_since(head, tid)) if x]
    line = next(x for x in said if "rejected" in x)
    assert not line.rstrip().endswith("—"), line


def test_nudging_a_mention_does_not_die_in_a_thread(tmp_path):
    """`/nudge @Santa` is what a phone gives you -- Telegram completes a mention
    with its `@` attached. The seat is `Santa`, so the wake raised
    `@Santa holds no seat on topic 5` inside a daemon thread, where nobody was
    listening: the chat said "waking @Santa..." and then stayed silent for ever.
    Seen in a live session, not in a test."""
    from mooting.store import connect
    from mooting.telegram import ChatBoard

    db = tmp_path / "b.db"
    st = connect(db, init=True)
    st.add_agent("Jeremy", "human")
    st.add_agent("Santa", "claude", driver="spawn")
    st.open_topic("t", "T", "b", "Jeremy", seats=["Jeremy", "Santa"])
    st.close()

    chat = ChatBoard(db, "t", "Jeremy")
    try:
        # The `@` is tolerated, and the seat resolves whatever the case.
        assert chat.console._seat_named("@Santa") == "Santa"
        assert chat.console._seat_named("santa") == "Santa"
        assert chat.console._seat_named("Santa") == "Santa"

        # A name that is not here says so, rather than raising out of sight.
        assert chat.console._seat_named("@Nobody") is None
    finally:
        chat.close()


def test_nudging_an_unknown_seat_answers_in_the_chat(tmp_path):
    """The failure has to arrive where the person is looking."""
    from mooting.store import connect
    from mooting.telegram import ChatBoard

    db = tmp_path / "b2.db"
    st = connect(db, init=True)
    st.add_agent("Jeremy", "human")
    st.add_agent("Santa", "claude", driver="spawn")
    st.open_topic("t", "T", "b", "Jeremy", seats=["Jeremy", "Santa"])
    st.close()

    chat = ChatBoard(db, "t", "Jeremy")
    try:
        out = chat.handle("/nudge @Nobody")
        assert "Nobody" in out and "Santa" in out, out
    finally:
        chat.close()


def test_a_stop_notice_stays_on_one_line_and_italicises():
    """Seen on a phone: the pause notice arrived with literal underscores around
    it and stopped mid-word at `tapeou_`.

    Two causes, one symptom. The reason embedded 200 raw characters of somebody
    else's turn, newlines and all; and Telegram-HTML is assembled per line, so an
    italic span opening in one paragraph and closing in another matches nothing
    and both underscores survive as text."""
    from mooting.supervisor import snippet
    from mooting.telegram import blocks

    question = ("@Santa Direct response on both points:\n\n"
                "1. On hyperscalers/fabless: I concede that fabless giants "
                "(Apple, Nvidia, Google) do not run production mask "
                "calibration - the foundry owns the final tapeout and the "
                "OPC recipes, which is the part that carries the liability.")
    reason = f"Kevin is waiting on you: {snippet(question)}"

    assert "\n" not in reason, "a multi-line reason breaks the italics"

    out = blocks(f"_council stopped: {reason}_")
    assert len(out) == 1, out
    assert out[0].startswith("<i>") and out[0].endswith("</i>"), out[0]
    assert "_" not in out[0], "an underscore reached the chat as text"


def test_a_snippet_is_cut_at_a_word_not_through_one():
    """`tapeou` is what a blind slice gives you."""
    from mooting.supervisor import snippet

    assert snippet("short enough already") == "short enough already"

    long = "word " * 100
    got = snippet(long, limit=40)
    assert got.endswith("…")
    assert len(got) <= 41
    assert not got.rstrip("…").endswith("wor"), got

    # Whitespace of every kind collapses, so the result is one line.
    assert snippet("a\n\nb   c\td") == "a b c d"


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

    # Jeremy opened this meeting, so alice may argue in it but not close it.
    with pytest.raises(NotAuthorised):
        board.decide(pid, board.seat_for_chat("-100", "77"), approve=True,
                     rationale="capped at 6")

    # handed the chair, alice presses and alice rules
    board.set_chair(tid, "alice", "jeremy")
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


# ------------------------------------------------------------- topic picker
#
# Switching used to be `/topic switch <slug>`: an identifier retyped from
# memory on a phone keyboard, where a near miss moves the whole room somewhere
# nobody meant. These cover the parts that decide what a tap does, which are
# pure and need no bot.


def test_a_bare_topic_command_asks_for_the_picker_and_a_verb_does_not():
    from mooting.telegram import wants_picker

    assert wants_picker("/topic")
    assert wants_picker("/topics")
    assert wants_picker("  /Topics  ")
    assert wants_picker("/topic@council_bot")
    # A verb still means what it always did.
    assert not wants_picker("/topic new should we cap retries")
    assert not wants_picker("/topic switch retries")
    assert not wants_picker("/topical")


def test_the_picker_marks_where_the_room_is_and_shortens_long_titles():
    from mooting.telegram import picker_rows

    rows = [
        {"id": 3, "slug": "retries", "status": "open",
         "title": "Should failed webhook deliveries use exponential backoff"},
        {"id": 2, "slug": "aircon", "status": "paused", "title": "Aircon"},
        {"id": 1, "slug": "old", "status": "resolved", "title": "Old thing"},
    ]
    labels = [lbl for lbl, _ in picker_rows(rows, "retries")]

    assert labels[0].startswith("●")
    assert "●" not in labels[1]
    assert labels[1].startswith("⏸")
    assert labels[2].startswith("✓")
    # A button is one line on a phone, so a long title is cut at a word.
    assert len(labels[0]) <= 40
    assert labels[0].endswith("…") and not labels[0].endswith(" …")


def test_every_picker_button_carries_its_own_topic_id():
    """A chat scrolls. A button meaning "the third one" would drift with it."""
    from mooting.telegram import parse_pick, pick_callback, picker_rows

    rows = [{"id": 41, "slug": "a", "status": "open", "title": "A"},
            {"id": 7, "slug": "b", "status": "open", "title": "B"}]
    picked = [parse_pick(pick_callback(tid)) for _, tid in picker_rows(rows, "a")]

    assert picked == [41, 7]
    assert all(len(pick_callback(tid).encode("utf-8")) <= 64
               for _, tid in picker_rows(rows, None))
    # A sign-off button is not a picker button, and neither is anything else.
    assert parse_pick("rule:ok:3") is None
    assert parse_pick("") is None
    assert parse_pick("pick:notanumber") is None


def test_the_picker_shows_at_most_one_screen_of_topics():
    from mooting.telegram import PICKER_LIMIT, picker_rows

    many = [{"id": i, "slug": f"t{i}", "status": "open", "title": f"T{i}"}
            for i in range(40)]
    assert len(picker_rows(many, None)) == PICKER_LIMIT


def test_the_help_a_chat_receives_is_the_one_that_is_maintained():
    """`HELP` was defined twice, and the second copy won.

    The live text was the older one, which had lost the line telling somebody
    how to ask to join — the single thing a person who is not paired needs.
    Editing the first copy changed nothing, silently.
    """
    import mooting.telegram as tg

    source = pathlib.Path(tg.__file__).read_text(encoding="utf-8")
    assert source.count("\nHELP = (") == 1, "HELP is defined more than once"
    assert "ask to join" in tg.HELP
    assert "/topics" in tg.HELP


def test_a_chat_still_pointing_at_a_deleted_topic_can_open_a_new_one(tmp_path):
    """Found live: `/reset` cleared the board and the chat went completely mute.

    The room kept standing on the topic it was on. Building the session for the
    next message raised in the constructor, before any command was dispatched,
    so nothing answered at all — including `/topic new`, which was the only way
    back. Two people each read it as "I am not allowed to create a topic".
    """
    from mooting.store import connect
    from mooting.telegram import ChatBoard

    db = tmp_path / "board.db"
    s = connect(db, init=True)
    s.add_agent("Jeremy", "human")
    s.add_agent("Santa", "claude", driver="spawn")
    s.open_topic("gone", "Gone", "brief", "Jeremy", seats=("Jeremy", "Santa"))
    s.clear_topics()
    s.close()

    board = ChatBoard(db, "gone", "Jeremy")
    try:
        assert board.topic is None, "still standing on a topic that is not there"
        out = board.handle("/topic new can we open one after a reset")
        assert "can-we-open-one-after-a-reset" in out
        assert board.topic == "can-we-open-one-after-a-reset"
    finally:
        board.close()


def test_every_command_in_the_menu_actually_exists(tmp_path):
    """The menu is a promise: a thumb taps it and something has to happen.

    `/attach` sat in the help and the Telegram menu for weeks while the console
    answered "unknown /attach", because nothing checked that an advertised
    command was a reachable one. Driving each entry is the check — a command
    that is missing answers "unknown", and one that merely needs arguments
    answers with its usage.
    """
    from mooting.store import connect
    from mooting.telegram import MENU, ChatBoard

    #: Handled by the bot itself rather than by the shared console dispatch.
    bot_side = {"pair", "topics", "help"}

    db = tmp_path / "board.db"
    s = connect(db, init=True)
    s.add_agent("Jeremy", "human")
    s.add_agent("Santa", "claude", driver="spawn")
    s.open_topic("t", "A topic", "brief", "Jeremy", seats=("Jeremy", "Santa"))
    s.close()

    missing = []
    for command, _ in MENU:
        if command in bot_side:
            continue
        board = ChatBoard(db, "t", "Jeremy")
        try:
            out = board.handle(f"/{command}")
        finally:
            board.close()
        if f"unknown /{command}" in out:
            missing.append(command)

    assert not missing, f"advertised in the menu and not reachable: {missing}"


def test_the_destructive_commands_stay_off_the_menu(tmp_path):
    """`/reset` clears every topic, and has already been run here by accident."""
    from mooting.telegram import MENU, OFF_MENU

    listed = {command for command, _ in MENU}
    assert not (listed & set(OFF_MENU)), \
        "a destructive command is one tap from a thumb"


def test_a_meeting_opened_in_a_room_starts_with_that_rooms_team(tmp_path):
    from mooting.store import connect
    from mooting.telegram import ChatBoard

    db = tmp_path / "board.db"
    s = connect(db, init=True)
    s.add_agent("Jeremy", "human")
    for name in ("Santa", "Sam", "Kevin"):
        s.add_agent(name, "claude", driver="spawn")
    s.close()

    room = ("telegram", "-100111")
    chat = ChatBoard(db, None, "Jeremy", room=room)
    try:
        chat.handle("/team Santa Kevin")
        chat.handle("/topic new which engine should we use")
        seats = {r["agent"] for r in chat.console.store.seats(chat.console.topic_id)}
    finally:
        chat.close()

    assert seats == {"Santa", "Kevin", "Jeremy"}, seats
    assert "Sam" not in seats, "a seat outside the team was seated anyway"


def test_two_rooms_on_one_board_seat_their_own_teams(tmp_path):
    """The scenario this exists for: agents with A/B/C in one chat and D/E/F in
    another, on one board, without either team leaking into the other."""
    from mooting.store import connect
    from mooting.telegram import ChatBoard

    db = tmp_path / "board.db"
    s = connect(db, init=True)
    s.add_agent("Jeremy", "human")
    for name in ("Santa", "Sam", "Kevin"):
        s.add_agent(name, "claude", driver="spawn")
    s.close()

    seated = {}
    for room_id, team, question in ((("telegram", "-100111"), "Santa Sam", "engine choice"),
                                    (("telegram", "-100222"), "Kevin", "aircon efficiency")):
        chat = ChatBoard(db, None, "Jeremy", room=room_id)
        try:
            chat.handle(f"/team {team}")
            chat.handle(f"/topic new {question}")
            seated[room_id[1]] = {r["agent"] for r in
                                  chat.console.store.seats(chat.console.topic_id)}
        finally:
            chat.close()

    assert seated["-100111"] == {"Santa", "Sam", "Jeremy"}
    assert seated["-100222"] == {"Kevin", "Jeremy"}


def test_seating_somebody_for_one_meeting_does_not_join_them_to_the_team(tmp_path):
    """`/seats add` is the temporary gesture; `/team` is the one that sticks."""
    from mooting.store import connect
    from mooting.telegram import ChatBoard

    db = tmp_path / "board.db"
    s = connect(db, init=True)
    s.add_agent("Jeremy", "human")
    for name in ("Santa", "Sam"):
        s.add_agent(name, "claude", driver="spawn")
    s.close()

    room = ("telegram", "-100111")
    chat = ChatBoard(db, None, "Jeremy", room=room)
    try:
        chat.handle("/team Santa")
        chat.handle("/topic new first question")
        chat.handle("/seats add Sam")
        first = {r["agent"] for r in chat.console.store.seats(chat.console.topic_id)}
        assert "Sam" in first, "the temporary seat did not take"

        # The next meeting starts from the team, not from what the last one grew into.
        chat.handle("/topic new second question")
        second = {r["agent"] for r in chat.console.store.seats(chat.console.topic_id)}
        assert second == {"Santa", "Jeremy"}, second
    finally:
        chat.close()


def test_a_terminal_session_has_a_room_of_its_own(tmp_path):
    """No chat behind it, and still not a special case."""
    from mooting.console import Console
    from mooting.store import Store, connect

    db = tmp_path / "board.db"
    s = connect(db, init=True)
    s.add_agent("me", "human")
    s.add_agent("claude", "claude", driver="spawn")
    s.close()

    c = Console(db, None, "me")
    try:
        assert c.room == Store.LOCAL_ROOM
        c.emit = lambda *_: None
        c.handle("/team claude")
        c.handle("/topic new a question at the desk")
        assert {r["agent"] for r in c.store.seats(c.topic_id)} == {"claude", "me"}
        # and it is a different room from any chat
        assert c.store.room_team(c.store.ensure_room("telegram", "-100111")) == []
    finally:
        c.store.close()


def test_a_command_is_not_a_lost_user(tmp_path):
    """A chat with no topic answered every command with the topic list.

    Reported from a phone: `/team` came back "This chat is not on a topic yet"
    and a list of topics to tap. The list is right for somebody with something to
    say and nowhere to say it. A command answers for itself.
    """
    from mooting.telegram import wants_picker

    # Only the bare topic commands ask for the list.
    assert wants_picker("/topic") and wants_picker("/topics")
    for command in ("/team", "/effort", "/me Ege", "/seats", "/help", "/rounds 5"):
        assert not wants_picker(command), command


def test_a_room_remembers_its_topic_across_a_restart(tmp_path):
    """`where` lived in the bot, so restarting it lost the room's place — and
    then every command was answered with the topic list instead."""
    from mooting.store import connect

    db = tmp_path / "board.db"
    s = connect(db, init=True)
    s.add_agent("Jeremy", "human")
    s.add_agent("Santa", "claude", driver="spawn")
    s.open_topic("engine", "Which engine", "b", "Jeremy", seats=("Jeremy", "Santa"))
    rid = s.ensure_room("telegram", "-100111")

    assert s.room_topic("telegram", "-100111") is None
    s.set_room_topic(rid, "engine")
    s.close()

    # A new process, which is what a restart is.
    again = connect(db)
    try:
        assert again.room_topic("telegram", "-100111") == "engine"
        # and a topic that has gone since is not offered back
        again.clear_topics()
        assert again.room_topic("telegram", "-100111") is None
    finally:
        again.close()


def _two_room_board(tmp_path):
    from mooting.store import connect

    db = tmp_path / "board.db"
    s = connect(db, init=True)
    s.add_agent("Jeremy", "human")
    for name in ("Santa", "Sam", "Kevin"):
        s.add_agent(name, "claude", driver="spawn")
    s.close()
    return db


def test_a_meeting_opened_in_a_chat_belongs_to_that_chat(tmp_path):
    from mooting.store import connect
    from mooting.telegram import ChatBoard

    db = _two_room_board(tmp_path)
    chat = ChatBoard(db, None, "Jeremy", room=("telegram", "-100111"))
    try:
        chat.handle("/topic new engine choice")
    finally:
        chat.close()

    s = connect(db)
    try:
        mine = s.ensure_room("telegram", "-100111")
        theirs = s.ensure_room("telegram", "-100222")
        tid = int(s.topic("engine-choice")["id"])

        assert s.topic_visible_in(tid, mine)
        assert not s.topic_visible_in(tid, theirs), "the other room can see it"
        assert [t["slug"] for t in s.topics_for_room(theirs)] == []
    finally:
        s.close()


def test_a_meeting_opened_at_a_desk_belongs_to_everybody(tmp_path):
    """Starting at the desk and following it on a phone is the workflow."""
    from mooting.console import Console
    from mooting.store import connect

    db = _two_room_board(tmp_path)
    c = Console(db, None, "Jeremy")
    c.emit = lambda *_: None
    try:
        c.handle("/topic new a question at the desk")
        tid = c.topic_id
        assert c.store.topic(tid)["room_id"] is None
        for chat in ("-100111", "-100222"):
            assert c.store.topic_visible_in(
                tid, c.store.ensure_room("telegram", chat))
    finally:
        c.store.close()


def test_one_room_cannot_switch_into_another_rooms_meeting(tmp_path):
    """Isolation that a remembered slug defeats is not isolation."""
    from mooting.telegram import ChatBoard

    db = _two_room_board(tmp_path)
    a = ChatBoard(db, None, "Jeremy", room=("telegram", "-100111"))
    try:
        a.handle("/topic new private to room a")
    finally:
        a.close()

    b = ChatBoard(db, None, "Jeremy", room=("telegram", "-100222"))
    try:
        out = b.handle("/topic switch private-to-room-a")
        assert "no such topic" in out, out
        assert b.topic is None
        # and it is not offered in the list either
        assert "private-to-room-a" not in b.handle("/topic list")
    finally:
        b.close()


def test_the_pump_tells_a_room_only_about_its_own_meetings(tmp_path):
    """The leak this closes: every event went to every paired chat.

    Exercised through the same check the pump makes, rather than by faking
    aiogram — the decision is `topic_visible_in`, and the loop only obeys it.
    """
    from mooting.store import connect
    from mooting.telegram import ChatBoard, event_text

    db = _two_room_board(tmp_path)
    for chat, question in (("-100111", "engine choice"), ("-100222", "aircon")):
        board = ChatBoard(db, None, "Jeremy", room=("telegram", chat))
        try:
            board.handle(f"/topic new {question}")
        finally:
            board.close()

    s = connect(db)
    try:
        rooms = {c: s.ensure_room("telegram", c) for c in ("-100111", "-100222")}
        s.post(int(s.topic("engine-choice")["id"]), "Santa", "Godot, and here is why",
               count_turn=False)

        delivered = {c: [] for c in rooms}
        for ev in s.events_since(0, None):
            text = event_text(s, ev)
            if not text:
                continue
            for chat, rid in rooms.items():
                if s.topic_visible_in(ev.topic_id, rid):
                    delivered[chat].append(text)

        assert any("Godot" in t for t in delivered["-100111"])
        assert not any("Godot" in t for t in delivered["-100222"]), \
            "the other room was told about a meeting that is not its own"
    finally:
        s.close()


def test_topics_that_predate_rooms_stay_visible_everywhere(tmp_path):
    """Nothing on an existing board should disappear when this lands."""
    from mooting.store import connect

    db = _two_room_board(tmp_path)
    s = connect(db)
    try:
        tid = s.open_topic("older", "Older", "b", "Jeremy", seats=("Santa",))
        assert s.topic(tid)["room_id"] is None
        for chat in ("-100111", "-100222"):
            assert s.topic_visible_in(tid, s.ensure_room("telegram", chat))
    finally:
        s.close()


def test_the_account_running_the_bot_is_known_across_rooms(board):
    """Pairing is per room, and that left the operator with nobody to ask.

    Opening a group of your own meant sending `/pair` into a room where no
    member existed yet, then going to a terminal to approve yourself. The one
    question that is about the person rather than the room answers it.
    """
    board.pair_approve(board.pair_request("8770943593", "8770943593", "Jeremy"),
                       "jeremy", "jeremy")

    # The same Telegram account, seen in a room it has never been in.
    assert board.seat_for_user("8770943593") == "jeremy"
    # And nobody else is recognised this way.
    assert board.seat_for_user("999999") is None


def test_a_pending_request_does_not_make_somebody_known(board):
    """Asking is not being approved, in any room."""
    board.pair_request("-100999", "555", "A Stranger")
    assert board.seat_for_user("555") is None


def test_a_request_carries_a_handle_that_cannot_be_guessed(board):
    """`1`, `2`, `3` invited `/pair approve 4` for a request nobody had seen."""
    pid = board.pair_request("-100111", "42", "Someone")
    row = board.pairing("-100111", "42")

    assert row["ref"] and row["ref"] != str(pid)
    assert len(row["ref"]) >= 6
    second = board.pairing(
        "-100111", "43") if board.pair_request("-100111", "43", "Other") else None
    assert second["ref"] != row["ref"], "two requests share a handle"


def test_one_room_cannot_answer_another_rooms_request(board):
    """The hole the numbers invited: approving is a decision about who joins
    *this* council, and nothing checked which room the request came from."""
    board.pair_request("-100222", "77", "A Stranger")
    theirs = board.pairing("-100222", "77")

    # Reachable from the room it belongs to.
    assert board.pairing_by_ref(theirs["ref"], chat_id="-100222") is not None
    # And nowhere else.
    assert board.pairing_by_ref(theirs["ref"], chat_id="-100111") is None
    assert board.pairing_by_ref(str(theirs["id"]), chat_id="-100111") is None


def test_listing_requests_shows_only_this_room(board):
    """Listing every room's pending requests told whoever asked that other rooms
    exist and who is trying to get into them."""
    board.pair_request("-100111", "1", "Mine")
    board.pair_request("-100222", "2", "Theirs")

    here = board.pairings("pending", chat_id="-100111")
    assert [r["display"] for r in here] == ["Mine"]
    assert len(board.pairings("pending")) == 2, "the board still sees both"


def test_handles_are_backfilled_onto_a_board_that_predates_them(board, tmp_path):
    """An existing board has rows with no handle, and they must stay answerable."""
    import sqlite3

    from mooting.store import connect

    board.pair_request("-100111", "42", "Someone")
    path = board.path
    board.close()

    raw = sqlite3.connect(path)
    raw.execute("UPDATE pairings SET ref = NULL")
    raw.commit()
    raw.close()

    again = connect(path)                       # opening migrates
    try:
        row = again.pairing("-100111", "42")
        assert row["ref"], "an older request was left with no handle"
        assert again.pairing_by_ref(row["ref"], chat_id="-100111") is not None
    finally:
        again.close()


def _guest_room(tmp_path):
    """A host's board, with a guest let into one meeting."""
    from mooting.store import connect

    db = tmp_path / "board.db"
    s = connect(db, init=True)
    s.add_agent("Host", "human")
    s.add_agent("Guest", "human")
    s.add_agent("Santa", "claude", driver="spawn")
    s.open_topic("theirs", "The host's meeting", "b", "Host",
                 seats=("Host", "Guest", "Santa"))
    s.close()
    return db


def test_a_guest_cannot_clear_the_board_from_a_chat(tmp_path):
    """Letting somebody into one council handed them every council.

    `/reset yes` had no identity check at all, and a paired guest is a human
    seat like any other. There is no owner on the board to ask, so the gate is
    the machine: whoever holds the file and the token.
    """
    from mooting.telegram import ChatBoard

    db = _guest_room(tmp_path)
    chat = ChatBoard(db, "theirs", "Guest", room=("telegram", "-100111"))
    try:
        out = chat.handle("/reset yes")
        assert "at the machine" in out, out
        assert chat.console.store.topics(), "a guest cleared the board"
    finally:
        chat.close()


def test_the_board_can_still_be_cleared_at_the_machine(tmp_path):
    from mooting.console import Console

    db = _guest_room(tmp_path)
    c = Console(db, "theirs", "Host")
    c.emit = lambda *_: None
    try:
        c.handle("/reset yes")
        assert c.store.topics() == []
    finally:
        c.store.close()


def test_a_guest_cannot_delete_the_meeting_they_were_let_into(tmp_path):
    from mooting.telegram import ChatBoard

    db = _guest_room(tmp_path)
    chat = ChatBoard(db, "theirs", "Guest", room=("telegram", "-100111"))
    try:
        out = chat.handle("/topic rm theirs yes")
        assert "chairs" in out and "only they" in out, out
        assert chat.console.store.topic("theirs") is not None
    finally:
        chat.close()


def test_a_guest_cannot_unseat_anybody(tmp_path):
    from mooting.telegram import ChatBoard

    db = _guest_room(tmp_path)
    chat = ChatBoard(db, "theirs", "Guest", room=("telegram", "-100111"))
    try:
        out = chat.handle("/seats rm Santa")
        assert "chairs this meeting" in out, out
        seats = {r["agent"] for r in
                 chat.console.store.seats(chat.console.topic_id)}
        assert "Santa" in seats, "a guest unseated an agent"
    finally:
        chat.close()


def test_the_chair_can_still_remove_what_is_theirs(tmp_path):
    """The guard is about who, not about making removal hard."""
    from mooting.telegram import ChatBoard

    db = _guest_room(tmp_path)
    chat = ChatBoard(db, "theirs", "Host", room=("telegram", "-100111"))
    try:
        chat.handle("/seats rm Santa")
        seats = {r["agent"] for r in
                 chat.console.store.seats(chat.console.topic_id)}
        assert "Santa" not in seats
        chat.handle("/topic rm theirs yes")
        assert chat.console.store.topics() == []
    finally:
        chat.close()


def test_a_room_has_a_host_and_it_is_not_a_topics_chair(board):
    """Two different things. A chair runs one meeting and can be handed over;
    a host owns the room and decides who is let into it."""
    board.add_agent("Guest", "human")
    rid = board.ensure_room("telegram", "-100111")
    assert board.room_host(rid) is None

    assert board.claim_room(rid, "jeremy") == "jeremy"
    # Claiming is once: a guest arriving later does not take the room.
    assert board.claim_room(rid, "Guest") == "jeremy"
    assert board.room_host(rid) == "jeremy"

    # And a chair is per topic, assignable, and says nothing about the room.
    tid = board.open_topic("t", "T", "b", "Guest", seats=("jeremy", "Guest"))
    assert board.chair(tid) == "Guest"
    assert board.room_host(rid) == "jeremy"


def test_an_agent_cannot_host_a_room(board):
    rid = board.ensure_room("telegram", "-100111")
    with pytest.raises(NotAuthorised):
        board.claim_room(rid, "santa")
    assert board.room_host(rid) is None


def test_a_guest_cannot_let_more_guests_in(tmp_path):
    """The rule the removal guards were about, applied to the door.

    Any paired member could approve a request, so somebody let into a council
    could let in anybody else — which is the whole thing pairing exists to stop.
    """
    from mooting.store import connect

    db = tmp_path / "board.db"
    s = connect(db, init=True)
    s.add_agent("Host", "human")
    s.add_agent("Guest", "human")
    rid = s.ensure_room("telegram", "-100111")
    s.claim_room(rid, "Host")
    try:
        assert s.room_host(rid) == "Host"
        # The check the chat and the button both make.
        for presser, may in (("Host", True), ("Guest", False)):
            assert (presser == s.room_host(rid)) is may
    finally:
        s.close()


def test_owning_a_group_is_not_authority_over_somebody_elses_board(board):
    """The hole: a stranger who knows the bot's name makes a group, adds it, and
    is that group's creator. If creating a group were authority, every person
    they added would be let onto a board that is not theirs -- and the first one
    in could then hold the door for the rest.

    The rule the join path applies: the person adding somebody must already hold
    a seat here. Owning the group only says *which* seat is the host.
    """
    rid = board.ensure_room("telegram", "-100999")

    # A stranger: not paired anywhere on this board.
    added_by = board.seat_for_chat("-100999", "stranger")
    assert added_by is None
    # Which is the whole condition -- no seat, no auto-approval, whoever owns
    # the group in Telegram.
    assert not (added_by and True), "a stranger's invite was treated as a decision"
    assert board.room_host(rid) is None, "a stranger took the room"


def test_with_no_host_only_the_operator_answers_a_request(board):
    """"Any paired member" let the first person through the door hold it open.

    A room with no host has nobody established in it, so the fallback is the
    person running the bot -- the only authority that does not depend on the
    room being trustworthy.
    """
    board.add_agent("Guest", "human")
    rid = board.ensure_room("telegram", "-100999")
    operator = "jeremy"

    answers = board.room_host(rid) or operator
    assert answers == operator
    for presser, may in ((operator, True), ("Guest", False), (None, False)):
        assert (presser is not None and presser == answers) is may

    # Once a host is established, it is theirs.
    board.claim_room(rid, "Guest")
    assert (board.room_host(rid) or operator) == "Guest"


# ------------------------------------------------------------------- claiming
#
# Every earlier way of saying who owns a board was something a stranger could
# produce: a name on the command line, being first to pair, or creating the
# Telegram group. Reading the terminal the board lives on is not.


def test_a_code_is_worth_one_use_and_then_nothing(board):
    code = board.new_claim("jeremy")
    assert board.redeem_claim(code) == "jeremy"
    assert board.redeem_claim(code) is None, "a code worked twice"


def test_a_wrong_or_stale_code_is_worth_nothing(board):
    code = board.new_claim("jeremy")
    assert board.redeem_claim("nope") is None
    assert board.redeem_claim(code.upper()) == "jeremy", "case should not matter"

    board.new_claim("jeremy", ttl_s=-1)
    assert board.redeem_claim(board.setting("claim.code") or "x") is None
    assert board.setting("claim.code") is None, "a stale code was left lying about"


def test_a_code_cannot_hand_out_an_agent_seat(board):
    with pytest.raises(NotAuthorised):
        board.new_claim("santa")


def test_identity_is_the_account_not_the_name(board):
    """A name in a message is a claim anybody can make."""
    board.add_agent("Guest", "human")
    board.bind_identity("jeremy", "8770943593")

    assert board.seat_for_identity("8770943593") == "jeremy"
    assert board.seat_for_identity("999") is None
    # And it outranks anything inferred from pairing rows.
    assert board.seat_for_user("8770943593") == "jeremy"

    # One account, one seat: rebinding moves it rather than duplicating it.
    board.bind_identity("Guest", "8770943593")
    assert board.seat_for_identity("8770943593") == "Guest"
    assert board.q1("SELECT tg_user_id FROM agents WHERE name='jeremy'")["tg_user_id"] is None


def test_an_unknown_chat_is_not_a_chat_this_bot_works_in(board):
    """Default deny. Without an allowlist the bot answered anywhere it was added,
    so anybody who knew its name could stand up a group and talk to this board.

    This is the check `allowed` makes when no `--chat` was given.
    """
    assert board.pairings("approved", chat_id="-100999") == []

    board.pair_approve(board.pair_request("-100111", "42", "Jeremy"),
                       "jeremy", "jeremy")
    assert board.pairings("approved", chat_id="-100111") != []
    assert board.pairings("approved", chat_id="-100999") == [], \
        "an unknown chat looked known"


def test_a_room_is_not_told_what_it_just_said(board):
    """Every message appeared twice: once from the person, once from the bot.

    Telegram already put their words in the room. The pump sent every message
    event to every listening chat, including the one it came from.
    """
    board.pair_approve(board.pair_request("-100111", "42", "Jeremy"),
                       "jeremy", "jeremy")
    seats_here = {r["seat"] for r in board.pairings("approved", chat_id="-100111")}
    assert seats_here == {"jeremy"}

    tid = board.open_topic("t", "T", "b", "jeremy", seats=("jeremy", "santa"))
    board.post(tid, "jeremy", "what about the roof", count_turn=False)
    board.post(tid, "santa", "the roof is the expensive half", count_turn=False)

    # What the pump would send to that chat: the seat's answer, not the echo.
    delivered = [e.actor for e in board.events_since(0, tid)
                 if e.kind == "message" and e.actor not in seats_here]
    assert "santa" in delivered
    assert "jeremy" not in delivered, "the room was sent its own message back"


def test_another_rooms_person_is_still_heard(board):
    """The rule is "already in this room", not "is a person"."""
    board.add_agent("ege", "human")
    board.pair_approve(board.pair_request("-100111", "42", "Jeremy"),
                       "jeremy", "jeremy")
    tid = board.open_topic("t", "T", "b", "jeremy", seats=("jeremy", "ege", "santa"))
    board.post(tid, "ege", "from the other chat", count_turn=False)

    seats_here = {r["seat"] for r in board.pairings("approved", chat_id="-100111")}
    delivered = [e.actor for e in board.events_since(0, tid)
                 if e.kind == "message" and e.actor not in seats_here]
    assert "ege" in delivered, "somebody outside this room went unheard"


def test_each_seat_carries_a_mark_and_keeps_it(board):
    """A chat has no colour, so a seat gets a mark. Scanning for who said what
    should be a glance, which is what the full-screen view uses colour for."""
    from mooting.telegram import mark_for

    board.add_agent("kevin", "agy", driver="spawn")
    first = {n: mark_for(board, n) for n in ("jeremy", "santa", "kevin")}

    assert len(set(first.values())) == 3, "two seats share a mark"
    assert mark_for(board, "santa") == first["santa"], "a mark moved on its own"
    assert mark_for(board, "nobody") == ""


def test_a_person_whose_account_is_known_is_really_mentioned(board):
    """Being asked a question and finding out later are different things."""
    from mooting.telegram import with_mentions

    board.bind_identity("jeremy", "8770943593")

    out = with_mentions(board, "@jeremy which rooms?")
    assert 'tg://user?id=8770943593' in out
    assert ">jeremy</a>" in out


def test_a_name_with_no_account_is_left_as_written(board):
    """Half a mention is worse than none: a broken link reads as a bug."""
    from mooting.telegram import with_mentions

    assert with_mentions(board, "@santa what about it") == "@santa what about it"
    assert with_mentions(board, "no names here") == "no names here"
    # And an address that is not a seat is not touched either.
    assert "@nobody" in with_mentions(board, "ask @nobody")


def test_a_command_addressed_to_this_bot_is_still_a_command():
    """A group can hold several bots, so Telegram addresses a tapped command to
    one of them: `/seats` arrives as `/seats@jeremy_mooting_bot`.

    Commands with their own handler had the mention stripped for them and
    worked. Everything through the shared dispatch arrived with it attached and
    came back "unknown /seats@jeremy_mooting_bot" — invisible in a one-to-one
    chat, where Telegram appends nothing, which is where this was all tested.
    """
    from mooting.telegram import addressed_here

    us = "jeremy_mooting_bot"
    assert addressed_here("/seats", us) == "/seats"
    assert addressed_here("/seats@jeremy_mooting_bot", us) == "/seats"
    assert addressed_here("/topic@jeremy_mooting_bot new a question", us) == \
        "/topic new a question"
    assert addressed_here("/run@JEREMY_MOOTING_BOT", us) == "/run", "case matters not"
    assert addressed_here("plain talk", us) == "plain talk"


def test_another_bots_command_is_not_ours_to_answer():
    from mooting.telegram import addressed_here

    assert addressed_here("/seats@someone_else_bot", "jeremy_mooting_bot") is None


def test_nothing_is_stripped_before_the_name_is_known():
    """Safer direction: a command nobody claims beats one answered twice."""
    from mooting.telegram import addressed_here

    assert addressed_here("/seats@any_bot", None) == "/seats"


# ------------------------------------------------------- answers, not typing
#
# Typing on a phone is the cost this surface exists to avoid. A command whose
# answers are a short fixed list should offer them rather than ask.


def test_a_bare_command_asks_for_its_chooser():
    from mooting.telegram import wants_choices

    for command, which in (("/effort", "effort"), ("/rounds", "rounds"),
                           ("/nudge", "nudge"), ("/chair", "chair"),
                           ("/EFFORT", "effort")):
        assert wants_choices(command) == which, command


def test_a_command_that_already_has_its_answer_is_left_alone():
    from mooting.telegram import wants_choices

    for command in ("/effort high", "/rounds 5", "/nudge Santa", "/topics", "talk"):
        assert wants_choices(command) is None, command


def test_a_choice_carries_what_it_sets():
    from mooting.telegram import parse_set, set_callback

    for what, value in (("effort", "low"), ("rounds", "3"), ("chair", "Jeremy"),
                        ("wake", "Santa")):
        data = set_callback(what, value)
        assert parse_set(data) == (what, value)
        assert len(data.encode("utf-8")) <= 64

    assert parse_set("pick:3") is None
    assert parse_set("set:bogus:x") is None, "an unknown setting is not ours"
    assert parse_set("set:effort:") is None, "an empty value is not a choice"


def test_the_standing_keyboard_is_the_things_done_most_often():
    """Two rows of three: more squeezes the chat off a phone screen."""
    from mooting.telegram import DESK

    assert len(DESK) == 2 and all(len(row) == 3 for row in DESK)
    flat = [b for row in DESK for b in row]
    assert flat == sorted(set(flat), key=flat.index), "a button appears twice"
    for label in flat:
        assert label.startswith("/"), label


# ------------------------------------------------- a private chat is a room


def test_a_bound_account_gets_its_own_room(board):
    """A direct message is where you keep work the group has no business
    seeing, so it is a room like any other -- with one person in it."""
    board.bind_identity("jeremy", "111")          # redeemed a code from the machine

    assert board.private_room("111") == "jeremy"

    room = board.room("telegram", "111")
    assert room is not None and room["host"] == "jeremy"
    assert board.pairings("approved", chat_id="111"), "the room refuses its own owner"


def test_opening_the_same_private_room_twice_is_the_same_room(board):
    """Every message from that account calls this. It must not stack up rooms."""
    board.bind_identity("jeremy", "111")
    first, second = board.private_room("111"), board.private_room("111")

    assert first == second == "jeremy"
    assert len([r for r in board.rooms() if r["chat_id"] == "111"]) == 1


def test_being_trusted_in_a_group_does_not_earn_a_private_room(board):
    """The hole this closes: a room nobody else can look into still spends the
    owner's metered CLIs. Somebody approved into a group was vouched for *in
    that group*, where the host can see what they are doing. A private room is
    the opposite of that, so it takes the stronger proof -- a code redeemed from
    the terminal the board lives on, which a group member cannot produce."""
    board.add_agent("amber", "human")
    pid = board.pair_request("-100", "222", "Amber")
    board.pair_approve(pid, "amber", "jeremy")
    assert board.seat_for_user("222") == "amber", "she is known to the board"

    assert board.private_room("222") is None, "a group member let herself in"
    assert board.room("telegram", "222") is None


def test_a_stranger_gets_no_private_room(board):
    assert board.private_room("999") is None
    assert board.room("telegram", "999") is None


def test_a_private_topic_stays_out_of_the_group(board):
    """The whole point of the split: private projects in the direct message,
    friends in the group, and neither one leaking into the other."""
    board.bind_identity("jeremy", "111")
    board.private_room("111")
    mine = board.room("telegram", "111")["id"]
    theirs = board.ensure_room("telegram", "-100")

    private = board.open_topic("taxes", "T", "b", "jeremy", seats=("santa",),
                               room_id=mine)
    shared = board.open_topic("dinner", "T", "b", "jeremy", seats=("santa",),
                              room_id=theirs)

    assert board.topic_visible_in(private, mine)
    assert not board.topic_visible_in(private, theirs), "a private topic reached the group"
    assert board.topic_visible_in(shared, theirs)
    assert not board.topic_visible_in(shared, mine), "the group reached into the private room"
