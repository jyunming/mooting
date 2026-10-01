"""The surface an agent sees, called the way a CLI calls it.

`test_audit.py` reads the tool list. These call the tools. The gap between the
two is where an agent seated on one meeting could read, and put proposals on,
every other meeting on the board -- including ones in another room.
"""

from __future__ import annotations

import shlex

import pytest

import mooting.mcp_server as server
from mooting.store import StoreError, connect


@pytest.fixture
def board(tmp_path, monkeypatch):
    s = connect(tmp_path / "board.db", init=True)
    s.add_agent("me", "human")
    for n in ("claude", "codex"):
        s.add_agent(n, n, driver="spawn")
    # claude sits on `ours`; `theirs` is somebody else's meeting.
    s.open_topic("ours", "Ours", "our brief", "me", seats=("claude", "me"))
    s.open_topic("theirs", "Theirs", "a private brief", "me", seats=("codex", "me"))
    monkeypatch.setattr(server, "BOARD", s)
    monkeypatch.setattr(server, "AGENT", "claude")
    yield s
    s.close()


# ------------------------------------------------------------ seated: it works

def test_a_seated_agent_can_do_everything_it_is_offered(board):
    assert "posted #" in server.mooting_say("ours", "one point, with evidence")
    assert "proposal #" in server.mooting_propose("ours", "Adopt it", "because")
    pid = board.proposals(board.topic("ours")["id"])[0]["id"]
    assert "recorded support" in server.mooting_vote(pid, "support", "yes")
    assert "our brief" in server.mooting_read("ours")
    assert "passed" in server.mooting_pass("ours")
    assert server.mooting_tasks("ours") == "No tasks planned yet."


def test_the_inbox_shows_what_others_said_and_the_open_proposal(board):
    tid = board.topic("ours")["id"]
    board.post(tid, "me", "what do you think?", count_turn=False)
    board.propose(tid, "me", "Ship it", "body")
    inbox = server.mooting_inbox()
    assert "what do you think?" in inbox
    assert "Ship it" in inbox
    assert "theirs" not in inbox


# -------------------------------------------------------- unseated: refused

@pytest.mark.parametrize("call", [
    lambda: server.mooting_read("theirs"),
    lambda: server.mooting_say("theirs", "hello"),
    lambda: server.mooting_propose("theirs", "Take over", "body"),
    lambda: server.mooting_ask("theirs", "codex", "a question"),
    lambda: server.mooting_pass("theirs"),
    lambda: server.mooting_tasks("theirs"),
    lambda: server.mooting_assign("theirs", "codex", "a task"),
], ids=["read", "say", "propose", "ask", "pass", "tasks", "assign"])
def test_a_meeting_the_agent_is_not_seated_at_is_refused(board, call):
    before = len(board.transcript(board.topic("theirs")["id"]))
    out = call()
    assert out.startswith("refused:"), out
    assert "a private brief" not in out
    assert len(board.transcript(board.topic("theirs")["id"])) == before


def test_voting_on_another_meetings_proposal_is_refused(board):
    pid = board.propose(board.topic("theirs")["id"], "codex", "Theirs", "body")
    assert server.mooting_vote(pid, "object", "no").startswith("refused:")
    assert board.votes(pid) == []


def test_status_lists_only_the_meetings_this_seat_holds(board):
    status = server.mooting_status()
    assert "`ours`" in status
    assert "theirs" not in status and "codex" not in status


def test_an_unknown_topic_is_refused_rather_than_raised(board):
    assert server.mooting_say("nowhere", "hi").startswith("refused:")


# ------------------------------------------------ the store, as a second line

def test_the_store_refuses_an_unseated_agent_a_proposal_or_a_vote(board):
    theirs = board.topic("theirs")["id"]
    with pytest.raises(StoreError, match="holds no seat"):
        board.propose(theirs, "claude", "Take over", "body")
    pid = board.propose(theirs, "codex", "Theirs", "body")
    with pytest.raises(StoreError, match="holds no seat"):
        board.vote(pid, "claude", "support")


def test_a_person_needs_no_seat_to_propose(board):
    board.add_agent("guest", "human")
    assert board.propose(board.topic("ours")["id"], "guest", "An idea", "body")


def test_a_closed_meeting_takes_no_new_proposal(board):
    tid = board.topic("ours")["id"]
    board.set_topic_status(tid, "resolved", actor="me")
    with pytest.raises(StoreError, match="not accepting proposals"):
        board.propose(tid, "claude", "Late", "body")


# ------------------------------------------------------------------ web.py

def test_the_browser_session_refuses_a_public_address(tmp_path):
    from mooting.web import serve_web

    with pytest.raises(StoreError, match="refusing to bind 0.0.0.0"):
        serve_web(tmp_path / "b.db", host="0.0.0.0", port=1, human="me", topic=None)


def test_the_served_command_survives_a_shell(monkeypatch):
    from mooting import web

    monkeypatch.setattr(web.os, "name", "posix")
    argv = ["python", "--db", "/boards/a b&c.db", "tui", "x;rm -rf ~"]
    assert shlex.split(web.shell_command(argv)) == argv
