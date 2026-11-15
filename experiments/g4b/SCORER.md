# Instructions for the G4b scorer

Given to one fresh Claude subagent per sheet, with the sheet's path. It reads the
sheet and nothing else, and writes `scoring/scores/<seed>.json`.

---

You are scoring code reviews against an answer key. Read exactly one file, the
sheet named below, and open nothing else.

The sheet has the code, the answer-key rows for it, and several texts under labels
like `S1234` (a solo review) and `P5678` (a panel message that read three solo
reviews, called X, Y and Z). You do not know who wrote any text; do not guess.

For **every** label, decide:

1. **found** — the key items this text earns. A text earns an item when it names
   the function (or plainly the same place) **and** gives the mechanism, meaning
   what in the code causes it, **or** a triggering input whose stated wrong outcome
   follows from that mechanism. The right function with a different cause does not
   earn it. "Might be" earns it only when the mechanism is stated. A panel message
   earns an item only if it holds the bug to be real; repeating a review in order to
   reject it does not earn it. Each solo review's heading says which letter the
   panel uses for it. A panel message that affirms a bug **by reference** ("Y's
   first point is real", "agree with X on `get`") earns the item when the point it
   refers to earns that item in the referenced review; resolve the reference
   yourself.
2. **claims** — every other bug the text claims, one entry each, with a verdict:
   - `real`: an input, run against this code, would produce behaviour that
     contradicts the code's own docstrings or comments, or crash on input they
     allow. Items in the key's incidental list are real.
   - `hedged`: the text itself makes it conditional on an assumption the code does
     not support ("if this runs on several threads" where nothing says so).
   - `unverifiable`: it depends on code the excerpt leaves out (`# ...`).
   - `false`: none of the above.
   Style remarks are not claims.

Write only this JSON, no prose, to the output path:

```json
{"S1234": {"found": ["c1.1"], "claims": [{"claim": "compact races put", "verdict": "real"}]},
 "P5678": {"found": [], "claims": []}}
```
