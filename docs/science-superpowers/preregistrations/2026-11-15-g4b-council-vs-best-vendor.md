# Pre-registration: G4b — does a council beat the best single vendor on code review?

**Frozen at commit:** stamped by `prereg.sh freeze`
**Follows:** `2026-11-15-g4-council-vs-solo.md` (null, 5/5 vs 5/5: the seeds were too easy)
**Harness:** `experiments/g4b/run.py` · **blinding:** `experiments/g4b/blind.py` · **analysis:** `experiments/g4b/analyze.py`

## What was done before this freeze (feasibility, not outcome data)

No vendor CLI turn has been spent on G4b. Three small-model trials, each one Haiku
subagent per seed with the solo prompt, no tools beyond reading its one file:

| trial | code | planted items | Haiku found |
|---|---|---|---|
| 1 | eight single-bug seeds, docstring hints removed (`pilot_v1/`) | 8 | 8 |
| 2 | set C below, two bugs a seed | 16 | 16 |
| 3 | set R below, real past bugs | 10 | 6 |
| 4 | the real built prompt at effort `low` (80 words, at most two points) for c5, r6, c2 | 5 | 4 |

Trial 4 found both c5 items and both c2 items within the limit, and in r6 named
two other points and dropped r6.1, which trial 3 had found as its fourth point.
That is why the owner chose to run both conditions below.

Trial 3 is an overestimate: a subagent inherits this repository's `CLAUDE.md`,
which describes three of the R bugs (r5.2, r6.1, r8.1), and two of its reports
cite it. The vendor seats run from an empty sandbox directory and do not see it.
Set C is kept at the owner's request even though trial 2 suggests a ceiling.

Trial 2 raised real issues the key had not listed; some were added to set C's
*incidental* list. No planted item changed after any trial.

## Design

- **Sets.** C: eight written seeds, `experiments/g4b/seeds/c*.py`, sixteen planted
  items shown happening by `check_seeds.py`. R: eight excerpts of Mooting's own
  code as it stood before eight real fixes, `experiments/g4b/real/r*.py`, cut by
  `build_real.py`; ten items, each evidenced by its fix commit. Keys:
  `ANSWER_KEY.md`, `ANSWER_KEY_R.md`. **26 items in all.**
- **Vendors.** claude, codex, agy (Antigravity). Cursor is not included.
- **Solo arm.** Per seed and vendor, a fresh seat (`g4bs<vendor>`) alone on its own
  topic with the solo prompt; one turn. The topic is closed before the next opens,
  so a seat's inbox holds one topic (the harness refuses to wake otherwise).
- **Panel arm.** Per seed, three further seats (`g4bp<vendor>`) on one topic whose
  brief is the code plus the three solo reviews labelled A/B/C, the letter → vendor
  assignment rotated by seed. One **concurrent** round: each panel seat answers the
  same brief without seeing the other panel seats, so speaking order does not exist.
  Each is asked to check every reported bug, add missed ones, and end with `FINAL:`.
- **Two conditions, run separately.** Effort `low` in both (Mooting's shipped
  default, and what G4 used).
  - `capped`: the prompt as shipped, which asks every seat to stay under 80 words
    with at most two supporting points.
  - `free`: the harness removes that length section (and "One point, briefly")
    from every seat's prompt, for this experiment only; the product is unchanged.
  Both arms of a condition get the same prompt rules.
- **Where seats run.** Each seat has its own empty working folder under the runs
  directory (codex uses its own empty sandbox there). Checked before the freeze:
  no `CLAUDE.md`, `AGENTS.md` or memory folder in any of them or above them. Without
  this, claude and agy would have run inside this repository and read a
  `CLAUDE.md` that describes three of set R's bugs.
- **Cost.** 6 turns a seed, 96 per condition, **192 in all**, on the owner's
  subscriptions; plus one Claude scorer subagent per sheet, 32 in all.

## Scoring

- **Blind.** `blind.py` writes one sheet per seed and condition: the code, that seed's key rows,
  the three solo reviews and the three panel messages, each under a random label.
  Vendor is hidden everywhere; the panel's A/B/C letters are swapped for X/Y/Z,
  and each solo's heading says which of X/Y/Z the panel calls it, so a panel
  message that agrees by reference ("Y's first point is real") can be scored.
  Solo and panel texts are in separate sections, because a panel message quotes
  the reviews and cannot pass as a solo.
- **Scorer.** One fresh Claude subagent per sheet, given the sheet and the "How a
  review earns an item" and "Adjudicating" sections of the key. It returns, for
  every label, the key items found and every other claimed bug with a verdict
  (`real`, `false`, `hedged`, `unverifiable`). Its output is saved as
  `scoring/scores/<seed>.json` before `key.json` is opened.
- **Rule for an item.** A text earns an item when it names the function (or plainly
  the same place) and gives the mechanism, or a triggering input whose wrong
  outcome follows from that mechanism. A panel message also earns it by affirming,
  by reference, a point in a review that earns it. The scorer's full instructions
  are `experiments/g4b/SCORER.md`.
