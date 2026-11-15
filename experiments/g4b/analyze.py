"""The registered analysis. Reads the scorer's verdicts and the unblinding key.

Usage: python -X utf8 experiments/g4b/analyze.py <runs dir> <capped|free>
Reads <runs dir>/scoring/<condition>/key.json and .../scores/<seed>.json, where
a scores file is {label: {"found": [item ids], "claims": [{"claim": str,
"verdict": "real" | "false" | "hedged" | "unverifiable"}]}} for every label on
that seed's sheet. Prints the primary result first, then the secondary ones.
"""
from __future__ import annotations

import json
import sys
from math import comb
from pathlib import Path

HERE = Path(__file__).parent
VENDORS = ["claude", "codex", "agy"]
#: Two primary tests, one per condition: Bonferroni.
ALPHA = 0.025


def items_of(set_name: str) -> list[str]:
    path = HERE / ("ANSWER_KEY.md" if set_name == "C" else "ANSWER_KEY_R.md")
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.startswith("| ") and ln[2:3] in "cr" and "." in ln[2:8]:
            out.append(ln.split("|")[1].strip())
    return out


def sign_p(wins: int, losses: int) -> float:
    """One-sided exact sign test: P(X >= wins) for X ~ Bin(wins + losses, 1/2)."""
    n = wins + losses
    if n == 0:
        return 1.0
    return sum(comb(n, i) for i in range(wins, n + 1)) / 2 ** n


def main() -> int:
    root, cond = Path(sys.argv[1]), sys.argv[2]
    base = root / "scoring" / cond
    key = json.loads((base / "key.json").read_text(encoding="utf-8"))
    records = [json.loads(f.read_text(encoding="utf-8"))
               for f in sorted((root / "out" / cond).glob("*.json"))]
    scores: dict[str, dict] = {}
    for f in (base / "scores").glob("*.json"):
        scores.update(json.loads(f.read_text(encoding="utf-8")))
    missing = sorted(set(key) - set(scores))
    if missing:
        raise SystemExit(f"no score for {len(missing)} texts, e.g. {missing[:3]}")

    items = items_of("C") + items_of("R")
    seed_of = {i: i.split(".")[0] for i in items}
    solo: dict[str, set[str]] = {v: set() for v in VENDORS}
    panel_votes: dict[str, int] = {i: 0 for i in items}
    claims = {"solo": [], "panel": []}
    for label, who in key.items():
        s = scores[label]
        found = {i for i in s.get("found", []) if i in seed_of
                 and seed_of[i] == who["seed"].split("_")[0]}
        if who["arm"] == "solo":
            solo[who["vendor"]] |= found
        else:
            for i in found:
                panel_votes[i] += 1
        claims[who["arm"]].append([c["verdict"] for c in s.get("claims", [])])

    D = {i for i, n in panel_votes.items() if n >= 2}
    U = set().union(*solo.values())
    best_n = max(len(s) for s in solo.values())
    best = [v for v in VENDORS if len(solo[v]) == best_n]

    print(f"items: {len(items)} ({len(items_of('C'))} in C, {len(items_of('R'))} in R)")
    for v in VENDORS:
        print(f"  solo {v:6} {len(solo[v]):2}")
    print(f"  union  {len(U):2}    panel (2 of 3) {len(D):2}")
    print()
    turns = failed = 0
    cost = {"solo": [0, 0.0, 0, 0], "panel": [0, 0.0, 0, 0]}   # turns, seconds, tokens in, out
    for r in records:
        rows = [("solo", w) for s in r["solo"].values() for w in s["wakes"]]
        rows += [("panel", w) for w in r["panel"]["wakes"]]
        for arm, w in rows:
            turns += 1
            failed += w["outcome"] != "ok"
            c = cost[arm]
            c[0] += 1
            if w["started_at"] and w["ended_at"]:
                from datetime import datetime
                fmt = "%Y-%m-%d %H:%M:%S"
                c[1] += (datetime.strptime(w["ended_at"][:19], fmt)
                         - datetime.strptime(w["started_at"][:19], fmt)).total_seconds()
            c[2] += w["tokens_in"] or 0
            c[3] += w["tokens_out"] or 0
    silent = sum(1 for r in records for s in r["solo"].values() if not s["said"]) +         sum(1 for r in records for said in r["panel"]["said"].values() if not said)
    print(f"condition {cond}: {turns} turns, {failed} failed, {silent} seats posted nothing")
    bad = failed + silent > 10
    if bad:
        print("  more than 10 turns failed or were silent: INCONCLUSIVE whatever p says (registered)")
    print()
    print(f"PRIMARY: panel against the best single vendor, one-sided exact sign test, alpha {ALPHA}")
    verdicts = []
    for v in best:
        wins, losses = len(D - solo[v]), len(solo[v] - D)
        p = sign_p(wins, losses)
        lost = sign_p(losses, wins)
        verdict = ("H1 confirmed" if p < ALPHA else
                   "panel worse (registered disconfirmation)" if lost < ALPHA else "null")
        verdicts.append(verdict)
        print(f"  vs {v}: panel-only {wins}, {v}-only {losses}, p = {p:.4f} -> {verdict}")
    print(f"  => {'H1 confirmed' if all(x == 'H1 confirmed' for x in verdicts) else verdicts[0] if len(set(verdicts)) == 1 else 'null (differs across tied best vendors)'}")
    print()
    print("SECONDARY (registered, not confirmatory)")
    for v in best:
        w, l = len(U - solo[v]), len(solo[v] - U)
        print(f"  S1 union vs {v}: union-only {w}, {v}-only {l}, p = {sign_p(w, l):.4f}")
    print(f"  S2 panel vs union: lost {sorted(U - D)}  gained {sorted(D - U)}")
    for arm in ("solo", "panel"):
        rows = claims[arm]
        n = len(rows) or 1
        false = sum(r.count("false") for r in rows)
        hedged = sum(r.count("hedged") for r in rows)
        print(f"  S3 {arm:5}: {len(rows)} texts, false alarms {false} ({false / n:.2f} a text), hedged {hedged}")
    for set_name in ("C", "R"):
        its = set(items_of(set_name))
        line = "  ".join(f"{v} {len(solo[v] & its)}" for v in VENDORS)
        print(f"  S5 set {set_name}: {line}  union {len(U & its)}  panel {len(D & its)}  of {len(its)}")
    for arm, (n, secs, tin, tout) in cost.items():
        print(f"  S4 {arm:5}: {n} turns, {secs / 60:.1f} min of wake time, tokens in {tin} out {tout}"
              " (only where the CLI reports them)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
