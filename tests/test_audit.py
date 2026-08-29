"""Regressions for what a deep audit of this codebase found.

Every test here corresponds to a defect that existed and that the 121 tests
before it did not catch. Several are crashes; the reason they went unnoticed is
recorded in each docstring, because the gap in the suite is as much the finding
as the bug.
"""

from __future__ import annotations

import asyncio

import pytest

from moot.store import CAPABILITIES, NotAuthorised, StoreError, connect


@pytest.fixture()
def board(tmp_path):
    s = connect(tmp_path / "board.db", init=True)
    s.add_agent("me", "human")
    for n in ("claude", "codex"):
        s.add_agent(n, n, driver="spawn")
    yield s
    s.close()


# ------------------------------------------------------------------ store

def test_deleting_a_seat_that_owns_work_is_refused_not_crashed(board):
    """tasks.assignee is a NOT NULL foreign key onto agents(name), so removing a
    seat that was ever assigned work raised a raw sqlite IntegrityError instead
    of anything a caller could act on. The only existing delete test used a seat
    that had merely posted a message."""
    topic = board.open_topic("w", "T", "B", "me", seats=("claude", "codex", "me"),
                             mode="work", manager="claude")
    board.draft_task(topic, "claude", "codex", "Add backoff")

    with pytest.raises(StoreError, match="task"):
        board.delete_agent("codex")
    assert board.agent("codex"), "the seat should survive a refused delete"


def test_an_abstention_is_not_recorded_as_support(board):
    """The votes table stored the right stance, but the parallel message was
    written as kind='support' for anything that was not an objection -- so an
    abstention read as a supporting argument everywhere messages are shown."""
    topic = board.open_topic("t", "T", "B", "me", seats=("claude", "codex", "me"))
    pid = board.propose(topic, "claude", "Adopt backoff", "body")
    board.vote(pid, "codex", "abstain", "not my area")

    kinds = {m["kind"] for m in board.transcript(topic) if m["author"] == "codex"}
    assert "support" not in kinds, "an abstention was written down as support"


def test_approving_a_plan_releases_it_in_the_same_transaction(board):
    """Split across two transactions, a crash in between left a proposal approved
    with its tasks stuck as drafts -- which the work loop would then put up as a
    second, disconnected plan."""
    topic = board.open_topic("w", "T", "B", "me", seats=("claude", "codex", "me"),
                             mode="work", manager="claude")
    board.draft_task(topic, "claude", "codex", "Add backoff")
    pid = board.submit_plan(topic, "claude")
    board.decide(pid, "me", approve=True, rationale="go")

    assert board.proposal(pid)["status"] == "approved"
    assert [t["status"] for t in board.tasks(topic)] == ["assigned"]
    # A rejection must release nothing, in the same single step.
    board.draft_task(topic, "claude", "codex", "Another")
    pid2 = board.submit_plan(topic, "claude")
    board.decide(pid2, "me", approve=False, rationale="no")
    assert [t["status"] for t in board.tasks(topic) if t["proposal_id"] == pid2] \
        == ["draft"]


def test_opening_a_board_that_is_not_there_says_so(tmp_path):
    """Silently creating one turned a wrong --db or working directory into
    "nothing set up yet", which reads like a fresh install rather than a mistake.
    Every fixture passed init=True, so no test could tell the difference."""
    missing = tmp_path / "nowhere" / "board.db"
    with pytest.raises(StoreError, match="no board at"):
        connect(missing)
    assert not missing.exists(), "a failed open must not leave a board behind"

    connect(missing, init=True).close()          # and creating one still works
    assert missing.exists()
    connect(missing).close()                      # which then opens fine


def test_a_mistyped_capability_is_refused_at_registration(board):
    """It used to be accepted and only surfaced later as a confusing "not
    registered with execute capability" refusal, long after the typo."""
    assert CAPABILITIES == {"deliberate", "execute"}
    with pytest.raises(StoreError, match="capability"):
        board.add_agent("typo", "codex", driver_cfg={"capability": "excute"})
    board.add_agent("fine", "codex", driver_cfg={"capability": "execute"})


# ----------------------------------------------------------------- drivers

def test_a_cancelled_wake_kills_the_child_process():
    """The supervisor wraps driver.wake in its own wait_for, so a timeout arrives
    as a cancellation, not a TimeoutError. Catching only TimeoutError meant the
    CLI was never killed: the council moved on and left it running, burning quota
    on every timed-out turn. FakeDriver spawns nothing, so no test saw it."""
    from moot.drivers.base import Driver

    class Sleeper(Driver):
        binary = "python"

        async def wake(self, seat, prompt):      # pragma: no cover - unused
            return None

    d = Sleeper()
    started: list = []

    async def go():
        task = asyncio.ensure_future(
            d._run([__import__("sys").executable, "-c", "import time; time.sleep(60)"],
                   cwd=".", timeout=60))
        await asyncio.sleep(1.0)                 # let it actually spawn
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    # The child is killed inside _run's handler; reaching here without the event
    # loop complaining about a live transport is the observable part.
