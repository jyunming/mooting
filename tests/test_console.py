"""The interactive prompt, driven headlessly.

Every earlier smoke test piped stdin, which takes the plain `input()` fallback --
so the prompt_toolkit path, the thing that makes replying interactively work at
all, had never once run. prompt_toolkit ships a pipe input and a dummy output
precisely so an app can be exercised without a terminal, and that is what these
do: real PromptSession, real key handling, real completer.

This does not prove it *looks* right in a terminal. It proves the code path runs,
the keys land, and the completions are the ones a person would expect.
"""

from __future__ import annotations

import pytest

from mooting.store import connect

ptk = pytest.importorskip("prompt_toolkit")
from prompt_toolkit.application import create_app_session          # noqa: E402
from prompt_toolkit.document import Document                        # noqa: E402
from prompt_toolkit.input import create_pipe_input                  # noqa: E402
from prompt_toolkit.output import DummyOutput                       # noqa: E402

from mooting.console import Console, _ConsoleCompleter                # noqa: E402


@pytest.fixture()
def console(tmp_path):
    s = connect(tmp_path / "board.db", init=True)
    s.add_agent("me", "human")
    s.add_agent("claude", "claude", driver="spawn")
    s.add_agent("codex", "codex", driver="spawn")
    s.open_topic("t", "A topic", "brief", "me", seats=("claude", "codex", "me"))
    c = Console(tmp_path / "board.db", "t", "me")
    yield c
    c.store.close()
    s.close()


def drive(console: Console, keys: str) -> None:
    """Run the real interactive loop against a pipe, as a person would type."""
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        with create_app_session(input=pipe, output=DummyOutput()):
            console._ptk_loop()


def test_the_interactive_prompt_actually_runs_and_accepts_commands(console):
    drive(console, "/seats\n/quit\n")
    # Reaching here at all is the point: the prompt_toolkit path constructed a
    # session, read keys, dispatched a command and exited cleanly.


def test_typing_a_line_posts_it_to_the_board(console):
    console.auto = False        # do not spawn a supervisor thread in a test
    drive(console, "the gateway config lives in ops/\n/quit\n")

    bodies = [m["body"] for m in console.store.transcript(console.topic_id)]
    assert "the gateway config lives in ops/" in bodies


def test_answering_clears_a_question_through_the_real_prompt(console):
    console.auto = False
    console.store.ask(console.topic_id, "claude", "me", "Where does the config live?")
    assert console.pending_asks()

    drive(console, "ops/gateway.yaml\n/quit\n")

    assert console.pending_asks() == [], "answering did not clear the question"


def test_at_mention_from_the_prompt_directs_a_question(console):
    console.auto = False
    drive(console, "@codex what does the gateway do today?\n/quit\n")

    asks = console.store.open_mentions(console.topic_id)
    assert [(a["asker"], a["target"]) for a in asks] == [("me", "codex")]


def test_effort_is_retunable_from_the_prompt(console):
    drive(console, "/effort high\n/quit\n")
    assert console.effort() == "high"


# ------------------------------------------------------------------ completion

def _complete(console: Console, text: str) -> list[str]:
    comp = _ConsoleCompleter(console)
    return [c.text for c in comp.get_completions(Document(text, len(text)), None)]


def test_slash_commands_complete(console):
    assert "/effort" in _complete(console, "/eff")
    assert "/approve" in _complete(console, "/ap")


def test_at_completes_seat_names_but_never_your_own(console):
    out = _complete(console, "@c")
    assert "@claude" in out and "@codex" in out
    assert "@me" not in out, "completing your own name would be a no-op mention"


def test_toolbar_reports_what_is_waiting_on_you(console):
    console.store.ask(console.topic_id, "claude", "me", "Where is it?")
    bar = console.toolbar()
    assert "1 question(s) for you" in bar and "effort" in bar


# ------------------------------------------------------------------ agenda

