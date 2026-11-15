"""The shell commands, driven the way a person types them.

Every command here goes through `cli.main`, on a board in a temp directory,
with drivers that cost nothing. The ones that matter most are the identity
checks: the CLI resolves who "you" are on its own, and an agent seat must never
get through it to sign off, resume a meeting, or grant itself rounds.
"""

from __future__ import annotations

import pytest

from mooting.cli import main
from mooting.drivers import FakeDriver
from mooting.store import connect


@pytest.fixture()
def run(tmp_path, monkeypatch, capsys):
    """Run one command line against this test's board; return (code, stdout)."""
    monkeypatch.delenv("MOOTING_HUMAN", raising=False)
    monkeypatch.delenv("MOOTING_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "board.db")

    def _run(*argv):
        capsys.readouterr()
        try:
            code = main(["--db", db, *argv])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
            print(exc.code)
        return code, capsys.readouterr().out

    _run.db = db
    return _run


@pytest.fixture()
def council(run, tmp_path, monkeypatch):
    """A board with a person, one agent seat and a topic between them."""
    empty = tmp_path / "seat-cwd"
    empty.mkdir()
    assert run("init", "--human", "jeremy")[0] == 0
    assert run("agents", "add", "santa", "claude", "--cwd", str(empty))[0] == 0
    assert run("topic", "new", "reno", "--title", "Renovation",
               "--brief", "What to do first", "--seats", "santa")[0] == 0
    board = connect(run.db)

    def fake_drivers(store, names=None):
        return {"santa": FakeDriver(store)}
    monkeypatch.setattr("mooting.cli._drivers", fake_drivers)
    yield board
    board.close()


def proposal(board, title="Cap at 6"):
    return board.propose(int(board.topic("reno")["id"]), "santa", title, "do this")


# ----------------------------------------------------------- who "you" are

def test_an_agent_seat_cannot_sign_off_through_the_cli(run, council):
    pid = proposal(council)

    code, out = run("--as", "santa", "approve", str(pid), "-m", "looks right to me")

    assert code != 0 and "not a human seat" in out
    assert council.proposal(pid)["status"] == "open"


def test_the_only_person_on_a_board_signs_off_without_saying_who(run, council):
    pid = proposal(council)

    code, out = run("reject", str(pid), "-m", "too high")

    assert code == 0 and f"proposal #{pid} rejected by jeremy" in out


def test_an_agent_cannot_resume_a_meeting_or_grant_itself_rounds(run, council):
    tid = int(council.topic("reno")["id"])
    council.set_topic_status(tid, "paused", "jeremy", "waiting")

    code, out = run("--as", "santa", "run", "reno", "--resume", "--rounds", "5")

    assert code != 0 and "not a human seat" in out
    assert council.topic("reno")["status"] == "paused"
    assert council.topic("reno")["max_rounds"] == 3


def test_a_second_person_makes_the_cli_ask_who_you_are(run, council):
    run("agents", "add", "alice", "human")
    pid = proposal(council)

    code, out = run("approve", str(pid))

    assert code != 0 and "mooting --as" in out
    assert council.proposal(pid)["status"] == "open"


# -------------------------------------------------------------- a meeting

def test_a_meeting_from_the_shell_end_to_end(run, council, tmp_path):
    code, out = run("topic", "agenda", "reno", "which rooms")
    assert "1. What to do first" in out and "2. which rooms" in out, "points add"
    code, out = run("topic", "agenda", "reno", "--set", "budget; which rooms")
    assert "1. budget" in out and "2. which rooms" in out and "3." not in out, "--set replaces"

    assert "posted" in run("say", "reno", "start with the roof")[1]
    assert "asked santa" in run("ask", "reno", "santa", "what does a roof cost?")[1]

    code, out = run("prompt", "reno", "santa")
    assert code == 0 and "what does a roof cost?" in out, "the seat must be shown the question"

    code, out = run("run", "reno", "--max-turns", "1")
    assert code == 0 and "== stopped" in out
    assert any(m["author"] == "santa" for m in council.transcript(int(council.topic("reno")["id"])))

    code, out = run("show", "reno")
    assert "start with the roof" in out and "santa" in out
    assert "reno" in run("ls")[1]

    pid = proposal(council)
    code, out = run("proposals", "reno", "--full")
    assert f"#{pid} [open] Cap at 6" in out and "do this" in out

    code, out = run("conclude", "reno")
    assert code == 1 and "still waiting on you" in out, "an undecided proposal blocks closing"

    run("approve", str(pid), "-m", "fine")
    minutes = tmp_path / "reno.md"
    code, out = run("conclude", "reno", "roof first", "-o", str(minutes))
    assert code == 0 and "concluded `reno`" in out
    assert "Cap at 6" in minutes.read_text(encoding="utf-8")
    assert council.topic("reno")["status"] == "resolved"

    code, out = run("minutes", "reno", "-o", "-")
    assert code == 0 and "Renovation" in out


def test_nudge_reports_a_failed_wake_and_exits_non_zero(run, council, monkeypatch):
    assert run("nudge", "reno", "santa") == (0, "santa: ok\n")

    def failing(store, names=None):
        return {"santa": FakeDriver(store, fail_agents={"santa"})}
    monkeypatch.setattr("mooting.cli._drivers", failing)

    code, out = run("nudge", "reno", "santa")
    assert code == 1 and "FAILED" in out and "simulated wake failure" in out


def test_a_paused_meeting_does_not_run_until_resumed(run, council):
    council.set_topic_status(int(council.topic("reno")["id"]), "paused", "jeremy", "waiting")
    code, out = run("run", "reno")
    assert code == 1 and "--resume" in out

    code, out = run("run", "reno", "--resume", "--rounds", "2", "--max-turns", "1")
    assert code == 0
    assert council.topic("reno")["max_rounds"] == 5


# ------------------------------------------------- what asks before deleting

def test_deleting_asks_first_and_does_it_only_with_yes(run, council):
    code, out = run("topic", "rm", "reno")
    assert code == 1 and "re-run with --yes" in out
    assert council.topic("reno")

    code, out = run("agents", "rm", "santa")
    assert code == 1 and "re-run with --yes" in out

    code, out = run("reset")
    assert code == 1 and "reno" in out

    assert run("topic", "rm", "reno", "--yes")[0] == 0
    assert council.topics() == []
    assert run("agents", "rm", "santa", "--yes")[0] == 0
    assert [a["name"] for a in council.agents()] == ["jeremy"]


def test_reset_all_keeps_the_people(run, council):
    code, out = run("reset", "--all", "--yes")
    assert code == 0 and "human seats kept" in out
    assert [a["name"] for a in council.agents()] == ["jeremy"]


# --------------------------------------------------------- tokens and setup

def test_a_token_is_issued_only_to_a_person(run, council):
    code, out = run("serve", "--grant", "santa")
    assert code == 1, "a token on an agent seat would be a way to sign off as one"

    code, out = run("serve", "--grant", "jeremy")
    assert code == 0 and "may speak and sign off" in out
    assert council.token_holders() == ["jeremy"]

    assert "revoked" in run("serve", "--revoke", "jeremy")[1]
    assert council.token_holders() == []


def test_agents_ls_and_tasks_on_an_empty_plan(run, council):
    code, out = run("agents", "ls")
    assert "santa" in out and "claude" in out and "jeremy" in out
    assert run("tasks", "reno")[1] == "no tasks\n"


def test_install_reports_each_seat(run, council):
    code, out = run("install", "santa")
    assert code == 0 and "santa" in out
