"""G4b: does a council beat the best single vendor on code review?

Per seed: three solo reviews (claude, codex, agy, one turn each, each alone on its
own topic), then one panel round in which three fresh seats of the same vendors
each read the code and all three reviews and say which bugs are real. 6 turns a
seed; set C (8 written seeds) and set R (8 real past bugs) make 96, and the two
conditions (`capped`: the shipped length rule; `free`: lifted) make 192.

Usage:
    python -X utf8 experiments/g4b/run.py <runs dir> --condition free --fake     # spends nothing
    python -X utf8 experiments/g4b/run.py <runs dir> --condition capped --fake --mute g4bscodex
    python -X utf8 experiments/g4b/run.py <runs dir> --condition capped           # real, 96 turns
    python -X utf8 experiments/g4b/run.py <runs dir> --condition free             # real, 96 turns
    python -X utf8 experiments/g4b/run.py <runs dir> --cleanup       # drop the codex/agy registrations

The registration is docs/science-superpowers/preregistrations/2026-11-15-g4b-council-vs-best-vendor.md.
A change to this file after the freeze is a deviation and goes in the report.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

from mooting.drivers import FakeDriver
from mooting.drivers.registry import build_drivers
from mooting.install import install_seat, uninstall_cmd
from mooting.store import connect
from mooting.supervisor import Caps, Supervisor

HERE = Path(__file__).parent
SETS = {"C": sorted((HERE / "seeds").glob("c*.py")),
        "R": sorted((HERE / "real").glob("r*.py"))}
VENDORS = ["claude", "codex", "agy"]
SOLO = {v: f"g4bs{v}" for v in VENDORS}
PANEL = {v: f"g4bp{v}" for v in VENDORS}
CHAIR = "chair"
LETTERS = "ABC"
EFFORT = "low"                      # the shipped default, and what G4 used
MUTE = "(no review: this reviewer did not post)"
CONDITIONS = ("capped", "free")


def lift_length_limit() -> None:
    """The `free` condition: drop the length rule from every seat's prompt.

    At effort low each seat is told to stay under 80 words with at most two
    supporting points, and a trial showed a lone review dropping a real bug to fit.
    Three solos get six points between them, so a council could win on slot count
    alone. This changes the prompt for this experiment only; the product is not
    touched.
    """
    orig = Supervisor.build_prompt
    if getattr(orig, "_g4b_free", False):
        return

    def build_prompt(self, topic_id, agent, head=None):
        prompt, cursor = orig(self, topic_id, agent, head=head)
        start = prompt.find("## How to say it")
        end = prompt.find("## What to do now")
        if start == -1 or end == -1 or end < start:
            raise SystemExit("free condition: the prompt no longer has the length section")
        prompt = prompt[:start] + prompt[end:]
        return prompt.replace(" One point, briefly.", ""), cursor

    build_prompt._g4b_free = True
    Supervisor.build_prompt = build_prompt

CONTEXT = {
    "C": "",
    "R": ("The code is from Mooting, a tool that seats several coding-agent CLIs at one "
          "board to discuss a person's code while a human chairs. Each seat is a CLI "
          "subprocess spawned once per turn; agents reach the board through an MCP server, "
          "people through a terminal, a Telegram or Discord chat, or a web page. One board "
          "holds many meetings, each with its own seats. It runs on Windows, macOS and "
          "Linux. Lines marked `# ...` were left out.\n\n"),
}

SOLO_PROMPT = (
    "{context}Review the Python code below for bugs. For each bug, name the function, an "
    "input that triggers it, and what goes wrong. If you find none, say so. Leave style "
    "alone. Review only the code in this brief; do not open files.\n\n```python\n{code}```\n"
)

PANEL_PROMPT = (
    "{context}Below is some Python code and three reviews of it, A, B and C, written "
    "independently. Check each reported bug against the code and say whether it is real, "
    "with the reason, and add any real bug all three missed. Review only the code in this "
    "brief; do not open files. End with a line `FINAL:` followed by the bugs you hold to "
    "be real, one per line, each naming the function and the cause.\n\n```python\n{code}```\n\n{reviews}"
)


def rotated(k: int) -> list[str]:
    return VENDORS[k % 3:] + VENDORS[:k % 3]


def setup(store, fake: bool) -> None:
    have = {a["name"] for a in store.agents()}
    if CHAIR not in have:
        store.add_agent(CHAIR, "human", driver="none")
    for v in VENDORS:
        for name in (SOLO[v], PANEL[v]):
            if name not in have:
                # A seat with no cwd runs wherever this script was started, which
                # is the repo: claude and agy would then load CLAUDE.md, and it
                # describes three of set R's bugs. An empty folder of its own each.
                cwd = Path(store.path).parent / "cwd" / name
                cwd.mkdir(parents=True, exist_ok=True)
                store.add_agent(name, v, driver="spawn", driver_cfg={"cwd": str(cwd)})
    if not fake:
        # codex and agy load their server from their own global config, one entry
        # per seat name. These names are new, so nothing the owner had is touched.
        for v in ("codex", "agy"):
            for name in (SOLO[v], PANEL[v]):
                ok, msg = install_seat(store, name)
                print(f"install {name}: {ok} {msg}", flush=True)
                if not ok:
                    raise SystemExit(f"could not register {name}: {msg}")


def said(store, tid: int, seat: str) -> list[str]:
    """What a seat put on a topic. A pass is a system row in its name."""
    out = []
    for m in store.transcript(tid, limit=None):
        if m["author"] != seat:
            continue
        out.append(m["body"] if m["kind"] != "system" else f"(the reviewer passed: {m['body']})")
    return out


def open_topics_held(store, seat: str) -> int:
    return sum(1 for t in store.topics() if t["status"] in {"open", "paused"}
               and store.seat(t["id"], seat) is not None)


def topic(store, slug: str, title: str, brief: str, seats: list[str]) -> int:
    try:
        return int(store.topic(slug)["id"])
    except Exception:
        return store.open_topic(slug, title, brief, CHAIR, seats=(*seats, CHAIR),
                                max_rounds=1, effort=EFFORT)


async def drive(store, tid: int, names: list[str], fake: bool, mute: set[str]) -> str:
    for n in names:
        # A seat's inbox lists every open topic it holds. Exactly one, or a solo
        # reviewer could read another vendor's answer on the way to its own.
        held = open_topics_held(store, n)
        if held != 1:
            raise SystemExit(f"{n} holds {held} open topics before its wake; expected 1")
    if fake:
        drivers = {n: FakeDriver(store, fail_agents=mute & set(names)) for n in names}
    else:
        drivers = build_drivers(store, only=names)
    sup = Supervisor(store, drivers, Caps(max_rounds=1, max_turns_per_seat=1, effort=EFFORT))
    return await sup.run_topic(tid)


def wakes(store, tid: int) -> list[dict]:
    return [dict(r) for r in store.q(
        "SELECT agent, outcome, detail, tokens_in, tokens_out, cost_usd, started_at, "
        "ended_at FROM wakes WHERE topic_id = ? ORDER BY id", (tid,))]


def finished(store, tid: int) -> bool:
    return store.topic(tid)["status"] == "resolved"


def close(store, tid: int) -> None:
    if not finished(store, tid):
        store.set_topic_status(tid, "resolved", CHAIR, "g4b: done")


def stop_if_capped(reason: str) -> None:
    # Caps pause, they never silently continue: a capped seat mid-run would turn
    # every later turn into a missing value, so the run stops and says why.
    if "capped" in reason or "per hour" in reason:
        raise SystemExit(f"stopped on a cap: {reason}. Rerun later; finished topics are kept.")


def run_seed(store, cond: str, set_name: str, k: int, path: Path, fake: bool, mute: set[str],
             out: Path) -> None:
    seed = path.stem
    code = path.read_text(encoding="utf-8")
    ctx = CONTEXT[set_name]
    record: dict = {"condition": cond, "set": set_name, "seed": seed, "effort": EFFORT,
                    "solo": {}, "panel": {}}

    for v in rotated(k):
        slug = f"g4b-{cond}-{seed}-solo-{v}"
        tid = topic(store, slug, f"G4b {seed} solo {v}",
                    SOLO_PROMPT.format(context=ctx, code=code), [SOLO[v]])
        if finished(store, tid):
            reason = "(already run)"
        else:
            reason = asyncio.run(drive(store, tid, [SOLO[v]], fake, mute))
            print(f"{seed} solo {v}: {reason}", flush=True)
            stop_if_capped(reason)          # before closing, so a rerun resumes it
            close(store, tid)
        record["solo"][v] = {"topic": slug, "stopped": reason,
                             "said": said(store, tid, SOLO[v]), "wakes": wakes(store, tid)}

    letters = dict(zip(LETTERS, rotated(k)))            # A -> vendor, rotated per seed
    reviews = "\n\n".join(
        f"## Review {L}\n\n" + ("\n\n".join(record["solo"][v]["said"]) or MUTE)
        for L, v in letters.items())
    brief = PANEL_PROMPT.format(context=ctx, code=code, reviews=reviews)
    for v, r in record["solo"].items():
        for body in r["said"]:
            assert body in brief, f"{seed}: {v}'s review is missing from the panel brief"
    slug = f"g4b-{cond}-{seed}-panel"
    seats = [PANEL[v] for v in VENDORS]
    tid = topic(store, slug, f"G4b {seed} panel", brief, seats)
    if finished(store, tid):
        reason = "(already run)"
    else:
        reason = asyncio.run(drive(store, tid, seats, fake, mute))
        print(f"{seed} panel: {reason}", flush=True)
        stop_if_capped(reason)
        close(store, tid)
    record["letters"] = letters
    record["panel"] = {"topic": slug, "stopped": reason, "brief_chars": len(brief),
                       "said": {v: said(store, tid, PANEL[v]) for v in VENDORS},
                       "wakes": wakes(store, tid)}
    (out / f"{seed}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1),
                                      encoding="utf-8")


def cleanup(store) -> None:
    for v in ("codex", "agy"):
        for name in (SOLO[v], PANEL[v]):
            cmd = uninstall_cmd(v, name)
            exe = shutil.which(cmd[0]) if cmd else None
            if exe is None:
                print(f"{name}: {cmd[0] if cmd else v} not found, nothing removed")
                continue
            r = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            print(f"{name}: {' '.join(cmd)} -> exit {r.returncode}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs_dir")
    ap.add_argument("--fake", action="store_true")
    ap.add_argument("--mute", action="append", default=[], help="(fake) a seat whose wake fails")
    ap.add_argument("--only", choices=sorted(SETS), help="run one set")
    ap.add_argument("--condition", choices=CONDITIONS, required=False,
                    help="capped = the shipped length rule; free = lifted. Required to run.")
    ap.add_argument("--cleanup", action="store_true")
    a = ap.parse_args()
    root = Path(a.runs_dir)
    root.mkdir(parents=True, exist_ok=True)
    store = connect(root / "board.db", init=True)
    if a.cleanup:
        cleanup(store)
        return 0
    if a.condition is None:
        ap.error("--condition capped|free is required")
    if a.condition == "free":
        lift_length_limit()
    out = root / "out" / a.condition
    out.mkdir(parents=True, exist_ok=True)
    setup(store, a.fake)
    for set_name in ([a.only] if a.only else ["C", "R"]):
        for k, path in enumerate(SETS[set_name]):
            run_seed(store, a.condition, set_name, k, path, a.fake, set(a.mute), out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