def test_an_agenda_is_what_the_seats_are_asked(tmp_path):
    """A one-line topic gets a one-line answer from every seat. The agenda is
    the difference between a chat and a meeting, so it has to reach the prompt
    as an agenda -- not be indistinguishable from the title."""
    from mooting.supervisor import _agenda_of

    board = connect(tmp_path / "board.db", init=True)
    board.add_agent("human", "human")
    board.add_agent("claude", "claude", driver="spawn")
    con = Console(tmp_path / "board.db", None, "human")
    con.store = board
    out = []
    con.emit = out.append

    con._new("should webhook retries use exponential backoff?")
    topic = board.topic(con.topic_id)
    # /new seeds brief with the title, so "has an agenda" must not mean "brief set"
    assert _agenda_of(topic) == "", "the bare title was mistaken for an agenda"

    con._agenda("decide the cap, then the jitter; out of scope: the queue")
    assert _agenda_of(board.topic(con.topic_id)).splitlines() == [
        "- decide the cap, then the jitter",
        "- out of scope: the queue",
    ]

    con._agenda("+ and who owns the runbook")
    agenda = _agenda_of(board.topic(con.topic_id))
    assert agenda.endswith("- and who owns the runbook"), agenda
    assert "decide the cap" in agenda, "appending replaced instead of adding"

    con._agenda("clear")
    assert _agenda_of(board.topic(con.topic_id)) == "", "clear left an agenda behind"
    board.close()


def test_the_agenda_reaches_the_prompt(tmp_path):
    """Setting it is pointless if the seats never see it."""
    from mooting.supervisor import Supervisor

    board = connect(tmp_path / "board.db", init=True)
    board.add_agent("human", "human")
    board.add_agent("claude", "claude", driver="spawn")
    tid = board.open_topic("t", "Retry policy", "Retry policy", "human",
                           seats=("claude", "human"))

    plain, _ = Supervisor(board, {}).build_prompt(tid, "claude")
    assert "### Agenda" not in plain, "a bare title was announced as an agenda"

    board.set_brief(tid, "settle the cap; then jitter", "human")
    withagenda, _ = Supervisor(board, {}).build_prompt(tid, "claude")
    assert "### Agenda" in withagenda
    assert "settle the cap; then jitter" in withagenda
    board.close()


def test_several_agenda_points_from_one_line(tmp_path):
    """The session input is one line, so a list has to be typeable on one line."""
    from mooting.supervisor import _agenda_of

    board = connect(tmp_path / "board.db", init=True)
    board.add_agent("human", "human")
    con = Console(tmp_path / "board.db", None, "human")
    con.store = board
    con.emit = lambda *a, **k: None
    con._new("retry policy")

    con._agenda("cap the retries; full or partial jitter; who owns the runbook")
    assert _agenda_of(board.topic(con.topic_id)).splitlines() == [
        "- cap the retries",
        "- full or partial jitter",
        "- who owns the runbook",
    ]

    con._agenda("+ and what we tell the on-call")
    assert _agenda_of(board.topic(con.topic_id)).splitlines()[-1] ==         "- and what we tell the on-call"

    # a single point is still a point, and is listed like one
    con._agenda("clear")
    con._agenda("just decide the cap")
    assert _agenda_of(board.topic(con.topic_id)) == "- just decide the cap"
    board.close()


def test_agenda_accumulates_rather_than_overwriting(tmp_path):
    """Two calls used to leave one point, and the confirmation looked identical
    either way -- so a whole agenda item could vanish with nothing to show it."""
    board = connect(tmp_path / "board.db", init=True)
    board.add_agent("human", "human")
    con = Console(tmp_path / "board.db", None, "human")
    con.store = board
    con.emit = lambda *a, **k: None
    con._new("Renovation project in Leuven")

    con._agenda("what budget should we aim for")
    con._agenda("which rooms are in scope")
    assert con._agenda_points() == ["what budget should we aim for",
                                    "which rooms are in scope"]

    con._agenda("drop 1")
    assert con._agenda_points() == ["which rooms are in scope"]

    con._agenda("set only the budget")
    assert con._agenda_points() == ["only the budget"], "`set` must replace"

    con._agenda("clear")
    assert con._agenda_points() == []
    board.close()


