"""Team mode: a manager plans, a human approves, workers execute.

The load-bearing claim is a negative one -- *no file is touched before a human
approves the plan* -- so these tests assert on computed board state after the loop
actually runs, and on the `executing` flag the driver really received. A test that
checked the guard clause exists would pass while the guard was bypassed elsewhere.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from mooting.drivers import FakeDriver
from mooting.store import NotAuthorised, StoreError, connect
from mooting.supervisor import Caps, Supervisor


@pytest.fixture()
def team(tmp_path):
    s = connect(tmp_path / "board.db", init=True)
    s.add_agent("human", "human", display="the chair")
    s.add_agent("boss", "claude", driver="spawn", driver_cfg={"cwd": str(tmp_path)})
    s.add_agent("hand", "codex", driver="spawn",
                driver_cfg={"cwd": str(tmp_path), "capability": "execute"})
    s.add_agent("thinker", "gemini", driver="spawn", driver_cfg={"cwd": str(tmp_path)})
    yield s
    s.close()


def work_topic(team, **kw):
    return team.open_topic(
        "ship-it", "Add retry backoff to the gateway",
        "Fixed 30s retries stampede on recovery. Ship exponential backoff.",
        "human", seats=("boss", "hand", "thinker", "human"),
        mode="work", manager="boss", **kw,
    )


def run(sup, tid):
    return asyncio.run(sup.run_topic(tid))


# ------------------------------------------------------------ who may assign

def test_only_the_manager_assigns(team):
    topic = work_topic(team)
    assert team.is_manager(topic, "boss")

    with pytest.raises(NotAuthorised):
        team.draft_task(topic, "hand", "thinker", "do my work for me")

    tid = team.draft_task(topic, "boss", "hand", "Add backoff", acceptance="tests pass")
    assert team.task(tid)["status"] == "draft"


def test_tasks_only_exist_on_a_work_topic(team):
    chat = team.open_topic("chat", "T", "B", "human", seats=("boss", "hand"), mode="discuss")
    with pytest.raises(StoreError):
        team.draft_task(chat, "boss", "hand", "sneak some work in")


# --------------------------------------------------- nothing runs before approval

def test_no_worker_is_woken_until_a_human_approves_the_plan(team):
    """The whole safety claim. The loop has no path to an `assigned` task except
    through Store.decide, so it cannot route around the gate."""
    topic = work_topic(team)
    woken: list[str] = []
    driver = FakeDriver(team, script=lambda st, seat, p: (woken.append(seat.agent) or None))

    team.draft_task(topic, "boss", "hand", "Add backoff", acceptance="tests pass")
    reason = run(Supervisor(team, {"boss": driver, "hand": driver}, Caps()), topic)

    assert "awaits your approval" in reason
    assert "hand" not in woken, "a worker ran before the plan was approved"
    assert team.tasks(topic, status="assigned") == []
    assert team.tasks(topic, status="draft") or team.tasks(topic)[0]["proposal_id"]


def test_approving_the_plan_is_the_only_thing_that_releases_work(team):
    topic = work_topic(team)
    team.draft_task(topic, "boss", "hand", "Add backoff", acceptance="tests pass")
    pid = team.submit_plan(topic, "boss")

    assert team.tasks(topic, status="assigned") == []
    with pytest.raises(NotAuthorised):
        team.decide(pid, "boss", approve=True)      # the manager cannot self-approve
    assert team.tasks(topic, status="assigned") == []

    team.decide(pid, "human", approve=True, rationale="go")
    assert [t["title"] for t in team.tasks(topic, status="assigned")] == ["Add backoff"]


def test_a_rejected_plan_releases_nothing(team):
    topic = work_topic(team)
    team.draft_task(topic, "boss", "hand", "Add backoff")
    pid = team.submit_plan(topic, "boss")
    team.decide(pid, "human", approve=False, rationale="wrong shape")

    assert team.tasks(topic, status="assigned") == []
    assert team.tasks(topic, status="draft"), "drafts should survive a rejection"


# ------------------------------------------------------------- the two-key rule

def test_execute_capability_alone_does_not_grant_execution(team):
    """`hand` is registered execute-capable, but on a *meeting* topic it must be
    woken read-only. Neither key works on its own."""
    chat = team.open_topic("chat", "T", "B", "human", seats=("hand",), mode="discuss")
    got: list[bool] = []
    driver = FakeDriver(team, script=lambda st, seat, p: (got.append(seat.executing) or "ok"))

    asyncio.run(Supervisor(team, {"hand": driver}).wake_seat(chat, "hand"))
    assert got == [False], "a meeting wake must never be an executing wake"


def test_an_approved_task_wakes_its_worker_in_execute_mode(team):
    topic = work_topic(team)
    team.draft_task(topic, "boss", "hand", "Add backoff", acceptance="tests pass")
    team.decide(team.submit_plan(topic, "boss"), "human", approve=True)

    seen: list[tuple[str, bool]] = []

    def worker(st, seat, prompt):
        seen.append((seat.agent, seat.executing))
        st.update_task(team.tasks(seat.topic_id, assignee=seat.agent)[0]["id"],
                       seat.agent, "done", "added backoff with jitter")
        return None

    driver = FakeDriver(team, script=worker)
    run(Supervisor(team, {"boss": driver, "hand": driver}, Caps(max_turns_per_seat=1)), topic)

    assert ("hand", True) in seen, f"worker not woken to execute: {seen}"
    assert team.tasks(topic)[0]["result"].startswith("added backoff")


def test_a_task_assigned_to_a_deliberation_seat_is_blocked_not_run(team):
    """A task nobody can do is a planning error the human should see, not a stall."""
    topic = work_topic(team)
    team.draft_task(topic, "boss", "thinker", "Edit the gateway")   # thinker: no execute
    team.decide(team.submit_plan(topic, "boss"), "human", approve=True)

    ran: list[str] = []
    driver = FakeDriver(team, script=lambda st, seat, p: (ran.append(seat.agent) or None))
    run(Supervisor(team, {"boss": driver, "thinker": driver}, Caps(max_turns_per_seat=1)), topic)

    assert "thinker" not in ran, "a deliberation-only seat was woken to execute"
    task = team.tasks(topic)[0]
    assert task["status"] == "blocked" and "execute capability" in task["result"]


# ------------------------------------------------------------- task lifecycle

def test_a_worker_may_only_report_on_its_own_task(team):
    topic = work_topic(team)
    tid = team.draft_task(topic, "boss", "hand", "Add backoff")
    team.decide(team.submit_plan(topic, "boss"), "human", approve=True)

    with pytest.raises(NotAuthorised):
        team.update_task(tid, "thinker", "done", "not mine to finish")
    team.update_task(tid, "hand", "done", "done properly")
    assert team.task(tid)["status"] == "done"


def test_only_the_manager_accepts_finished_work(team):
    topic = work_topic(team)
    tid = team.draft_task(topic, "boss", "hand", "Add backoff")
    team.decide(team.submit_plan(topic, "boss"), "human", approve=True)
    team.update_task(tid, "hand", "done", "shipped")

    with pytest.raises(NotAuthorised):
        team.update_task(tid, "hand", "accepted", "I approve of myself")
    team.update_task(tid, "boss", "accepted", "looks right")
    assert team.task(tid)["status"] == "accepted"


def test_a_draft_cannot_be_worked_on(team):
    topic = work_topic(team)
    tid = team.draft_task(topic, "boss", "hand", "Add backoff")
    with pytest.raises(StoreError):
        team.update_task(tid, "hand", "in_progress")


def test_the_worker_prompt_is_the_task_not_the_debate(team):
    """A worker needs its task, where to do it and how to report. The council
    transcript would just spend its context."""
    topic = work_topic(team)
    tid = team.draft_task(topic, "boss", "hand", "Add backoff",
                          body="Exponential, capped at 6 attempts.",
                          acceptance="gateway tests pass")
    team.decide(team.submit_plan(topic, "boss"), "human", approve=True)
    team.post(topic, "thinker", "IRRELEVANT-DEBATE-CHATTER", count_turn=False)

    prompt = Supervisor(team, {}).build_task_prompt(topic, team.task(tid))

    assert "Add backoff" in prompt and "capped at 6 attempts" in prompt
    assert "gateway tests pass" in prompt
    assert "mooting_task_update" in prompt
    assert "IRRELEVANT-DEBATE-CHATTER" not in prompt


def test_a_worker_that_finishes_without_reporting_is_not_left_stranded(team, tmp_path):
    """Observed live: a seat committed real work and never called
    mooting_task_update. Commits are the evidence; the report was only a claim."""
    topic = work_topic(team)
    tid = team.draft_task(topic, "boss", "hand", "Add backoff")
    team.decide(team.submit_plan(topic, "boss"), "human", approve=True)

    # A worker that does nothing at all and says nothing: blocked, not "done".
    silent = FakeDriver(team, script=lambda st, seat, p: None)
    run(Supervisor(team, {"boss": silent, "hand": silent}, Caps(max_turns_per_seat=1)), topic)

    task = team.task(tid)
    assert task["status"] == "blocked", f"silent no-op should not read as done: {task['status']}"
    assert "nothing committed" in task["result"]


# ------------------------------------------------- the worktree, for real

def _git(*args, cwd):
    import subprocess
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:                    # no git at all: skip, do not error
        pytest.skip("git unavailable")


@pytest.fixture()
def real_repo(tmp_path):
    """A genuine git repo.

    Every other test here hands the supervisor a plain `tmp_path`, so
    `git rev-parse --git-dir` fails and `_ensure_workspace` takes its fallback
    branch every single time -- meaning the `git worktree add` that work mode's
    whole isolation story rests on had never once run in a test. It runs here.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    if _git("init", "-q", cwd=repo).returncode != 0:
        pytest.skip("git unavailable")
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)
    (repo / "gateway.py").write_text("RETRY_SECONDS = 30\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "initial", cwd=repo)
    return repo


def test_a_task_gets_its_own_branch_and_checkout(tmp_path, real_repo):
    board = connect(tmp_path / "board.db", init=True)
    board.add_agent("human", "human")
    board.add_agent("boss", "claude", driver="spawn", driver_cfg={"cwd": str(real_repo)})
    board.add_agent("hand", "codex", driver="spawn",
                    driver_cfg={"cwd": str(real_repo), "capability": "execute"})
    topic = board.open_topic("ship-it", "T", "B", "human",
                             seats=("boss", "hand", "human"), mode="work", manager="boss")
    tid = board.draft_task(topic, "boss", "hand", "Add backoff", acceptance="tests pass")

    sup = Supervisor(board, {})
    sup._ensure_workspace(topic, board.task(tid))
    task = board.task(tid)

    assert task["branch"] == f"mooting/task-{tid}", \
        f"no branch was cut -- fell back? branch={task['branch']!r}"
    tree = pathlib.Path(task["worktree"])
    assert tree.is_dir() and (tree / "gateway.py").exists(), "the checkout is not real"
    assert task["base_sha"] == _git("rev-parse", "HEAD", cwd=real_repo).stdout.strip()

    # the branch exists in the repo, and it is NOT the branch the user is on
    branches = _git("branch", "--list", cwd=real_repo).stdout
    assert f"mooting/task-{tid}" in branches
    assert "* mooting/" not in branches, "the user's own checkout was moved onto the task branch"

    # and commits made in the worktree are what _commits_on counts
    assert sup._commits_on(task) == 0
    (tree / "gateway.py").write_text("RETRY_SECONDS = 1\n", encoding="utf-8")
    _git("add", "-A", cwd=tree)
    _git("commit", "-q", "-m", "backoff", cwd=tree)
    assert sup._commits_on(board.task(tid)) == 1, "work in the worktree was not counted"
    board.close()


def test_the_work_log_counts_what_landed_not_what_was_claimed(tmp_path, real_repo):
    """A worker's report is a claim about its branch. The log shows the commits
    beside it so the two can disagree in public."""
    from mooting.minutes import commits_on, worklog

    board = connect(tmp_path / "board.db", init=True)
    board.add_agent("human", "human")
    board.add_agent("boss", "claude", driver="spawn", driver_cfg={"cwd": str(real_repo)})
    board.add_agent("hand", "codex", driver="spawn",
                    driver_cfg={"cwd": str(real_repo), "capability": "execute"})
    topic = board.open_topic("ship-it", "T", "B", "human",
                             seats=("boss", "hand", "human"), mode="work", manager="boss")
    tid = board.draft_task(topic, "boss", "hand", "Add backoff", acceptance="tests pass")
    # A draft task cannot be reported on -- the plan has to be approved first,
    # which is the whole point of the fence. Turn that key before testing the log.
    board.decide(board.submit_plan(topic, "boss"), "human", approve=True, rationale="go")
    Supervisor(board, {})._ensure_workspace(topic, board.task(tid))

    task = board.task(tid)
    assert commits_on(task) == 0

    tree = pathlib.Path(task["worktree"])
    (tree / "gateway.py").write_text("RETRY_SECONDS = 1\n", encoding="utf-8")
    _git("add", "-A", cwd=tree)
    _git("commit", "-q", "-m", "backoff", cwd=tree)

    board.update_task(tid, "hand", "done", "shipped it")
    log = "\n".join(worklog(board, topic))
    assert "commits" in log, "the work log does not report commits at all"
    assert "| 1 |" in log, f"the commit that landed is not in the log:\n{log}"
    assert "shipped it" in log, "the worker's own report was dropped"
    board.close()


# --------------------------- work that happened and was never committed


def _work_task(tmp_path, real_repo):
    """A board with one assigned task whose worktree is a genuine repo."""
    board = connect(tmp_path / "board.db", init=True)
    board.add_agent("human", "human")
    board.add_agent("boss", "claude", driver="spawn", driver_cfg={"cwd": str(real_repo)})
    board.add_agent("hand", "codex", driver="spawn",
                    driver_cfg={"cwd": str(real_repo), "capability": "execute"})
    topic = board.open_topic("ship-it", "T", "B", "human",
                             seats=("boss", "hand", "human"), mode="work",
                             manager="boss")
    tid = board.draft_task(topic, "boss", "hand", "Cap the retries")
    board.set_task_workspace(tid, "mooting/task-1", str(real_repo), "HEAD")
    return board, tid


def test_work_left_uncommitted_is_not_reported_as_nothing(tmp_path, real_repo):
    """Found by running a real seat against a real repo: it edited the file
    correctly and never ran `git commit`. The loop looks only at commits, so it
    said "nothing committed" -- which reads as "did nothing", and a manager
    re-assigns work that is sitting right there in the worktree."""
    board, tid = _work_task(tmp_path, real_repo)
    sup = Supervisor(board, {})
    try:
        assert sup._uncommitted(board.task(tid)) == 0, "a clean tree looked dirty"

        (real_repo / "gateway.py").write_text("RETRY_SECONDS = 30\nMAX = 6\n",
                                              encoding="utf-8")

        assert sup._uncommitted(board.task(tid)) == 1, \
            "real changes in the worktree were invisible"
    finally:
        board.close()


def test_a_worktree_that_is_not_there_is_zero_not_a_crash(tmp_path, real_repo):
    """A probe inside the loop must never take the turn down with it."""
    board, tid = _work_task(tmp_path, real_repo)
    try:
        board.set_task_workspace(tid, "mooting/task-1", "", "")
        assert Supervisor(board, {})._uncommitted(board.task(tid)) == 0
    finally:
        board.close()


def test_a_worker_is_told_to_commit_before_it_is_told_to_report(team):
    """A live seat did the work correctly and left it uncommitted. Committing
    was two words inside a sentence about what *not* to do, while "Reporting
    back" was the section that read as how to finish. Order carries the
    emphasis, so the order is what is pinned."""
    topic = work_topic(team)
    tid = team.draft_task(topic, "boss", "hand", "Add backoff")
    team.decide(team.submit_plan(topic, "boss"), "human", approve=True)
    team.set_task_workspace(tid, "mooting/task-1", "/tmp/wt", "HEAD")

    prompt = Supervisor(team, {}).build_task_prompt(topic, team.task(tid))

    assert "git commit" in prompt, "the worker was never told to commit"
    assert prompt.index("git commit") < prompt.index("mooting_task_update"), \
        "reporting came before committing, which is the order that lost work"
    assert "uncommitted is invisible" in prompt, \
        "the reason it matters was not given, only the instruction"


def test_a_worker_with_no_worktree_is_not_told_to_commit(team):
    """Work outside a repo falls back to the working directory, and there is
    no branch to commit to. Telling it to commit anyway would be an error the
    seat cannot act on."""
    topic = work_topic(team)
    tid = team.draft_task(topic, "boss", "hand", "Add backoff")
    team.decide(team.submit_plan(topic, "boss"), "human", approve=True)

    prompt = Supervisor(team, {}).build_task_prompt(topic, team.task(tid))

    assert "git commit" not in prompt
    assert "mooting_task_update" in prompt, "it still has to report"


# ------------------------- an execute seat has to be able to commit

def _claude_seat(executing: bool):
    from mooting.drivers.base import Seat

    return Seat(topic_id=1, topic_slug="t", agent="hand", kind="claude",
                cli_session=None, executing=executing)


def test_an_executing_claude_seat_may_commit(tmp_path):
    """`acceptEdits` auto-approves file edits and nothing else, so `git commit`
    came back "requires approval" in a non-interactive run. Two live runs ended
    with the work staged and uncommitted, which the loop can only read as no
    work at all. The seat itself finally said why."""
    from mooting.drivers.spawn import ClaudeDriver

    flags = ClaudeDriver(tmp_path / "b.db").tool_profile(_claude_seat(True))

    assert "acceptEdits" in flags
    assert any("git commit" in f for f in flags), "it still cannot commit"
    assert any("git add" in f for f in flags), "it cannot stage what it wrote"
    assert "mcp__mooting" in flags, "it can no longer report back"


def test_an_executing_seat_still_may_not_push_or_merge(tmp_path):
    """The prompt tells a worker not to merge, not to push and not to touch
    another branch. Naming the verbs is what makes that a fact rather than a
    request; widening the permission mode would have left it as something the
    seat is merely asked to respect."""
    from mooting.drivers.spawn import ClaudeDriver

    flags = " ".join(ClaudeDriver(tmp_path / "b.db").tool_profile(_claude_seat(True)))

    for forbidden in ("git push", "git merge", "git checkout", "git reset",
                      "git rebase", "git branch"):
        assert forbidden not in flags, f"{forbidden} was granted"


def test_a_deliberating_seat_gets_no_shell_at_all(tmp_path):
    """The half that must not drift: a seat that is only arguing has no reason
    to touch a repository."""
    from mooting.drivers.spawn import ClaudeDriver

    flags = ClaudeDriver(tmp_path / "b.db").tool_profile(_claude_seat(False))

    assert "Bash" not in " ".join(flags)
    assert "manual" in flags


def test_an_executing_agy_seat_is_given_its_worktree(tmp_path):
    """agy runs shell commands in its own scratch directory and ignores the
    process working directory. Asked where it was, it answered
    `~\.gemini\antigravity-cli\brain\<uuid>\scratch` and "fatal: not a git
    repository" -- so a worker could not reach the worktree it was given at
    all, let alone commit to it."""
    from mooting.drivers.base import Seat
    from mooting.drivers.spawn import AgyDriver

    tree = str(tmp_path / "work" / "task-1")
    seat = Seat(topic_id=1, topic_slug="t", agent="hand", kind="agy",
                cli_session=None, executing=True, cfg={"cwd": tree})

    flags = AgyDriver(tmp_path / "b.db").tool_profile(seat)

    assert "accept-edits" in flags
    assert "--add-dir" in flags and tree in flags, \
        "the worktree was never put in agy's scope"


def test_a_deliberating_agy_seat_gets_no_directory(tmp_path):
    """Deliberation runs in a scratch directory that says nothing, and adding
    one to the workspace is how a project's notes join a council uninvited."""
    from mooting.drivers.base import Seat
    from mooting.drivers.spawn import AgyDriver

    seat = Seat(topic_id=1, topic_slug="t", agent="hand", kind="agy",
                cli_session=None, cfg={"cwd": str(tmp_path)})

    flags = AgyDriver(tmp_path / "b.db").tool_profile(seat)

    assert flags == ["--mode", "plan"]
