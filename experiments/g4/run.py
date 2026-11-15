"""G4: does a three-seat council find more seeded defects than one seat alone?

Usage:
    python -X utf8 experiments/g4/run.py <runs dir> --fake     # dry run, spends nothing
    python -X utf8 experiments/g4/run.py <runs dir>            # the real run: 35 turns

The registration is docs/science-superpowers/preregistrations/2026-11-15-g4-council-vs-solo.md.
Everything here follows it; a change to this file after the freeze is a deviation
and goes in the report.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mooting.drivers import FakeDriver
from mooting.drivers.registry import build_drivers
from mooting.install import install_seat
from mooting.store import connect
from mooting.supervisor import Caps, Supervisor

SEEDS = ["s1_page", "s2_safe_join", "s3_retry", "s4_tokens", "s5_stats"]
COUNCIL = ["g4claude", "g4codex", "g4agy"]
SOLO = "g4solo"
CHAIR = "chair"

PROMPT = (
    "Review the Python code below for bugs. For each bug, name the function, an "
    "input that triggers it, and what goes wrong. If you find none, say so. "
    "Leave style alone.\n\n```python\n{code}```\n"
)


def setup(store, fake: bool) -> None:
    have = {a["name"] for a in store.agents()}
    for name, kind in [(CHAIR, "human"), ("g4claude", "claude"), ("g4codex", "codex"),
                       ("g4agy", "agy"), (SOLO, "claude")]:
        if name not in have:
            store.add_agent(name, kind, driver="spawn" if kind != "human" else "none")
    if not fake:
        # codex and agy load their server from their own global config, one entry
        # per seat name. These names are new, so nothing the owner had is touched.
        for name in ("g4codex", "g4agy"):
            ok, msg = install_seat(store, name)
            print(f"install {name}: {ok} {msg}", flush=True)


def topic_for(store, seed: str, arm: str) -> int:
    slug = f"g4-{seed.replace('_', '-')}-{arm}"
    try:
        return int(store.topic(slug)["id"])
    except Exception:
        pass
    code = (Path(__file__).parent / "seeds" / f"{seed}.py").read_text(encoding="utf-8")
    seats = (*COUNCIL, CHAIR) if arm == "council" else (SOLO, CHAIR)
    return store.open_topic(slug, f"G4 {seed} {arm}", PROMPT.format(code=code), CHAIR,
                            seats=seats, max_rounds=2 if arm == "council" else 1,
                            effort="low")


async def run_one(store, tid: int, arm: str, fake: bool) -> str:
    names = COUNCIL if arm == "council" else [SOLO]
    if fake:
        drivers = {n: FakeDriver(store) for n in names}
    else:
        drivers = build_drivers(store, only=names)
    rounds = 2 if arm == "council" else 1
    sup = Supervisor(store, drivers, Caps(max_rounds=rounds, max_turns_per_seat=rounds,
                                          effort="low"))
    return await sup.run_topic(tid)


def export(store, tid: int, seed: str, arm: str, reason: str, out: Path) -> None:
    msgs = [{"id": m["id"], "author": m["author"], "kind": m["kind"], "body": m["body"]}
            for m in store.transcript(tid, limit=None)]
    wakes = [dict(r) for r in store.q(
        "SELECT agent, outcome, detail, tokens_in, tokens_out, cost_usd, started_at, "
        "ended_at FROM wakes WHERE topic_id = ? ORDER BY id", (tid,))]
    (out / f"{seed}-{arm}.json").write_text(json.dumps(
        {"seed": seed, "arm": arm, "stopped": reason, "messages": msgs, "wakes": wakes},
        ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs_dir")
    ap.add_argument("--fake", action="store_true")
    a = ap.parse_args()
    root = Path(a.runs_dir)
    out = root / "out"
    out.mkdir(parents=True, exist_ok=True)
    store = connect(root / "board.db", init=True)
    setup(store, a.fake)
    for seed in SEEDS:
        for arm in ("council", "solo"):
            tid = topic_for(store, seed, arm)
            if store.transcript(tid, limit=None) and any(
                    m["author"] != CHAIR and m["kind"] != "system"
                    for m in store.transcript(tid, limit=None)):
                print(f"{seed} {arm}: already has a transcript, skipped", flush=True)
                continue
            reason = asyncio.run(run_one(store, tid, arm, a.fake))
            export(store, tid, seed, arm, reason, out)
            n = sum(1 for m in store.transcript(tid, limit=None) if m["author"] != CHAIR)
            print(f"{seed} {arm}: {reason} ({n} messages)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