def test_renaming_a_topic_moves_its_handle_unless_you_chose_one(tmp_path):
    board = connect(tmp_path / "board.db", init=True)
    board.add_agent("human", "human")
    con = Console(tmp_path / "board.db", None, "human")
    con.store = board
    con.emit = lambda *a, **k: None

    con._new("Renovation project in Leuven")
    tid = con.topic_id
    # the target is named, so you can rename a topic you are not looking at
    con._rename("renovation-project-in-leuven 'Leuven renovation: scope and budget'")
    assert board.topic(tid)["title"] == "Leuven renovation: scope and budget"
    assert board.topic(tid)["slug"] == "leuven-renovation-scope-and-budget"

    # and renaming from elsewhere works the same way
    board.open_topic("other", "Other", "Other", "human", seats=("human",))
    con._switch("other")
    con._rename("leuven-renovation-scope-and-budget 'Leuven: budget only'")
    assert board.topic(tid)["title"] == "Leuven: budget only"
    assert con.topic_id != tid, "renaming another topic must not move you to it"

    # a handle somebody chose deliberately is theirs to keep
    other = board.open_topic("kb", "Knowledge base", "Knowledge base", "human",
                             seats=("human",))
    board.rename_topic(other, "Docs rewrite", "human")
    assert board.topic(other)["slug"] == "kb", "a chosen handle was rewritten"
    board.close()


def test_a_moved_command_says_where_it_went(tmp_path):
    """Grouping the topic commands broke five spellings people had learned. The
    one question a rename creates is "where did it go", and "unknown" does not
    answer it."""
    board = connect(tmp_path / "board.db", init=True)
    board.add_agent("me", "human")
    con = Console(tmp_path / "board.db", None, "me")
    con.store = board
    out = []
    con.emit = lambda s="": out.append(s)

    con.handle("/agenda budget; rooms")
    said = " ".join(out)
    assert "/topic agenda" in said, said
    assert "budget; rooms" in said, "the arguments were dropped from the hint"

    out.clear()
    con.handle("/frobnicate")
    assert "unknown" in " ".join(out), "a genuinely unknown command must still say so"
    board.close()


def test_rounds_sets_a_total_and_plus_adds(tmp_path):
    """`/rounds 7` on a 3-round topic used to give 10 rounds and 13 turns --
    two numbers nobody asked for, with nothing on screen explaining either."""
    board = connect(tmp_path / "board.db", init=True)
    board.add_agent("me", "human")
    board.add_agent("kevin", "claude", driver="spawn")
    con = Console(tmp_path / "board.db", None, "me")
    con.store = board
    con.emit = lambda *a, **k: None
    con._topic("new retry policy")
    tid = con.topic_id

    con._rounds("7")
    assert board.topic(tid)["max_rounds"] == 7, "a total was read as an increment"
    assert {s["max_turns"] for s in board.seats(tid)} == {7}, \
        "a seat capped below the round count goes quiet mid-meeting"

    con._rounds("+2")
    assert board.topic(tid)["max_rounds"] == 9

    # and a nonsense value is refused rather than silently applied
    con._rounds("0")
    assert board.topic(tid)["max_rounds"] == 9
    board.close()


def test_a_second_session_cannot_drive_the_same_topic(console, tmp_path):
    """The claim lives on the board because that is all two processes share.

    Guarding with an in-process flag alone let a second console start a second
    supervisor and wake every seat twice against one budget.
    """
    from mooting.store import connect

    other = connect(tmp_path / "board.db")
    try:
        assert other.take_drive(console.topic_id, "another-session") is None

        said = []
        console.emit = said.append
        console._drive()

        assert any("already being driven" in line for line in said)
        # And the other session still holds it -- a refusal must not release it.
        assert other.take_drive(console.topic_id, "a-third-session") == "another-session"
    finally:
        other.close()
