"""The debate loop.

Reads the board, decides who speaks next, wakes them, repeats. Everything it
knows how to do, a human could do by hand with `mooting nudge` -- that is
deliberate. The supervisor is an accelerator over a board that works without it.

Three rules it will not break:

1. **A human closes every proposal.** The loop stops and waits. It cannot approve,
   and it cannot route around a pending decision by starting a new sub-debate.
2. **Caps pause, they do not silently continue.** Hitting a turn or wake ceiling
   parks the topic in `paused` with a reason on the board, where a human sees it.
3. **A failed wake is not a lost message.** The seat's cursor is untouched, so the
   agent catches up next time anything wakes it. Adapters flake; topics don't die.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import re
import subprocess
from pathlib import Path
from dataclasses import dataclass
from typing import Sequence

from .drivers.base import Driver, Seat, WakeResult
from .store import Store

log = logging.getLogger("mooting.supervisor")


def _attachment_section(store, topic_id: int, budget: int) -> list[str]:
    """Source material, put where every seat can actually reach it.

    Text is inlined rather than merely pointed at, because a deliberating seat
    cannot open files: codex runs it in an empty sandbox on purpose, and the
    others have no reason to go looking. A path alone would be readable by the
    one execute-capable seat and invisible to everyone else, which is the worst
    of both -- the council would argue about a document only one member had.

    The path is given as well, so a seat that *can* open it may.
    """
    rows = store.attachments(topic_id)
    if not rows:
        return []
    lines = ["## Attached", ""]
    spent = 0
    for a in rows:
        head = f"**{a['name']}** ({a['bytes']:,} bytes)"
        if a["note"]:
            head += f" — {a['note']}"
        lines += [head, f"`{a['path']}`", ""]
        if not a["is_text"]:
            lines += ["_Not text; open it from that path if you can._", ""]
            continue
        try:
            body = pathlib.Path(a["path"]).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            lines += [f"_Could not be read back: {exc}_", ""]
            continue
        room = budget - spent
        if room <= 0:
            lines += ["_Not inlined: the attachment budget for this turn is "
                      "spent. Open it from the path above._", ""]
            continue
        if len(body) > room:
            body = body[:room]
            lines += ["```", body, "```",
                      f"_Truncated at {room:,} characters; the whole file is at "
                      f"the path above._", ""]
        else:
            lines += ["```", body, "```", ""]
        spent += len(body)
    return lines


def _agenda_of(topic) -> str:
    """The topic's agenda, or nothing when it is only an echo of the title.

    `/new` seeds the brief with the title so a topic is never empty, which means
    "has an agenda" is not the same as "brief is set" -- it means somebody wrote
    something the title does not already say.
    """
    brief = (topic["brief"] or "").strip()
    return "" if brief == (topic["title"] or "").strip() else brief



#: How the room is framed to a seat. This is the whole difference between the two
#: topic modes, and it is a real one: told that disagreement is the product, a
#: capable model will manufacture disagreement rather than spend a turn agreeing.
#: That is what you want when a decision hangs on finding the flaw, and actively
#: harmful when the room is trying to build something.
FRAMING = {
    "debate": (
        "**This is a debate.** Disagreement is the product. Spend your turns on "
        "objections that would change the outcome, and say what specifically would "
        "have to be true for you to withdraw one. If you agree, say so in a "
        "sentence and stop -- do not restate the argument in your own words."
    ),
    "work": (
        "**You are the manager of this team.** Break the goal into tasks small "
        "enough that one agent can finish each in a single sitting, and assign "
        "each to the seat best suited to it -- use `mooting_assign`. Give every task "
        "an acceptance line, so 'done' is checkable rather than a matter of "
        "opinion. Nothing runs until a human approves your plan, so put the whole "
        "plan up at once. When work comes back, review it and use "
        "`mooting_task_update(id, \"accepted\"|\"rejected\", why)`."
    ),
    "discuss": (
        "**This is a working discussion, not a debate.** Build on what others have "
        "said: add what is missing, supply the evidence someone asked for, sharpen a "
        "half-formed idea. Agreeing is a real contribution and needs no apology -- do "
        "not manufacture an objection to justify your turn. Disagree only where you "
        "actually do, and then say it plainly."
    ),
}


@dataclass(frozen=True)
class Caps:
    """Cost governor.

    Live debate spends real subscription quota without a human in the loop, and
    the point of routing work across four vendors is to *save* that quota. An
    unbounded loop would burn Copilot's monthly requests on chatter, so every
    ceiling here is a hard stop that pauses for a human rather than a soft hint.
    """
    max_rounds: int = 3
    max_turns_per_seat: int = 6
    max_wakes_per_agent_per_hour: int = 30
    #: Reasoning effort for every seat unless the topic or the seat overrides it.
    #: `low` is the default because that is what most sessions are: a question, a
    #: few readings, keep moving. Measured on a real council prompt it ran 8.8x
    #: faster than default effort (31.8s a turn against 279s), which is the
    #: difference between a conversation and a wait. Depth is one command away --
    #: `/effort high` when the ruling turns on catching a flaw -- and the trade is
    #: real in that direction: what `low` spends less of is argument quality.
    #: It is also the one value every driver takes; agy has no `medium`.
    effort: str = "low"
    #: A real task runs for many minutes. The deliberation ceiling would kill
    #: legitimate work part-way through and leave a half-finished worktree.
    work_timeout_s: float = 1800.0
    #: Ceiling on the catch-up excerpt in a wake prompt.
    #:
    #: Unbounded, this compounds into a doom loop: a failed wake leaves the seat's
    #: cursor unadvanced, so the next attempt carries *more* history, which makes
    #: failure likelier. Observed live -- an agy prompt reached 44,845 characters
    #: and blew the Windows command-line limit, having grown across three failed
    #: wakes. It is also simply expensive: nobody needs 45k characters of debate
    #: replayed to say one thing.
    max_catchup_chars: int = 12_000
    #: Ceiling on inlined attachment text per turn. Source material that crowds
    #: out the argument is worse than a path the seat has to ask about.
    max_attachment_chars: int = 8_000
    #: Consecutive silent turns across all seats that mean the debate is spent.
    quiet_rounds_to_settle: int = 1


def snippet(text: str, limit: int = 200) -> str:
    """A quotable one-liner from somebody's whole turn.

    `text[:200]` cut mid-word and kept the newlines, which mattered more than it
    sounds: the chat wraps this reason in italics, and Telegram-HTML is built
    per line, so a span that opened in one paragraph and closed in another
    matched nothing and arrived as literal underscores around a message that
    stopped mid-word.
    """
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:          # only back up to a word break if it is near
        cut = cut[:space]
    return cut.rstrip(" ,.;:—-") + "…"


def addressed_to(text: str, target: str, limit: int = 400) -> str:
    """The part of a turn that is actually aimed at `target`.

    One message naming two seats records a mention for each, and both rows store
    the whole body (`store._record_mentions`). Quoting from the top therefore
    showed the opening line -- which was addressed to somebody else -- under a
    heading saying this person was waiting on you. Reported from a phone as
    "those don't seem to be a question", and they were not: they were another
    seat's paragraph.

    Quoting from where the person is named gives them the part that is theirs.
    """
    m = re.search(rf"@{re.escape(target)}\b", text)
    if m:
        # From the start of that line, so "Final takeaway for @Jeremy" keeps
        # its lead-in instead of opening mid-sentence on the name itself.
        text = text[text.rfind(chr(10), 0, m.start()) + 1:]
    # Emphasis markers are noise inside a quotation that is already italic.
    return snippet(re.sub(r"[*_`]{1,3}", "", text), limit)


class Supervisor:
    def __init__(
        self,
        store: Store,
        drivers: dict[str, Driver],
        caps: Caps | None = None,
        turn_taking: str = "concurrent",
    ) -> None:
        #: keyed by agent *kind* (claude/codex/...), or by agent name for overrides
        self.drivers = drivers
        self.store = store
        self.caps = caps or Caps()
        #: concurrent | sequential -- see run_topic.
        self.turn_taking = turn_taking

    # ------------------------------------------------------------------ driving

    def driver_for(self, agent: str, kind: str) -> Driver | None:
        return self.drivers.get(agent) or self.drivers.get(kind)

    async def run_topic(self, topic_id: int) -> str:
        """Drive one topic until it needs a human, settles, or hits a cap.

        Returns the terminal reason, which is also written to the board so the
        state is legible without reading logs.
        """
        if self.store.topic(topic_id)["mode"] == "work":
            return await self.run_work(topic_id)
        while True:
            reason = self._blocking_reason(topic_id)
            if reason:
                self._park(topic_id, reason)
                return reason

            speakers = self._eligible(topic_id)
            if speakers and self.turn_taking == "concurrent":
                # Everyone answers the same board state at once, then reacts to
                # each other next round. Round wall-clock becomes max(seat) rather
                # than sum(seat) -- the only structural latency win available,
                # since spawn is ~2% of a turn and the rest is inference.
                #
                # One shared cursor for the whole round is what "simultaneous"
                # means: every prompt is built against the same head, and every
                # seat that succeeds advances to it. Nobody sees a peer's message
                # from this round, which also removes first-speaker anchoring.
                #
                # A proposal opened mid-round does not abort turns already in
                # flight; they finish, and _blocking_reason catches it at the top
                # of the next iteration. Per-seat caps bound the overrun.
                head = self.store.head()
                await self._gather(
                    (self.wake_seat(topic_id, a, head=head) for a in speakers),
                    what=f"concurrent round on topic {topic_id}",
                )
                # A concurrent round IS a round. Without this the counter only
                # advanced in the "nobody left to speak" branch, which concurrency
                # never reaches while seats still have peers to react to -- so
                # max_rounds was silently unenforced and only per-seat caps stopped
                # the loop.
                if not self._advance_round(topic_id):
                    return self._human_ask_reason(topic_id) or "rounds_exhausted"
                continue

            speaker = speakers[0] if speakers else None
            if speaker is None:
                # Everyone has spoken and nobody has anything new. Advance, or settle.
                if not self._advance_round(topic_id):
                    return self._human_ask_reason(topic_id) or "rounds_exhausted"
                continue

            await self.wake_seat(topic_id, speaker)

    def _advance_round(self, topic_id: int) -> bool:
        """Tick the round counter. False means the topic is out of rounds."""
        topic = self.store.topic(topic_id)
        if topic["round"] + 1 >= topic["max_rounds"]:
            reason = (self._human_ask_reason(topic_id)
                      or "rounds exhausted -- needs a human to extend or rule")
            self._park(topic_id, reason)
            return False
        with self.store.tx() as c:
            c.execute("UPDATE topics SET round = round + 1 WHERE id = ?", (topic_id,))
        self.store.post(
            topic_id, "mooting",
            f"--- round {topic['round'] + 2} of {topic['max_rounds']} ---",
            kind="system", count_turn=False,
        )
        return True

    def _effort_for(self, topic, agent: str) -> str | None:
        """Topic override beats seat default beats council default."""
        cfg = json.loads(self.store.agent(agent)["driver_cfg"])
        try:
            topic_effort = topic["effort"]
        except (IndexError, KeyError):
            topic_effort = None
        return topic_effort or cfg.get("effort") or self.caps.effort

    async def _gather(self, coros, *, what: str) -> None:
        """`gather(..., return_exceptions=True)` hands the exceptions back in a
        list. Both call sites used to drop that list on the floor, so a store
        call that raised anywhere in `wake_seat` outside the driver guard
        vanished completely -- no log line, no board message -- and left the
        seat's state at "waking", which the TUI renders as a seat thinking
        forever. Whatever else happens, the failure gets said out loud."""
        for outcome in await asyncio.gather(*coros, return_exceptions=True):
            if isinstance(outcome, asyncio.CancelledError):
                log.debug("%s: cancelled", what)
            elif isinstance(outcome, BaseException):
                log.error("%s: %r", what, outcome, exc_info=outcome)

    async def wake_seat(self, topic_id: int, agent: str, *, head: int | None = None) -> WakeResult:
        row = self.store.seat(topic_id, agent)
        if row is None:
            raise ValueError(f"{agent} holds no seat on topic {topic_id}")
        meta = self.store.agent(agent)
        topic = self.store.topic(topic_id)

        driver = self.driver_for(agent, meta["kind"])
        if driver is None:
            # Human and external seats are never woken; they read the board.
            return WakeResult.failure(f"no driver for {agent} (kind={meta['kind']})")

        seat = Seat(
            topic_id=topic_id,
            topic_slug=topic["slug"],
            agent=agent,
            kind=meta["kind"],
            cli_session=row["cli_session"],
            cfg=json.loads(meta["driver_cfg"]),
            effort=self._effort_for(topic, agent),
        )
        prompt, cursor = self.build_prompt(topic_id, agent, head=head)

        self.store.set_seat_state(topic_id, agent, "waking")
        wake_id = self.store.record_wake(topic_id, agent)
        before = self.store.q1(
            "SELECT COUNT(*) c FROM messages WHERE topic_id = ? AND author = ?",
            (topic_id, agent))["c"]
        try:
            try:
                result = await asyncio.wait_for(driver.wake(seat, prompt), timeout=driver.timeout_s)
            except asyncio.TimeoutError:
                result = WakeResult.failure(f"timed out after {driver.timeout_s}s")
            except asyncio.CancelledError:
                # Cancellation is a BaseException, so `except Exception` missed it and
                # the wake was never closed. Three seats were left reading "thinking"
                # for twenty minutes after the user stopped the council.
                self.store.finish_wake(wake_id, "cancelled", "stopped mid-turn")
                self.store.set_seat_state(topic_id, agent, "idle")
                raise
            except Exception as exc:  # an adapter bug must not take the topic down
                log.exception("driver %s raised for %s", driver.kind, agent)
                result = WakeResult.failure(f"{type(exc).__name__}: {exc}")

            self.store.finish_wake(wake_id, "ok" if result.ok else "error", result.detail)

            spoke = self.store.q1(
                "SELECT COUNT(*) c FROM messages WHERE topic_id = ? AND author = ?",
                (topic_id, agent))["c"] > before

            if result.ok and not spoke:
                # The CLI ran and exited clean while the seat said nothing. That is
                # not the same as an answer, and reporting it as a successful wake
                # left a question apparently ignored with nothing on the board to
                # explain it. Say so: the turn was spent either way.
                self.store.post(
                    topic_id, "mooting",
                    f"{agent} was woken and said nothing — its turn produced no post. "
                    f"Anything still asked of it stays open; /nudge {agent} to try again.",
                    kind="system", count_turn=False)

            if result.ok:
                if result.cli_session and result.cli_session != row["cli_session"]:
                    self.store.set_cli_session(topic_id, agent, result.cli_session)
                # Only advance the cursor on success. A failed wake leaves it alone so
                # the agent still sees everything it missed whenever it next speaks.
                self.store.advance_cursor(topic_id, agent, cursor)
                self.store.set_seat_state(topic_id, agent, "idle")
            else:
                self.store.set_seat_state(topic_id, agent, "failed")
                self.store.post(
                    topic_id, "mooting",
                    f"wake failed for {agent}: {result.detail}. "
                    f"Its cursor is unchanged -- it will catch up when next woken.",
                    kind="system", count_turn=False,
                )
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            # The driver guard above only covers `driver.wake`. Everything
            # around it -- finishing the wake row, counting posts, advancing the
            # cursor -- talks to the board too, and a board can be locked or
            # surprise us. When one of those raised, the exception went into
            # `gather(return_exceptions=True)` and was discarded, leaving the
            # seat at "waking" with nothing anywhere to explain it. Put the seat
            # somewhere terminal, then re-raise so `_gather` logs it.
            log.exception("bookkeeping failed for %s on topic %s", agent, topic_id)
            for repair in (lambda: self.store.finish_wake(wake_id, "error", "bookkeeping failed"),
                           lambda: self.store.set_seat_state(topic_id, agent, "failed")):
                try:
                    repair()
                except Exception:      # the board may be the thing that is broken
                    pass
            raise


    # ------------------------------------------------------------------- work

    async def run_work(self, topic_id: int) -> str:
        """Team mode: a manager plans, a human approves, workers execute.

        The states are deliberately few, and the order matters. Execution never
        precedes approval because the only path out of `draft` is `Store.decide`
        on the plan proposal -- this loop cannot route around it, because it has
        no other way to reach an `assigned` task.
        """
        while True:
            topic = self.store.topic(topic_id)
            if topic["status"] != "open":
                return f"topic is {topic['status']}"

            # 1. A plan on the table is a human decision, and nothing else happens
            #    until it is made.
            plan = [p for p in self.store.proposals(topic_id, status="open")]
            if plan:
                self._park(topic_id, f"work plan #{plan[0]['id']} awaits your approval")
                return f"work plan #{plan[0]['id']} awaits your approval"

            # 2. Approved work runs, in parallel, each in its own worktree.
            runnable = self._runnable_tasks(topic_id)
            if runnable:
                for task in runnable:            # sequential: git index is not concurrent
                    self._ensure_workspace(topic_id, task)
                await self._gather(
                    (self._wake_for_task(topic_id, self.store.task(int(t["id"])))
                     for t in runnable),
                    what=f"parallel tasks on topic {topic_id}",
                )
                continue

            # 3. Finished or blocked work needs the manager's verdict.
            manager = self._manager(topic_id)
            reviewable = [t for t in self.store.tasks(topic_id)
                          if t["status"] in {"done", "blocked"}]
            if reviewable and manager and self._has_budget(topic_id, manager):
                await self.wake_seat(topic_id, manager)
                continue

            # 4. Drafts the manager wrote go to the human as one plan.
            if self.store.tasks(topic_id, status="draft"):
                pid = self.store.submit_plan(topic_id, manager or topic["opened_by"])
                self._park(topic_id, f"work plan #{pid} awaits your approval")
                return f"work plan #{pid} awaits your approval"

            # 5. Nothing planned yet: the manager plans.
            if not self.store.tasks(topic_id) and manager and self._has_budget(topic_id, manager):
                await self.wake_seat(topic_id, manager)
                continue

            reason = self._human_ask_reason(topic_id) or self._work_summary(topic_id)
            self._park(topic_id, reason)
            return reason

    def _manager(self, topic_id: int) -> str | None:
        for s in self.store.seats(topic_id):
            if s["role"] == "manager":
                return s["agent"]
        return None

    def _has_budget(self, topic_id: int, agent: str) -> bool:
        s = self.store.seat(topic_id, agent)
        return bool(s) and s["turns_used"] < min(s["max_turns"], self.caps.max_turns_per_seat)

    def _runnable_tasks(self, topic_id: int) -> list:
        """Assigned tasks whose worker may actually execute.

        A task assigned to a seat without `--capability execute` is not silently
        run read-only -- it is reported, because a task nobody can do is a planning
        error the human should see rather than a mystery stall.
        """
        out = []
        for t in self.store.tasks(topic_id, status="assigned"):
            agent = t["assignee"]
            cfg = json.loads(self.store.agent(agent)["driver_cfg"])
            if cfg.get("capability") != "execute":
                self.store.post(
                    topic_id, "mooting",
                    f"task #{t['id']} is assigned to {agent}, which was not registered "
                    f"with --capability execute. Re-register that seat or reassign.",
                    kind="system", count_turn=False)
                self.store.update_task(int(t["id"]), agent, "blocked",
                                       "assignee has no execute capability")
                continue
            if self._has_budget(topic_id, agent):
                out.append(t)
        return out

    def _ensure_workspace(self, topic_id: int, task) -> None:
        """A branch and an isolated checkout per task.

        Workers run concurrently; pointing several of them at one checkout would
        have them overwrite each other's edits. Worktrees are also how the result
        stays reviewable -- work lands on `mooting/task-N`, never on the branch you
        are sitting on, and merging stays a human git action.
        """
        if task["worktree"]:
            return
        tid = int(task["id"])
        cfg = json.loads(self.store.agent(task["assignee"])["driver_cfg"])
        repo = cfg.get("cwd") or str(Path.cwd())
        branch = f"mooting/task-{tid}"
        tree = Path(self.store.path).parent / "work" / f"task-{tid}"
        try:
            subprocess.run(["git", "-C", repo, "rev-parse", "--git-dir"],
                           capture_output=True, check=True)
            tree.parent.mkdir(parents=True, exist_ok=True)
            base = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace").stdout.strip()
            r = subprocess.run(["git", "-C", repo, "worktree", "add", "-b", branch,
                                str(tree), "HEAD"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            if r.returncode != 0:
                raise RuntimeError(r.stderr.strip()[:300])
            self.store.set_task_workspace(tid, branch, str(tree), base)
        except Exception as exc:
            # Not fatal: without git the task simply runs in the seat's own cwd,
            # which is the user's choice to make by not using a repo.
            log.warning("no worktree for task %s: %s", tid, exc)
            self.store.set_task_workspace(tid, "", repo)
            self.store.post(topic_id, "mooting",
                            f"task #{tid} has no isolated worktree ({exc}); "
                            f"it will run directly in {repo}",
                            kind="system", count_turn=False)

    async def _wake_for_task(self, topic_id: int, task) -> WakeResult:
        agent = task["assignee"]
        meta = self.store.agent(agent)
        cfg = json.loads(meta["driver_cfg"])
        driver = self.driver_for(agent, meta["kind"])
        if driver is None:
            return WakeResult.failure(f"no driver for {agent}")

        seat = Seat(
            topic_id=topic_id,
            topic_slug=self.store.topic(topic_id)["slug"],
            agent=agent,
            kind=meta["kind"],
            cli_session=None,
            cfg={**cfg, "cwd": task["worktree"] or cfg.get("cwd")},
            # Effort for execution is the seat's own or the council default -- never
            # the topic dial, so a `low` brainstorming setting cannot quietly make
            # the actual work sloppy.
            effort=cfg.get("effort") or self.caps.effort,
            executing=True,                     # both keys turned: see Seat.executing
            timeout_s=self.caps.work_timeout_s,
        )
        self.store.update_task(int(task["id"]), agent, "in_progress")
        self.store.set_seat_state(topic_id, agent, "waking")
        wake_id = self.store.record_wake(topic_id, agent)
        try:
            result = await driver.wake(seat, self.build_task_prompt(topic_id, task))
        except Exception as exc:
            log.exception("task driver failed for %s", agent)
            result = WakeResult.failure(f"{type(exc).__name__}: {exc}")
        self.store.finish_wake(wake_id, "ok" if result.ok else "error", result.detail)
        self.store.set_seat_state(topic_id, agent, "idle" if result.ok else "failed")

        tid = int(task["id"])
        if self.store.task(tid)["status"] == "in_progress":
            # The turn ended without the worker calling mooting_task_update. Observed
            # live: a seat committed real work to its branch and simply never
            # reported. Leaving the task in_progress strands it -- the manager is
            # never asked to review, and the loop then finds nothing runnable.
            #
            # So don't take the silence at face value in either direction. Look at
            # what landed on the branch: commits are the evidence, and the report
            # was only ever a claim about them.
            if not result.ok:
                self.store.update_task(tid, agent, "blocked", f"wake failed: {result.detail}")
            else:
                n = self._commits_on(self.store.task(tid))
                if n:
                    self.store.update_task(
                        tid, agent, "done",
                        f"(no report from the worker; {n} commit(s) on {task['branch']})")
                else:
                    self.store.update_task(
                        tid, agent, "blocked",
                        "turn ended with no report and nothing committed")
        return result

    def _commits_on(self, task) -> int:
        """Commits this task's branch has that its base did not.

        The honest measure of whether work happened, independent of what the
        worker said about it.
        """
        tree, base = task["worktree"], task["base_sha"]
        if not tree or not base:
            return 0
        try:
            r = subprocess.run(["git", "-C", tree, "rev-list", "--count", f"{base}..HEAD"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace")
            return int(r.stdout.strip() or 0) if r.returncode == 0 else 0
        except Exception as exc:
            log.warning("could not count commits for task %s: %s", task["id"], exc)
            return 0

    def build_task_prompt(self, topic_id: int, task) -> str:
        """What a worker is told. Deliberately NOT the council catch-up prompt --
        a worker needs its task, where to do it, and how to report; the debate is
        noise that would spend its context."""
        topic = self.store.topic(topic_id)
        tid = int(task["id"])
        where = task["worktree"] or "your working directory"
        lines = [
            f"You are **{task['assignee']}**, working on an Mooting team topic.",
            "",
            f"## Task #{tid}: {task['title']}",
            "",
            task["body"].strip() or "_(no further detail given)_",
            "",
        ]
        if task["acceptance"]:
            lines += ["**Done when:** " + task["acceptance"].strip(), ""]
        lines += [
            f"**Context:** {topic['title']}",
            topic["brief"].strip(),
            "",
            "## Where to work",
            "",
            f"`{where}`",
        ]
        if task["branch"]:
            lines.append(f"This is an isolated git worktree on branch `{task['branch']}`. "
                         f"Commit there. Do not merge, do not push, and do not touch "
                         f"any other branch -- a human reviews and merges.")
        lines += [
            "",
            "## Reporting back",
            "",
            "Use the `mooting` MCP tools; nothing else you write is read by anyone.",
            "",
            f"- `mooting_task_update({tid}, \"done\", \"<what you changed and where>\")` when finished.",
            f"- `mooting_task_update({tid}, \"blocked\", \"<what stopped you>\")` if you cannot proceed.",
            "- `mooting_ask(topic, agent, question)` to ask the manager or a human first.",
            "",
            "Do only this task. If you notice other work that needs doing, say so in "
            "your report rather than doing it.",
        ]
        return "\n".join(lines)

    def _work_summary(self, topic_id: int) -> str:
        tasks = self.store.tasks(topic_id)
        if not tasks:
            return "no tasks planned -- the manager has nothing to do or no budget left"
        counts: dict[str, int] = {}
        for t in tasks:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        if set(counts) <= {"accepted"}:
            return f"work complete -- {counts.get('accepted', 0)} task(s) accepted"
        return "work paused: " + ", ".join(f"{n} {st}" for st, n in sorted(counts.items()))

    # ------------------------------------------------------------- turn-taking

    def _blocking_reason(self, topic_id: int) -> str | None:
        topic = self.store.topic(topic_id)
        if topic["status"] != "open":
            return f"topic is {topic['status']}"

        pending = self.store.proposals(topic_id, status="open")
        if pending:
            # A proposal blocks once every seat has had its *chance* to respond --
            # not once every seat has cast a formal vote. Waiting for votes lets a
            # council that argues without calling mooting_vote debate forever with a
            # decision outstanding, which is exactly the routing-around this rule
            # exists to prevent. Seeing a proposal and saying nothing is abstention.
            cursors = {s["agent"]: s["last_seen"] for s in self.store.seats(topic_id)
                       if s["kind"] not in {"human", "external"}}
            for p in pending:
                opened_at = self.store.proposal_event_id(p["id"])
                voted = {v["agent"] for v in self.store.votes(p["id"])}
                outstanding = [
                    a for a, seen in cursors.items()
                    if a != p["author"] and a not in voted and seen < opened_at
                ]
                if not outstanding:
                    return f"proposal #{p['id']} ({p['title']!r}) awaits a human decision"

        # A question waits for its answer. If a human was asked, nobody else
        # speaks until they reply -- the alternative is a room that talks over the
        # person it just asked, and by the time they answer the debate has moved
        # on without the fact only they had.
        for m in self.store.open_mentions(topic_id, only_asks=True):
            if self.store.is_human(m["target"]):
                return (f"{m['asker']} is waiting on you: "
                        f"{addressed_to(m['question'], m['target'])}")
        if not self._eligible(topic_id):
            return self._human_ask_reason(topic_id)
        return None

    def _human_ask_reason(self, topic_id: int) -> str | None:
        """Why the council stopped, when what it is missing is *you*.

        Once nobody else can proceed, an unanswered question addressed to a human
        IS the reason the topic stopped. Reporting "rounds exhausted" instead
        would bury the one thing that needs a person, which is the failure this
        whole feature exists to prevent.
        """
        for m in self.store.open_mentions(topic_id, only_asks=True):
            if self.store.is_human(m["target"]):
                return (f"{m['asker']} is waiting on you: "
                        f"{addressed_to(m['question'], m['target'])}")
        return None

    def _eligible(self, topic_id: int) -> list[str]:
        """Every seat that has something to react to and budget to react with.

        A seat qualifies when its cursor is behind the board, which is what makes
        the loop terminate on its own: once everyone is caught up, the round
        yields nobody and the topic settles. Directed asks come first -- in
        sequential mode that is a real queue jump, and in concurrent mode it only
        orders the list, since everyone runs together anyway.
        """
        head = self.store.head()
        seats = {s["agent"]: s for s in self.store.seats(topic_id)}

        def can_speak(s) -> bool:
            if s["kind"] in {"human", "external"} or not s["enabled"]:
                return False        # humans answer on their own schedule
            if s["turns_used"] >= min(s["max_turns"], self.caps.max_turns_per_seat):
                self.store.set_seat_state(topic_id, s["agent"], "capped")
                return False
            if self.store.wakes_in_last_hour(s["agent"]) >= self.caps.max_wakes_per_agent_per_hour:
                self.store.set_seat_state(topic_id, s["agent"], "capped")
                return False
            return True

        ordered: list[str] = []
        # A mention buys priority, not budget: a capped seat is still not woken.
        asked = self.store.open_mentions(topic_id)
        for m in asked:
            s = seats.get(m["target"])
            if s is not None and s["agent"] not in ordered and can_speak(s):
                ordered.append(s["agent"])

        # An outstanding question narrows the round to whoever was asked. Letting
        # the others carry on means the answer arrives into a conversation that
        # has already moved past the question.
        if asked:
            return ordered

        for s in seats.values():
            if s["agent"] in ordered or s["last_seen"] >= head:
                continue
            if can_speak(s):
                ordered.append(s["agent"])
        return ordered

    def _next_speaker(self, topic_id: int) -> str | None:
        eligible = self._eligible(topic_id)
        return eligible[0] if eligible else None

    def _park(self, topic_id: int, reason: str) -> None:
        topic = self.store.topic(topic_id)
        if topic["status"] == "open":
            self.store.set_topic_status(topic_id, "paused", "mooting", reason)
            self.store.post(topic_id, "mooting", f"paused: {reason}", kind="system", count_turn=False)

    # ----------------------------------------------------------------- prompting

    def build_prompt(self, topic_id: int, agent: str, head: int | None = None) -> tuple[str, int]:
        """What the agent is told when woken, plus the cursor that covers it.

        The cursor is returned rather than committed, because it may only be
        advanced if the wake succeeds -- otherwise a dropped turn silently eats
        the messages the agent never saw.
        """
        topic = self.store.topic(topic_id)
        row = self.store.seat(topic_id, agent)
        cursor = row["last_seen"] if row else 0
        # A caller driving a concurrent round passes one shared head so every seat
        # in the round sees the same board.
        head = self.store.head() if head is None else head

        new = self.store.events_since(cursor, topic_id, limit=200, until=head)
        msg_ids = [e.payload.get("message_id") for e in new if e.kind == "message"]
        msgs = [m for m in self.store.transcript(topic_id) if m["id"] in set(filter(None, msg_ids))]

        seats = ", ".join(
            f"{s['agent']} ({s['kind']}, {s['turns_used']}/{min(s['max_turns'], self.caps.max_turns_per_seat)} turns)"
            for s in self.store.seats(topic_id)
        )
        turns_left = min(row["max_turns"], self.caps.max_turns_per_seat) - row["turns_used"]

        lines = [
            f"You are **{agent}**, holding a seat on a Mooting council.",
            "",
            f"## Topic: {topic['title']}  (`{topic['slug']}`, round {topic['round'] + 1}/{topic['max_rounds']})",
            "",
            # An agenda is worth naming as one. A seat handed a bare question
            # answers the question; a seat handed the points to settle works
            # through them, which is the difference between a chat and a meeting.
            *(["### Agenda", "", _agenda_of(topic), ""]
              if _agenda_of(topic) else []),
            *_attachment_section(self.store, topic_id,
                                 self.caps.max_attachment_chars),
            f"**Council:** {seats}",
            f"**Your budget:** {turns_left} turn(s) left.",
            "",
            FRAMING[topic["mode"]],
            "",
        ]

        # A direct ask goes first and is named as such. Burying "@you, what about
        # X?" inside a wall of catch-up is how a question gets answered vaguely or
        # not at all.
        asks = self.store.open_mentions(topic_id, agent)
        if asks:
            lines.append("## Asked of you directly")
            lines.append("")
            for a in asks:
                lines.append(f"**{a['asker']}** asked you:")
                lines.append(a["question"].strip())
                lines.append("")
            lines.append("Answer this first. Posting anything clears the question,")
            lines.append("so if you cannot answer it, say so explicitly rather than changing the subject.")
            lines.append("")

        if msgs:
            lines.append("## Since you last spoke")
            lines.append("")
            # Newest first into a budget, then flipped back: when there is too much
            # to replay, the recent exchange is what a seat needs to answer.
            kept, spent, elided = [], 0, 0
            for m in reversed([m for m in msgs if m["author"] != agent]):
                body = m["body"].strip()
                if spent + len(body) > self.caps.max_catchup_chars and kept:
                    elided += 1
                    continue
                kept.append((m, body))
                spent += len(body)
            if elided:
                lines.append(f"_({elided} earlier message(s) left out — "
                             f"`mooting_read` for the full transcript.)_")
                lines.append("")
            for m, body in reversed(kept):
                lines.append(f"**{m['author']}** ({m['kind']}):")
                lines.append(body)
                lines.append("")
        else:
            lines += ["## Since you last spoke", "", "_Nothing new -- you are opening._", ""]

        # Bounded by the same head, for the same reason as the events above.
        open_props = [p for p in self.store.proposals(topic_id, status="open")
                      if self.store.proposal_event_id(p["id"]) <= head]
        if open_props:
            lines.append("## Open proposals")
            lines.append("")
            for p in open_props:
                stances = ", ".join(f"{v['agent']}:{v['stance']}" for v in self.store.votes(p["id"])) or "no votes yet"
                lines.append(f"- **#{p['id']} {p['title']}** by {p['author']} — {stances}")
                lines.append(f"  {p['body'].strip()[:600]}")
            lines.append("")

        lines += [
            "## What to do now",
            "",
            f"Use the **`mooting-{agent}`** MCP server — that one, not any other "
            f"`mooting-*` server you can see. It is bound to your seat, and posting "
            f"through a different one would attribute your words to another "
            f"councillor (the board will refuse it).",
            "",
            "Your reply text here is not read by anyone —",
            "**only what you post through the tools reaches the council.**",
            "",
            "- `mooting_read(topic)` — full transcript, if the excerpt above is not enough.",
            "- `mooting_say(topic, body)` — argue, add evidence, or disagree. One point, made well.",
            "- `mooting_propose(topic, title, body)` — a concrete decision you want taken.",
            "- `mooting_ask(topic, agent, question)` — put a question to one councillor by name.",
            *(["- `mooting_assign(topic, agent, title, body, acceptance)` — draft a task (manager only).",
               "- `mooting_tasks(topic)` — the current plan and its state.",
               "- `mooting_task_update(task_id, status, result)` — `accepted` / `rejected` on finished work."]
              if topic["mode"] == "work" else []),
            "- `mooting_vote(proposal_id, stance, rationale)` — `support` / `object` / `abstain`.",
            "- `mooting_pass(topic, why)` — nothing to add. Passing is a real answer; say so and stop.",
            "",
            "You cannot approve a proposal, including your own. Votes are advisory;",
            "a human holds every decision. Do not edit files — this council deliberates.",
        ]
        return "\n".join(lines), head


async def run(store: Store, drivers: dict[str, Driver], topic_id: int, caps: Caps | None = None) -> str:
    return await Supervisor(store, drivers, caps).run_topic(topic_id)


def next_speaker(store: Store, topic_id: int, caps: Caps | None = None) -> str | None:
    """Exposed for `mooting status` and for tests that assert turn order."""
    return Supervisor(store, {}, caps)._next_speaker(topic_id)


__all__ = ["Supervisor", "Caps", "run", "next_speaker"]
