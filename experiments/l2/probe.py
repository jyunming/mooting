"""L2 probe: a real seat posts, then runs out of time before it exits.

The seat is asked to post one line and then keep writing, and its per-wake limit
is set short, so the post lands before the limit and the exit does not. Each
trial is its own topic. An agy or codex seat is registered once and removed at
the end; claude takes its server per run. Prints, per trial, what the board recorded.

Usage:
    python -X utf8 experiments/l2/probe.py <out dir> [--kind claude] [--n 3] [--timeout 40]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

from mooting.drivers.registry import build_drivers
from mooting.install import install_seat, uninstall_cmd
from mooting.store import connect
from mooting.supervisor import Caps, Supervisor

CHAIR = "chair"
SEAT = ""                           # set from --kind
BRIEF = ("This is a timing probe, not a debate. First, call mooting_say on this topic with "
         "the body `L2 probe: posted.` Then, after that call has returned, write your final "
         "reply: an essay of about 800 words on the history of the printing press. Do not "
         "post the essay; it is only your reply text.")


async def trial(store, i: int, timeout: float) -> dict:
    tid = store.open_topic(f"l2-{i}", f"L2 probe {i}", BRIEF, CHAIR, seats=(SEAT, CHAIR),
                           max_rounds=1, effort="low")
    sup = Supervisor(store, build_drivers(store, only=[SEAT]),
                     Caps(max_rounds=1, max_turns_per_seat=1, effort="low"))
    await sup.run_topic(tid)
    wake = dict(store.q1("SELECT outcome, detail, started_at, ended_at FROM wakes "
                         "WHERE topic_id = ? ORDER BY id DESC", (tid,)))
    seat = store.seat(tid, SEAT)
    notes = [m["body"] for m in store.transcript(tid, limit=None) if m["author"] == "mooting"
             and "round" not in m["body"] and "paused" not in m["body"]]
    posts = [m["body"] for m in store.transcript(tid, limit=None) if m["author"] == SEAT]
    with store.tx() as c:
        c.execute("UPDATE topics SET status = 'resolved' WHERE id = ?", (tid,))
    return {"trial": i, "posted": posts, "wake": wake, "seat_state": seat["state"],
            "cursor_moved": seat["last_seen"] > 0, "notes": notes}


async def main_async(a) -> int:
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    store = connect(out / "board.db", init=True)
    have = {x["name"] for x in store.agents()}
    if CHAIR not in have:
        store.add_agent(CHAIR, "human", driver="none")
    cwd = out / "cwd" / SEAT
    cwd.mkdir(parents=True, exist_ok=True)
    store.add_agent(SEAT, a.kind, driver="spawn",
                    driver_cfg={"cwd": str(cwd), "timeout_s": a.timeout})
    ok, msg = install_seat(store, SEAT)
    print(f"install: {ok} {msg}", flush=True)
    if not ok:
        return 1
    try:
        results = []
        for i in range(a.n):
            r = await trial(store, a.start + i, a.timeout)
            print(json.dumps(r, ensure_ascii=False), flush=True)
            results.append(r)
        (out / f"results_{a.tag}.json").write_text(
            json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
    finally:
        cmd = uninstall_cmd(a.kind, SEAT)
        if cmd:
            proc = subprocess.run([shutil.which(cmd[0]) or cmd[0], *cmd[1:]],
                                  capture_output=True, text=True)
            print(f"uninstall: exit {proc.returncode}", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=40.0)
    ap.add_argument("--tag", default="after")
    ap.add_argument("--kind", default="agy", choices=["agy", "codex", "claude"])
    a = ap.parse_args()
    global SEAT
    SEAT = f"l2{a.kind}"
    return asyncio.run(main_async(a))


if __name__ == "__main__":
    sys.exit(main())
