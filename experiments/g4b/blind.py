"""Turn the run's records into scoring sheets that hide who wrote what.

Usage: python -X utf8 experiments/g4b/blind.py <runs dir> <capped|free>
Writes <runs dir>/scoring/<condition>/sheets/<seed>.md and .../key.json.
The scorer sees only the sheets. A sheet keeps the panel apart from the solos,
because a panel message quotes "Review A" and cannot be disguised as a solo; it
hides which vendor wrote any text, and the A/B/C letters in panel messages are
replaced so they cannot be traced back to a vendor either.
"""
from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
KEYS = {"C": HERE / "ANSWER_KEY.md", "R": HERE / "ANSWER_KEY_R.md"}


def key_rows(set_name: str, seed: str) -> list[str]:
    prefix = seed.split("_")[0] + "."              # c1_kvstore -> "c1."; r1 -> "r1."
    rows = [ln for ln in KEYS[set_name].read_text(encoding="utf-8").splitlines()
            if ln.startswith(f"| {prefix}")]
    return rows


def code_for(set_name: str, seed: str) -> str:
    folder = "seeds" if set_name == "C" else "real"
    return (HERE / folder / f"{seed}.py").read_text(encoding="utf-8")


def main() -> int:
    root, cond = Path(sys.argv[1]), sys.argv[2]
    base = root / "scoring" / cond
    sheets = base / "sheets"
    sheets.mkdir(parents=True, exist_ok=True)
    rng = random.Random(f"20261115-{cond}")
    key: dict[str, dict] = {}
    # Drawn without replacement: two texts on one label would overwrite each other.
    numbers = iter(rng.sample(range(1000, 10000), 9000))
    for f in sorted((root / "out" / cond).glob("*.json")):
        r = json.loads(f.read_text(encoding="utf-8"))
        seed, set_name = r["seed"], r["set"]
        texts = []
        letter_of = {v: L for L, v in r["letters"].items()}
        for v, s in r["solo"].items():
            texts.append(("solo", v, "\n\n".join(s["said"]) or "(nothing posted)"))
        panel = []
        for v, said in r["panel"]["said"].items():
            body = "\n\n".join(said) or "(nothing posted)"
            panel.append(("panel", v, body))
        rng.shuffle(texts)
        rng.shuffle(panel)
        # The panel's letters name vendors through the rotation; relabel them X/Y/Z
        # in the same random order for every panel text of this seed.
        swap = dict(zip("ABC", rng.sample("XYZ", 3)))
        lines = [f"# {seed}", "", "## Code", "", "```python", code_for(set_name, seed).rstrip(),
                 "```", "", "## Answer key rows for this seed", "", *key_rows(set_name, seed), ""]
        lines += ["## Solo reviews", ""]
        for arm, v, body in texts:
            label = f"S{next(numbers)}"
            key[label] = {"seed": seed, "set": set_name, "arm": arm, "vendor": v}
            # The panel refers to reviews by letter; the scorer has to resolve
            # "Y's second bug is real" to the text it points at.
            lines += [f"### {label} (the panel calls this review {swap[letter_of[v]]})", "",
                      body.strip(), ""]
        lines += ["## Panel messages (each read the three solo reviews, there called X, Y and Z)", ""]
        for arm, v, body in panel:
            label = f"P{next(numbers)}"
            key[label] = {"seed": seed, "set": set_name, "arm": arm, "vendor": v}
            body = re.sub(r"\b(?:Review|review|reviewer|Reviewer)\s+([ABC])\b",
                          lambda m: m.group(0)[:-1] + swap[m.group(1)], body)
            body = re.sub(r"(?<![A-Za-z])([ABC])(?=[:)’'s]|\b(?= (?:and|or|both|all|is|are|was|says|said|claims|found|missed|reported)\b))",
                          lambda m: swap[m.group(1)], body)
            lines += [f"### {label}", "", body.strip(), ""]
        (sheets / f"{seed}.md").write_text("\n".join(lines), encoding="utf-8")
    (base / "key.json").write_text(json.dumps(key, indent=1), encoding="utf-8")
    print(f"{len(key)} texts on {len(list(sheets.glob('*.md')))} sheets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