- **Panel verdict.** The panel finds an item when **at least 2 of its 3** messages
  earn it. A panel seat that did not post counts as not earning it.
- **Best single vendor.** The vendor with the most items over all 26, chosen after
  the fact. This favours the solo side. When vendors tie, H1 must hold against
  every tied vendor.

## Hypotheses

Tested separately in each condition.

- H0: in Mooting at effort `low`, the panel finds no more of the 26 items than the
  best single vendor.
- H1 (directional): it finds more.

## Primary analysis (exact)

`analyze.py <runs dir> <condition>`, unchanged, once per condition: over the 26 items, count items the panel found and the
best vendor missed (`wins`) and the reverse (`losses`). One-sided exact sign test,
p = P(X ≥ wins), X ~ Bin(wins + losses, ½). Items both found or both missed are
ties and do not enter.

## Prediction

- Direction: panel ≥ best vendor.
- Magnitude: small. Set C is expected at or near the ceiling (all ties). Set R has
  room: if the best vendor finds about 6–8 of 10 and the other two cover 1–3 of the
  rest, the panel can gain at most that. **The most likely outcome is null**,
  because the test needs at least 5 wins against 0 losses (or 7–1, 8–1, 9–2…).

## Decision rule

Per condition, at α = .025 (two primary tests, Bonferroni):

- **Confirm H1** if p < .025 against every tied best vendor.
- **Disconfirm** (panel worse) if the reverse test, P(X ≥ losses), is < .025.
- **Null** otherwise, reported with the counts.
- The two conditions are reported side by side. A confirmation in `capped` only
  is read as "the council wins at the shipped setting, possibly on slot count";
  in `free` only, as "the council wins when every seat may list everything".

## Sample size, stopping, missing data

- Fixed: 16 seeds, 26 items, 96 turns per condition. No seeds or items added after the freeze.
- The two conditions run one after the other on one board (codex and agy bind a seat
  name to one board), with topic names prefixed by the condition.
- No retry. A seat that fails or does not post is recorded as such; its solo review
  reaches the panel as `(no review: this reviewer did not post)` and earns nothing.
- A cap pause (per-hour wakes) stops the run; rerunning continues from the first
  unfinished topic and never re-runs a finished one.
- If more than 10 of a condition's 96 turns fail or post nothing, that condition's
  result is reported but called inconclusive regardless of p.

## Multiplicity

Two confirmatory tests, one per condition, each at α = .025. Everything below is
secondary and is reported without a confirmatory claim.

## Secondary (registered, not confirmatory)

- S1: union of the three solos against the best vendor (same test) — what asking
  three vendors buys before any panel.
- S2: panel against the union — items the panel lost and gained.
- S3: false alarms and hedged claims per text, solo against panel, from the
  scorer's verdicts; 10 randomly chosen `false` verdicts are re-checked by hand
  against the code and the agreement rate is reported.
- S4: cost per arm — turns, wall time, and tokens where a CLI reports them.
- S5: every count above split by set C and set R.
- S6: capped against free — how many items each solo vendor finds under each, and
  whether the panel's advantage (if any) shrinks when the limit is lifted.

## Known weaknesses, stated now

- The excerpts in R were cut around the bug; a whole-file review is harder.
- codex and agy seats can read files on this machine; the fixed code of set R and
  both answer keys are in this repository. The prompt says not to open files;
  nothing enforces it.
- The scorer is a Claude model and one vendor is Claude; labels are blind, the
  model is not neutral.
- In `capped` a seat is asked for 80 words and two points, which trial 4 showed can
  drop a real bug; the panel has three times the points in total. `free` exists to
  separate that from the discussion itself.
- G4 (the predecessor) ran its seats from inside this repository; this was found
  while preparing G4b and is reported there, not corrected after the fact.
- Two items in a seed are not independent of each other (same reader, same turn).

## Planned deviations handling

Any deviation is written in the report and makes the affected analysis exploratory.

## Frozen data checksums

- /c/dev/mooting/experiments/g4b/run.py 889d4f72193cd36ef3147f61ebe40f4cf8da7577
- /c/dev/mooting/experiments/g4b/blind.py 41963db4f557886cd9c5438749c737047cbe99c0
- /c/dev/mooting/experiments/g4b/analyze.py 6e04331b9ab97b42c422a6ab8a7323fd3b199d3a
- /c/dev/mooting/experiments/g4b/SCORER.md c7bf0561f0e154bb2c0aa0ac0929b32c928da91e
- /c/dev/mooting/experiments/g4b/ANSWER_KEY.md bb6e806a4dc37f96512598a2a4e2acb9811bac80
- /c/dev/mooting/experiments/g4b/ANSWER_KEY_R.md a03b3bb08e8a85c166305f7fb5d2213ecc41003d
