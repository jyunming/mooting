# Pre-registration: G4 — does a three-seat council find more seeded defects than one seat?

**Frozen at commit:** the first commit of this file (see `prereg.sh audit`).
**Roadmap:** `docs/ROADMAP.md` G4. **Owner approval:** 2026-11-15, two rounds, 35 turns.

## Material
Five snippets in `experiments/g4/seeds/`, each with exactly one planted defect.
`experiments/g4/check_seeds.py` shows each defect happening (run 2026-11-15, all five).

| seed | function | the defect | found when a message names the function **and** says |
|---|---|---|---|
| s1_page | `page` | `start = number * size` skips the first page | the start index is off by one / page 1 returns the second page / should be `(number - 1) * size` |
| s2_safe_join | `safe_join` | `startswith(root)` accepts a sibling such as `www-private` | a prefix check lets a path outside root through (sibling with the same prefix, missing separator, should use `commonpath`/`relative_to`) |
| s3_retry | `retry` | returns the last exception instead of raising it | when every attempt fails the error is returned, not raised |
| s4_tokens | `is_valid` | compares local naive `now()` with a naive UTC time | local time is compared with UTC, so validity is wrong by the UTC offset |
| s5_stats | `incr` | reads the count outside the lock | the read happens outside the lock, so concurrent increments are lost |

A mention of the right function with a wrong or vague mechanism ("might have a
threading issue") is **not found**. A message that names the defect and then
says it is fine is not found.

## Arms (identical prompt, `low` effort, fresh seats)
- **council:** seats `g4claude` (claude), `g4codex` (codex), `g4agy` (agy); 2 rounds, one turn per seat per round = 6 turns. Round 2 sees round 1.
- **solo:** seat `g4solo` (claude); 1 round, 1 turn.
- Prompt, verbatim, followed by the snippet: "Review the Python code below for bugs. For each bug, name the function, an input that triggers it, and what goes wrong. If you find none, say so. Leave style alone."
- Harness: `experiments/g4/run.py`. Dry run with FakeDriver passed (35 wakes, 10 topics).

## Hypotheses
- H0: the council finds no more of the five defects than the solo seat.
- H1 (directional): the council finds more.

## Primary outcome and decision rule
- Per seed and arm: found / not found, by the table above, over every seat message in that topic.
- Primary count: seeds found by council (0-5) vs by solo (0-5).
- **Supports H1:** council count > solo count. **Null:** equal. **Against H1:** council < solo.
- n = 5 pairs. Only a 5-0 split of discordant seeds would pass a one-sided sign test at 0.05 (p = 0.031); anything else is reported as one measurement on one machine, not as an effect.

## Prediction (written before any real turn)
Council ≥ solo on every seed. The single most likely outcome is a **tie**, because
claude alone probably finds most of these at low effort. A tie is a null result and
will be reported as one.

## Sample size and stopping
Fixed: 5 seeds × 2 arms, one run each. A topic with a failed wake (error or timeout)
is rerun once as a fresh topic; if it fails again its seed pair is excluded from the
primary count and reported. No other reruns, no added seeds.

## Scoring procedure
Scored by the agent that wrote this file, from the exported transcripts, against the
table above. Arms cannot be hidden (a council has six messages). Every "found" is
reported with the sentence that earned it, quoted, so the owner can re-score.

## Exploratory (reported separately, never as the result)
- Which seat found each defect in round 1, alone.
- Whether round 2 added a find, corrected a wrong claim, or talked a seat out of a correct one.
- Claims of other bugs (false alarms or real extra issues), listed verbatim.
- Tokens per arm where the CLI reports them.

## Deviations
Any change to the seeds, prompt, harness or rubric after this file's first commit is
a deviation, listed in the report, and makes the affected result exploratory.

## Frozen data checksums

- /c/dev/mooting/experiments/g4/seeds/s1_page.py a02e1ce16d0136416542eac45ec5f657d65f5f77
- /c/dev/mooting/experiments/g4/seeds/s2_safe_join.py de4bbe1c7c0f285ee029011d187a817de68e3dc6
- /c/dev/mooting/experiments/g4/seeds/s3_retry.py 761d0a5531688cade5917a11e163332d8983436a
- /c/dev/mooting/experiments/g4/seeds/s4_tokens.py f3bcff86b2c67aa3f3ae401d3d8e4ac8a5110a79
- /c/dev/mooting/experiments/g4/seeds/s5_stats.py fed6241a8f2324a6d2ee002bdc1fa1cb5f942ab0
- /c/dev/mooting/experiments/g4/run.py c85659333f66f806fe71f2b0c7f9bb7fc8aca76f
