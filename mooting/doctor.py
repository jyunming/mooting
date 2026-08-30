"""Per-CLI smoke test: does this adapter actually work, end to end?

A driver returning exit 0 proves nothing. The CLI can start, load the MCP server,
decide not to call it, and exit clean -- and a check on the return code goes green
while the seat is mute. So every probe here asserts on **what landed on the
board**: the message must be there, written by the right author, containing the
token the prompt asked for.

That distinction is the whole file. Anything cheaper reports success for a council
that cannot speak.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from dataclasses import dataclass

from .drivers.base import Seat
from .drivers.spawn import DRIVER_CLASSES
from .store import Store

PROBE_TOKEN = "MOOTING-PROBE-OK"

PROBE_PROMPT = f"""Call the `mooting_say` tool with topic="{{slug}}" and body="{{token}}". Do only that.

The tool is served by an MCP server named `mooting` (or `mooting-<seat>`). It may be
namespaced (`mooting-codex/mooting_say`, `mcp__mooting__mooting_say`) and may be deferred
until searched for -- find it and call it.

Call no other tool. Do not read files, run commands, or explore. If the
`mooting_say` call itself fails, reply NO-MOOTING-TOOLS and the error it gave.
"""


@dataclass
class Probe:
    agent: str
    kind: str
    ok: bool
    detail: str
    session: str | None = None
    stateful: bool = False

    def line(self) -> str:
        mark = "PASS" if self.ok else "FAIL"
        sess = ""
        if self.stateful:
            sess = f"  session={'captured' if self.session else 'NOT CAPTURED'}"
        return f"[{mark}] {self.agent:<12} {self.kind:<9} {self.detail}{sess}"


async def probe_agent(board: Store, agent: str, kind: str, timeout: float) -> Probe:
    cls = DRIVER_CLASSES.get(kind)
    if cls is None:
        return Probe(agent, kind, False, "no driver for this kind")
    if shutil.which(cls.binary) is None:
        return Probe(agent, kind, False, f"{cls.binary} not on PATH")

    slug = f"doctor-{agent}"
    existing = [t for t in board.topics() if t["slug"] == slug]
    tid = int(existing[0]["id"]) if existing else board.open_topic(
        slug, f"Installation self-test for {agent}",
        "Throwaway topic used by `mooting doctor`. Safe to delete.",
        "mooting", seats=(agent,), max_rounds=1, max_turns=99,
    )
    if existing and existing[0]["status"] != "open":
        board.set_topic_status(tid, "open", "mooting", "doctor re-run")

    before = {m["id"] for m in board.transcript(tid)}
    driver = cls(board.path, timeout_s=timeout)
    seat_row = board.seat(tid, agent)
    seat = Seat(tid, slug, agent, kind, seat_row["cli_session"] if seat_row else None)

    result = await driver.wake(seat, PROBE_PROMPT.format(slug=slug, token=PROBE_TOKEN))

    # The load-bearing assertion: did the agent actually reach the board?
    posted = [m for m in board.transcript(tid)
              if m["id"] not in before and m["author"] == agent and PROBE_TOKEN in m["body"]]

    if posted:
        if result.cli_session:
            board.set_cli_session(tid, agent, result.cli_session)
        return Probe(agent, kind, True, "reached the board", result.cli_session, driver.stateful)

    if "NO-MOOTING-TOOLS" in (result.tail or ""):
        return Probe(agent, kind, False, _no_tools_hint(kind, agent))

    if not result.ok:
        return Probe(agent, kind, False, f"wake failed: {result.detail}")
    return Probe(agent, kind, False, "CLI ran clean but posted nothing (mute seat)")


def _no_tools_hint(kind: str, agent: str) -> str:
    """The CLI started and answered, but saw no Mooting tools.

    Worth separating from a crash, because the usual cause is not Mooting at all.
    On this machine codex loaded *none* of its MCP servers -- not axon, not
    node_repl, not mooting -- because one unauthenticated HTTP server killed the
    shared rmcp worker (`Transport channel closed, when AuthRequired`) and took
    the whole subsystem down with it. Telling someone to check their
    `--mcp-config` when their CLI's MCP support is broken outright sends them
    debugging the wrong thing.
    """
    if kind in {"codex", "gemini"}:
        return (f"CLI saw no mooting tools. First: `mooting install {agent}`. "
                f"If that is already done, check whether this CLI loads ANY MCP server "
                f"(`{kind} mcp list`, then ask it to name a tool from another server) -- "
                f"one broken server can disable them all.")
    return "MCP server not visible; check the per-run --mcp-config injection."


async def run_doctor(board: Store, only: str | None = None, timeout: float = 180.0) -> int:
    wanted = {s.strip() for s in only.split(",")} if only else None
    seats = [a for a in board.agents()
             if a["kind"] in DRIVER_CLASSES and (not wanted or a["name"] in wanted)]

    if not seats:
        print("no agent seats registered. Try: mooting agents add claude claude --cwd .")
        return 1

    print(f"board: {board.path}")
    print(f"probing {len(seats)} seat(s); each spends one real turn on that CLI.\n")

    # Sequential on purpose: a parallel probe makes a rate-limit look like a bug.
    probes = [await probe_agent(board, a["name"], a["kind"], timeout) for a in seats]
    for p in probes:
        print(p.line())

    failed = [p for p in probes if not p.ok]
    print()
    if failed:
        print(f"{len(failed)} of {len(probes)} seat(s) cannot reach the board.")
        print("A failing seat is safe to leave registered -- the council runs without it,")
        print("and the supervisor records the failed wake rather than stalling the topic.")
    else:
        print(f"all {len(probes)} seat(s) reached the board.")
    return 1 if failed else 0
