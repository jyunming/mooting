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

from moot.store import connect

ptk = pytest.importorskip("prompt_toolkit")
from prompt_toolkit.application import create_app_session          # noqa: E402
from prompt_toolkit.document import Document                        # noqa: E402
from prompt_toolkit.input import create_pipe_input                  # noqa: E402
from prompt_toolkit.output import DummyOutput                       # noqa: E402

from moot.console import Console, _ConsoleCompleter                # noqa: E402


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
