"""Per-item table for the G4b report: which arm found which item.

Usage: python -X utf8 experiments/g4b/per_item.py <runs dir> <condition>
Written after the run, for the report's tables. It applies the same counting
rules as analyze.py but tests nothing: the confirmatory numbers come from
analyze.py alone.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from analyze import VENDORS, items_of  # noqa: E402


def main() -> int:
    root, cond = Path(sys.argv[1]), sys.argv[2]
    base = root / "scoring" / cond
    key = json.loads((base / "key.json").read_text(encoding="utf-8"))
    scores: dict[str, dict] = {}
    for f in (base / "scores").glob("*.json"):
        scores.update(json.loads(f.read_text(encoding="utf-8")))
    items = items_of("C") + items_of("R")
    solo = {v: set() for v in VENDORS}
    votes = {i: 0 for i in items}
    for label, who in key.items():
        seed = who["seed"].split("_")[0]
        found = {i for i in scores[label].get("found", []) if i.split(".")[0] == seed}
        if who["arm"] == "solo":
            solo[who["vendor"]] |= found
        else:
            for i in found:
                votes[i] += 1
    print(f"item   {'  '.join(f'{v:6}' for v in VENDORS)}  panel votes")
    for i in items:
        row = "  ".join(f"{'x' if i in solo[v] else '.':6}" for v in VENDORS)
        print(f"{i:6} {row}  {votes[i]}/3{'  *' if votes[i] >= 2 else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
