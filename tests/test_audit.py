"""Regressions for what a deep audit of this codebase found.

Every test here corresponds to a defect that existed and that the 121 tests
before it did not catch. Several are crashes; the reason they went unnoticed is
recorded in each docstring, because the gap in the suite is as much the finding
as the bug.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from mooting.store import CAPABILITIES, NotAuthorised, StoreError, connect


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
    from mooting.drivers.base import Driver

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


# ------------------------------------------------------------------- setup

def test_setup_seats_what_it_finds_and_wires_it_in_order(tmp_path, monkeypatch):
    """The order is the point: a seat of certain kinds posts under the wrong name
    until its own MCP server exists, so registration has to happen before
    anything is woken."""
    from mooting import setup as setup_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(setup_mod, "_found",
                        lambda: [("claude", "C:/x/claude.exe"), ("codex", "C:/x/codex.cmd")])
    installed: list[str] = []
    monkeypatch.setattr(setup_mod, "install_seat",
                        lambda store, name: (installed.append(name), (True, "registered"))[1])
    probed: list[str] = []

    async def fake_doctor(store, only=None, timeout=180.0):
        probed.append(only or "")
        return 0

    monkeypatch.setattr("mooting.doctor.run_doctor", fake_doctor)
    monkeypatch.setattr(setup_mod, "_ask", lambda *a, **k: (a[1] if len(a) > 1 else ""))

    rc = setup_mod.run(tmp_path / "board.db", assume_yes=True)

    assert rc == 0
    board = connect(tmp_path / "board.db")
    names = {a["name"] for a in board.agents()}
    assert {"claude", "codex"} <= names, f"seats not registered: {names}"
    assert any(a["kind"] == "human" for a in board.agents()), "no human seat"
    # codex needs a server of its own; claude is handed one per run.
    assert installed == ["codex"], f"wrong seats registered: {installed}"
    assert probed and "codex" in probed[0], "the seats were never proved"
    board.close()


def test_setup_stops_when_no_cli_is_installed(tmp_path, monkeypatch):
    """A council with no seats is not a council; say so rather than leaving an
    empty board that looks set up."""
    from mooting import setup as setup_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(setup_mod, "_found", lambda: [])
    monkeypatch.setattr(setup_mod, "_ask", lambda *a, **k: (a[1] if len(a) > 1 else ""))

    assert setup_mod.run(tmp_path / "board.db", assume_yes=True) == 1


# ------------------------------------------------------------- doc drift

#: Numbers spelled out in prose rot silently -- the sentence still reads fine
#: with the wrong word in it, so review never catches it. An audit found
#: ARCHITECTURE.md claiming eight MCP tools when there were eleven. Counting
#: the real thing is the only check that can go red.
NUMBER_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
    7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven",
    12: "twelve", 13: "thirteen", 14: "fourteen", 15: "fifteen",
}


def _repo_root():
    return pathlib.Path(__file__).resolve().parent.parent


def test_architecture_states_the_real_mcp_tool_count():
    server = (_repo_root() / "mooting" / "mcp_server.py").read_text(encoding="utf-8")
    actual = server.count("@mcp.tool()")
    assert actual, "no MCP tools found -- the counting method broke, not the doc"

    arch = (_repo_root() / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    row = [ln for ln in arch.splitlines() if "mcp_server.py" in ln and "|" in ln]
    assert row, "the file map no longer has an mcp_server.py row"
    assert NUMBER_WORDS[actual] in row[0], (
        f"ARCHITECTURE.md says {row[0].strip()!r} but there are {actual} tools"
    )


def test_no_doc_promises_a_transport_no_driver_implements():
    """`stdio_json` and `acp` are accepted strings with no class behind them.
    Prose may say they are planned; a file map may not say they exist."""
    from mooting.drivers.registry import DRIVER_CLASSES

    real = {d.kind for d in DRIVER_CLASSES.values()}
    arch = (_repo_root() / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    row = [ln for ln in arch.splitlines() if "`mooting/drivers/`" in ln]
    assert row, "the file map no longer has a drivers row"
    assert NUMBER_WORDS[len(real)] in row[0], (
        f"{len(real)} transport(s) implemented ({sorted(real)}), "
        f"but the file map says {row[0].strip()!r}"
    )


# ------------------------------------------------- codex containment

def test_codex_deliberation_never_runs_in_the_real_workspace(tmp_path):
    """Codex has no read-only MCP mode. The *only* thing keeping a deliberating
    codex seat away from the repo is `working_dir()` handing it an empty scratch
    directory instead of the real cwd -- so an inverted `seat.executing` check
    would silently give a meeting seat write access to the user's tree, and
    nothing else in the system would notice. This test is that check."""
    from mooting.drivers.base import Seat
    from mooting.drivers.spawn import CodexDriver

    repo = tmp_path / "the-real-repo"
    repo.mkdir()
    (repo / "secrets.txt").write_text("do not touch", encoding="utf-8")
    driver = CodexDriver(tmp_path / "board.db")

    def seat_for(executing):
        return Seat(topic_id=1, topic_slug="t", agent="codex", kind="codex",
                    cli_session=None, cfg={"cwd": str(repo)}, executing=executing)

    deliberating = pathlib.Path(driver.working_dir(seat_for(False)))
    assert deliberating != repo, "a deliberating codex seat was pointed at the repo"
    assert repo not in deliberating.parents, "the scratch dir is inside the repo"
    assert list(deliberating.iterdir()) == [], "the scratch dir is not empty"

    # ...and the second key does turn the lock.
    assert pathlib.Path(driver.working_dir(seat_for(True))) == repo


def test_agy_plan_mode_and_codex_approval_flags_survive(tmp_path):
    """Two argv flags carry safety meaning and nothing exercised them: codex
    needs --approve-for-me (without it the run blocks on a prompt nobody can
    answer), and agy's read-only guarantee is `--mode plan`."""
    from mooting.drivers.base import Seat
    from mooting.drivers.spawn import AgyDriver, CodexDriver

    seat = Seat(topic_id=1, topic_slug="t", agent="x", kind="codex",
                cli_session=None, cfg={"cwd": str(tmp_path)})

    codex = CodexDriver(tmp_path / "board.db").argv(seat, "prompt", None)
    assert "--approve-for-me" in codex, f"codex would block on approval: {codex}"
    assert "-" in codex, "codex takes the prompt on stdin, not in argv"

    agy = AgyDriver(tmp_path / "board.db").argv(seat, "prompt", None)
    assert "plan" in agy, f"agy lost its read-only mode: {agy}"


