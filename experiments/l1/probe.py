"""L1 probe: how often does a claude seat answer in text and never call mooting_say?

Replays the two G4b panel briefs where claude went silent (capped c4 and c5) on
fresh seats, one turn each, and keeps every CLI's full stdout so a silent turn
can be read afterwards. Each trial gets its own seat, session and empty cwd.

Usage:
    python -X utf8 experiments/l1/probe.py <source board.db> <out dir> [--n 8] [--fake]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mooting.drivers import FakeDriver
from mooting.drivers.base import Driver
from mooting.drivers.registry import build_drivers
from mooting.store import connect
from mooting.supervisor import Caps, Supervisor

SEEDS = {"c4": "g4b-capped-c4_settings-panel", "c5": "g4b-capped-c5_jobs-panel"}
CHAIR = "chair"


def capture(out_dir: Path) -> None:
    """Keep the whole of every CLI's stdout, named by the seat that ran it."""
    orig = Driver._run

    async def _run(self, argv, *, cwd, stdin=None, timeout=None):
        code, out, err = await orig(self, argv, cwd=cwd, stdin=stdin, timeout=timeout)
        name = Path(cwd).name
        (out_dir / "stdout" / f"{name}.json").write_text(out, encoding="utf-8")
        if err.strip():
            (out_dir / "stdout" / f"{name}.err").write_text(err, encoding="utf-8")
        return code, out, err

    Driver._run = _run


async def trial(store, seat: str, slug: str, brief: str, fake: bool) -> dict:
    tid = store.open_topic(slug, slug, brief, CHAIR, seats=(seat, CHAIR),
                           max_rounds=1, effort="low")
    drivers = ({seat: FakeDriver(store)} if fake else build_drivers(store, only=[seat]))
    sup = Supervisor(store, drivers, Caps(max_rounds=1, max_turns_per_seat=1, effort="low"))
    reason = await sup.run_topic(tid)
    posts = [m for m in store.transcript(tid, limit=None) if m["author"] == seat]
    with store.tx() as c:
        c.execute("UPDATE topics SET status = 'resolved' WHERE id = ?", (tid,))
    return {"seat": seat, "topic": slug, "stopped": reason, "posted": len(posts)}


async def main_async(a) -> int:
    src = connect(a.source)
    briefs = {k: src.topic(slug)["brief"] for k, slug in SEEDS.items()}
    out = Path(a.out_dir)
    (out / "stdout").mkdir(parents=True, exist_ok=True)
    store = connect(out / "board.db", init=True)
    if CHAIR not in {x["name"] for x in store.agents()}:
        store.add_agent(CHAIR, "human", driver="none")
    capture(out)
    jobs = []
    for k, brief in briefs.items():
        for i in range(a.n):
            seat = f"l1{a.tag}{k}n{i}"
            cwd = out / "cwd" / seat
            cwd.mkdir(parents=True, exist_ok=True)
            store.add_agent(seat, "claude", driver="spawn", driver_cfg={"cwd": str(cwd)})
            jobs.append((seat, f"l1-{a.tag}-{k}-{i}", brief))
    results = []
    for start in range(0, len(jobs), a.batch):
        batch = jobs[start:start + a.batch]
        results += await asyncio.gather(*(trial(store, s, sl, b, a.fake) for s, sl, b in batch))
        for r in results[start:]:
            print(json.dumps(r), flush=True)
    silent = [r for r in results if r["posted"] == 0]
    print(f"silent {len(silent)} of {len(results)}", flush=True)
    (out / f"results_{a.tag}.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("out_dir")
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--tag", default="a", help="names this batch of trials")
    ap.add_argument("--fake", action="store_true")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
