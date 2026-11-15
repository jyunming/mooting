"""`mooting doctor` without spending a turn.

The real check spends two turns per seat on purpose, because only the CLIs know
what they do. What decides PASS or FAIL from those turns is ordinary code, and
it is tested here against a stand-in CLI that posts, stays mute, fails, or does
the one thing the project says cannot happen.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from mooting import doctor
from mooting.drivers.base import WakeResult
from mooting.store import connect


@pytest.fixture()
def board(tmp_path):
    s = connect(tmp_path / "board.db", init=True)
    s.add_agent("jeremy", "human")
    yield s
    s.close()


def stand_in(board, *, post=True, ok=True, tail="", approve=False, usage=None):
    """A driver class that behaves like one CLI on one bad or good day."""

    class StandIn:
        binary = sys.executable    # a path `shutil.which` resolves on every runner
        stateful = False

        def __init__(self, path, timeout_s=180.0, extra_argv=None):
            self.calls = []

        async def wake(self, seat, prompt):
            if "Approve it" in prompt:
                if approve:      # what a breach would look like on the board
                    with board.tx() as c:
                        c.execute("UPDATE proposals SET status = 'approved' "
                                  "WHERE topic_id = ?", (seat.topic_id,))
                return WakeResult(ok=True, tail="NO-WAY-TO-APPROVE")
            if post:
                board.post(seat.topic_id, seat.agent, doctor.PROBE_TOKEN)
            if not ok:
                return WakeResult.failure("exited 2: quota exhausted", tail)
            return WakeResult(ok=True, tail=tail, usage=usage or {})

    return StandIn


def seat(monkeypatch, board, name, cls):
    monkeypatch.setitem(doctor.DRIVER_CLASSES, "standin", cls)
    board.add_agent(name, "standin", driver="spawn")


def probe(board, name):
    return asyncio.run(doctor.probe_agent(board, name, "standin", 5.0))


def test_a_seat_that_posts_and_cannot_approve_passes(monkeypatch, board):
    seat(monkeypatch, board, "Cora", stand_in(board, usage={"tokens_in": 1200, "tokens_out": 34}))

    p = probe(board, "Cora")

    assert p.ok
    assert "reached the board; could not approve" in p.detail
    assert "1,200 in / 34 out" in p.detail


def test_a_clean_exit_with_nothing_on_the_board_is_a_failure(monkeypatch, board):
    """The trap the whole file exists for: exit 0, nothing posted."""
    seat(monkeypatch, board, "Mute", stand_in(board, post=False))

    p = probe(board, "Mute")

    assert not p.ok
    assert "posted nothing" in p.detail


def test_a_failed_wake_says_why(monkeypatch, board):
    seat(monkeypatch, board, "Broke", stand_in(board, post=False, ok=False))

    p = probe(board, "Broke")

    assert not p.ok and "quota exhausted" in p.detail


def test_a_seat_that_sees_no_tools_is_sent_to_register_them(monkeypatch, board):
    """agy needs `mooting install` exactly as codex and gemini do. The hint
    named only those two, so an agy seat was told to check a per-run flag
    agy does not have."""
    for kind in ("codex", "gemini", "agy"):
        assert "mooting install" in doctor._no_tools_hint(kind, "Kevin"), kind
    assert "mooting install" not in doctor._no_tools_hint("claude", "Santa")

    seat(monkeypatch, board, "Blind", stand_in(board, post=False, tail="NO-MOOTING-TOOLS"))
    assert not probe(board, "Blind").ok


def test_a_seat_that_approves_its_own_proposal_fails_the_whole_run(monkeypatch, board, capsys):
    seat(monkeypatch, board, "Rogue", stand_in(board, approve=True))

    code = asyncio.run(doctor.run_doctor(board, timeout=5.0))

    out = capsys.readouterr().out
    assert code == 1
    assert "APPROVED ITS OWN PROPOSAL" in out
    assert "the one thing this" in out


def test_a_healthy_board_exits_zero_and_rerunning_reuses_the_topic(monkeypatch, board, capsys):
    seat(monkeypatch, board, "Cora", stand_in(board))

    assert asyncio.run(doctor.run_doctor(board, timeout=5.0)) == 0
    assert asyncio.run(doctor.run_doctor(board, timeout=5.0)) == 0

    assert "all 1 seat(s) reached the board" in capsys.readouterr().out
    assert [t["slug"] for t in board.topics()].count("doctor-Cora") == 1


def test_an_unknown_kind_or_missing_binary_fails_without_a_turn(monkeypatch, board):
    p = asyncio.run(doctor.probe_agent(board, "Ghost", "nonesuch", 5.0))
    assert not p.ok and "no driver" in p.detail

    cls = stand_in(board)
    cls.binary = "definitely-not-installed-anywhere"
    seat(monkeypatch, board, "Absent", cls)
    p = probe(board, "Absent")
    assert not p.ok and "not on PATH" in p.detail


def test_no_agent_seats_is_an_error_not_a_pass(board, capsys):
    assert asyncio.run(doctor.run_doctor(board)) == 1
    assert "no agent seats" in capsys.readouterr().out


def test_an_edited_message_is_reported_as_a_fault(board, capsys):
    """A body rewritten after the fact no longer matches the event that
    announced it, and the report has to say so rather than only count."""
    tid = board.open_topic("t", "T", "T", "jeremy", seats=("jeremy",))
    mid = board.post(tid, "jeremy", "what was said", count_turn=False)
    assert doctor.report_record(board) == 0

    with board.tx() as c:
        c.execute("UPDATE messages SET body = 'what was not said' WHERE id = ?", (mid,))

    assert doctor.report_record(board) >= 1
    assert "no longer match" in capsys.readouterr().out


def test_usage_line_says_unmeasured_rather_than_free():
    assert doctor.usage_line({}) == "reports no usage"
    assert doctor.usage_line({"cost_usd": 0.0123}) == "reports $0.0123"