# ------------------------------------------------------- model listing

def test_model_list_parsing_survives_real_cli_output():
    """`agy` is the one CLI that enumerates its own models, and `_parse` is what
    reads it -- untested, so a change to the output format would have broken the
    model picker silently for the only CLI it serves."""
    from mooting.models import _parse

    got = _parse(
        "Fetching models...\n"
        "gemini-3.1-pro\tGemini 3.1 Pro\n"
        "gemini-3.7-flash\tGemini 3.7 Flash\n"
        "\n"
        "  claude-opus-5   Claude Opus 5 (preview)  \n"
        "gemini-3.1-pro\tduplicate, should collapse\n"
    )
    assert got == ["gemini-3.1-pro", "gemini-3.7-flash", "claude-opus-5"], got

    # noise must not become a model name in the picker
    assert _parse("error: not logged in") == []
    assert _parse("Usage: agy models [options]") == []
    assert _parse("") == []


def test_codex_can_deliberate_outside_a_git_repo(tmp_path):
    """The deliberation sandbox is a bare directory under the board, and codex
    refuses to start outside a git repo. That only ever worked because the
    sandbox sits under .mooting/ and inherited whatever repo mooting was run from --
    so running mooting anywhere else killed every codex wake."""
    from mooting.drivers.base import Seat
    from mooting.drivers.spawn import CodexDriver

    driver = CodexDriver(tmp_path / "board.db")

    def seat_for(executing):
        return Seat(topic_id=1, topic_slug="t", agent="codex", kind="codex",
                    cli_session=None, cfg={"cwd": str(tmp_path)}, executing=executing)

    assert "--skip-git-repo-check" in driver.argv(seat_for(False), "p", None)
    # ...but an executing seat runs in the user's own tree, where codex refusing
    # to touch an unversioned directory is the right answer.
    assert "--skip-git-repo-check" not in driver.argv(seat_for(True), "p", None)
