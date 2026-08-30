"""The surface an agent CLI sees: `python -m mooting.mcp_server --agent <name>`.

Each CLI spawns its own copy of this over stdio. They all write to one SQLite
board, which is why no daemon is required and why the council survives any single
CLI dying mid-turn.

Two deliberate omissions:

**There is no `mooting_decide` tool.** Not a disabled one, not one that checks a
flag -- it does not exist. `Store.decide` refuses non-humans as a backstop, but an
agent should never see an approve button in its tool list to begin with. Humans
decide through `mooting approve` / the web UI, on a different surface entirely.

**Identity comes from argv, never from the model.** The agent name is bound when
the CLI spawns this process, so a model cannot post as a peer by passing a
different `author`. Every tool here writes as `--agent`, full stop.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .store import Store, StoreError, connect

# Bound at startup from argv/env; see the module docstring on why this is not a
# tool parameter.
AGENT: str = os.environ.get("MOOTING_AGENT", "unknown")
BOARD: Store | None = None

mcp = FastMCP("mooting")


def board() -> Store:
    if BOARD is None:  # pragma: no cover - wired in main()
        raise RuntimeError("board not opened")
    return BOARD


def _topic_id(ref: str | int) -> int:
    return int(board().topic(int(ref) if str(ref).isdigit() else str(ref))["id"])


def _fmt_transcript(rows: list[Any], limit: int = 60) -> str:
    out = []
    for m in rows[-limit:]:
        tag = "" if m["kind"] == "say" else f" [{m['kind']}]"
        out.append(f"#{m['id']} **{m['author']}**{tag}\n{m['body'].strip()}")
    return "\n\n".join(out) if out else "_(nothing yet)_"


# ----------------------------------------------------------------------- read

@mcp.tool()
def mooting_inbox() -> str:
    """What has happened on your councils since you last caught up.

    Call this at the start of every turn. It is the poll-on-turn path that keeps
    working when the supervisor is not running or a wake failed.
    """
    b = board()
    parts: list[str] = []
    for t in b.topics():
        if t["status"] not in {"open", "paused"}:
            continue
        seat = b.seat(t["id"], AGENT)
        if seat is None:
            continue
        new = b.events_since(seat["last_seen"], t["id"])
        msg_ids = {e.payload.get("message_id") for e in new if e.kind == "message"}
        msgs = [m for m in b.transcript(t["id"]) if m["id"] in msg_ids and m["author"] != AGENT]
        openp = b.proposals(t["id"], status="open")

        head = f"### `{t['slug']}` — {t['title']}  ({t['status']}, round {t['round'] + 1}/{t['max_rounds']})"
        budget = f"turns used {seat['turns_used']}/{seat['max_turns']}"
        body = _fmt_transcript(msgs) if msgs else "_nothing new for you_"
        prop = ""
        if openp:
            prop = "\n\n**Open proposals awaiting a human decision:**\n" + "\n".join(
                f"- #{p['id']} {p['title']} (by {p['author']})" for p in openp
            )
        parts.append(f"{head}\n_{budget}_\n\n{body}{prop}")

    if not parts:
        return "No councils need you right now."
    return "\n\n---\n\n".join(parts)


@mcp.tool()
def mooting_read(topic: str, after: int = 0) -> str:
    """Full transcript of a topic. `topic` is its slug or id; `after` a message id."""
    tid = _topic_id(topic)
    t = board().topic(tid)
    rows = board().transcript(tid, after=after)
    return (
        f"# {t['title']} (`{t['slug']}`, {t['status']})\n\n{t['brief']}\n\n"
        f"---\n\n{_fmt_transcript(rows, limit=200)}"
    )


@mcp.tool()
def mooting_status() -> str:
    """Who holds which seat, on what, and how much budget each has left."""
    b = board()
    lines = []
    for t in b.topics():
        if t["status"] in {"resolved", "aborted"}:
            continue
        lines.append(f"`{t['slug']}` — {t['title']} [{t['status']}]")
        for s in b.seats(t["id"]):
            mark = " ←you" if s["agent"] == AGENT else ""
            lines.append(
                f"  {s['agent']:<10} {s['kind']:<9} {s['state']:<8} "
                f"{s['turns_used']}/{s['max_turns']} turns{mark}"
            )
    return "\n".join(lines) or "No active councils."


# ---------------------------------------------------------------------- write

@mcp.tool()
def mooting_say(topic: str, body: str) -> str:
    """Post an argument to the council.

    This is the only way your reasoning reaches anyone -- text you return to your
    own CLI is not read by the council. Make one point, with evidence, and stop.

    Writing `@name` puts that councillor next in line to respond, so use it when
    you want a specific person's answer rather than the room's. It does not stop
    the council: addressing part of your argument to somebody, or summarising for
    them, leaves everyone free to carry on. Use `mooting_ask` when you genuinely
    cannot continue without their answer.
    """
    tid = _topic_id(topic)
    try:
        mid = board().post(tid, AGENT, body)
    except StoreError as exc:
        return f"refused: {exc}"
    return f"posted #{mid} to `{topic}` as {AGENT}."


@mcp.tool()
def mooting_propose(topic: str, title: str, body: str) -> str:
    """Put a concrete decision to the council.

    A proposal is the only thing that can become action, and only a human closes
    it. Other agents may support or object; those votes are advisory. Propose when
    the discussion has converged enough that a human could rule on it -- state the
    decision, the reasoning, and what changes if it is approved.
    """
    tid = _topic_id(topic)
    try:
        pid = board().propose(tid, AGENT, title, body)
    except StoreError as exc:
        return f"refused: {exc}"
    return (
        f"proposal #{pid} opened on `{topic}`. It now waits for a human. "
        "You cannot approve it, including yourself."
    )


@mcp.tool()
def mooting_vote(proposal_id: int, stance: str, rationale: str = "") -> str:
    """Record an advisory stance: `support`, `object`, or `abstain`.

    Objecting is worth more than agreeing. If you object, say what specifically
    would have to be true for you to withdraw the objection.
    """
    try:
        board().vote(proposal_id, AGENT, stance, rationale)
    except StoreError as exc:
        return f"refused: {exc}"
    return f"{AGENT} recorded {stance} on proposal #{proposal_id}. A human still decides."


@mcp.tool()
def mooting_ask(topic: str, agent: str, question: str) -> str:
    """Ask one named councillor directly for their opinion.

    This is an @mention with a guaranteed target: it posts your question to the
    board and puts that seat next in line to answer, ahead of the normal rotation.

    It is also the only thing that will *stop* the council: if you ask a human,
    the room waits for their reply rather than talking past them. Naming somebody
    with `@name` inside `mooting_say` gives them priority without stopping
    anyone, so reserve this for when the answer is genuinely blocking.

    Use it when someone else holds the knowledge the argument turns on -- the seat
    that read the sources, or owns the subsystem. It buys them priority, not extra
    budget, so a capped seat still will not be woken.
    """
    tid = _topic_id(topic)
    try:
        mid = board().ask(tid, AGENT, agent, question)
    except StoreError as exc:
        return f"refused: {exc}"
    return f"asked {agent} directly (#{mid}). They answer next; their reply lands on `{topic}`."


@mcp.tool()
def mooting_pass(topic: str, why: str = "nothing to add") -> str:
    """End your turn without arguing. A real answer, not a failure.

    Use it when you agree, when the point is outside what you can judge, or when
    repeating yourself would just spend another metered turn.
    """
    tid = _topic_id(topic)
    board().post(tid, AGENT, why, kind="system", count_turn=True)
    return f"{AGENT} passed on `{topic}`."




# ----------------------------------------------------------------------- work

@mcp.tool()
def mooting_assign(topic: str, agent: str, title: str, body: str = "",
                 acceptance: str = "") -> str:
    """Draft a task for one teammate. Manager only, on a work topic.

    The task is a *draft*: it does not run, and nobody is woken for it, until a
    human approves the plan. Write `acceptance` as something checkable -- the
    worker is told it, and you will be judging against it.

    Assign to the seat actually suited to the work, and only to seats registered
    with execute capability; a task assigned to a deliberation-only seat comes
    back blocked rather than silently doing nothing.
    """
    tid = _topic_id(topic)
    try:
        task_id = board().draft_task(tid, AGENT, agent, title, body, acceptance)
    except StoreError as exc:
        return f"refused: {exc}"
    return (f"task #{task_id} drafted for {agent}. It stays a draft until a human "
            f"approves the plan — draft the rest, then stop.")


@mcp.tool()
def mooting_tasks(topic: str) -> str:
    """The plan and where every task has got to."""
    tid = _topic_id(topic)
    rows = board().tasks(tid)
    if not rows:
        return "No tasks planned yet."
    out = []
    for t in rows:
        line = f"#{t['id']} [{t['status']}] {t['title']} — {t['assignee']}"
        if t["branch"]:
            line += f"  ({t['branch']})"
        out.append(line)
        if t["acceptance"]:
            out.append(f"    done when: {t['acceptance']}")
        if t["result"]:
            out.append(f"    reported: {t['result'][:400]}")
    return "\n".join(out)


@mcp.tool()
def mooting_task_update(task_id: int, status: str, result: str = "") -> str:
    """Report on your task, or sign off on someone else's if you are the manager.

    As the worker: `in_progress`, `done`, or `blocked`. Say concretely what you
    changed and where, or precisely what stopped you -- your report is all the
    manager and the human will see.

    As the manager: `accepted` or `rejected`, with the reason.
    """
    try:
        board().update_task(task_id, AGENT, status, result)
    except StoreError as exc:
        return f"refused: {exc}"
    return f"task #{task_id} → {status}."


def main(argv: list[str] | None = None) -> int:
    global AGENT, BOARD
    ap = argparse.ArgumentParser(description="Mooting MCP server (stdio, one per agent CLI)")
    ap.add_argument("--agent", default=os.environ.get("MOOTING_AGENT"),
                    help="seat name this CLI posts as; binds identity for the session")
    ap.add_argument("--db", default=os.environ.get("MOOTING_DB"), help="path to board.db")
    args = ap.parse_args(argv)

    if not args.agent:
        ap.error("--agent is required (or set MOOTING_AGENT)")

    AGENT = args.agent
    BOARD = connect(Path(args.db) if args.db else None)
    try:
        BOARD.agent(AGENT)
    except StoreError:
        print(f"mooting: unknown seat {AGENT!r}; run `mooting agents add {AGENT} <kind>` first",
              file=sys.stderr)
        return 2

    mcp.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
