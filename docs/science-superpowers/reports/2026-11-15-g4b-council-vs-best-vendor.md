# G4b report — a council against the best single vendor, on 26 bugs

Registration: `docs/science-superpowers/preregistrations/2026-11-15-g4b-council-vs-best-vendor.md`,
frozen at `b3c9adb` and stamped at `9988fcf`, before any vendor turn was spent. The
file is unchanged since the stamp. `prereg.sh audit` fails on its own path
handling, so the audit was done from git instead: `git hash-object` on all six
frozen files (run.py, blind.py, analyze.py, SCORER.md and both keys) matches the
checksums in the registration. Run record:
`C:/mooting_runs/rounds/2026-11-15_g4b/`. One machine (Windows 11), 2026-11-15,
effort `low`, 192 vendor turns, 32 scorer subagents.

## Primary result: null in both conditions

| | capped (shipped 80-word limit) | free (limit lifted for this run) |
|---|---|---|
| claude alone | 20 | 20 |
| codex alone | 19 | 20 |
| **agy alone (best)** | **21** | **21** |
| all three solos together | 22 | 23 |
| **panel** (2 of 3 messages earn it) | **21** | **23** |
| panel found, agy missed | 1 (r5.2) | 2 (r5.2, r8.1) |
| agy found, panel missed | 1 (r5.1) | 0 |
| sign test, one-sided | p = 0.75 | p = 0.25 |
| verdict at α = .025 | **null** | **null** |
| failed / silent turns (limit 10) | 1 / 2 | 0 / 1 |

These numbers are `analyze.py` output, run unchanged. The panel did not find
significantly more than the best vendor in either condition, which is the outcome
the registration called most likely. A win needed at least 5–0, and that was out
of reach from the start: three set-R items (r1.1, r3.1, r7.1) were missed by every
text in both conditions, and every set-C item was found by every arm.

Free's 23 against 21 is two wins and no losses, so it is null as well; reading it
as a trend would be reading noise.

## Secondary (registered, not confirmatory)

**S1, S2 — the panel step added nothing the solos had not already found.**

| | capped | free |
|---|---|---|
| union vs agy | 1–0, p = .50 | 2–0, p = .25 |
| items the panel lost from the union | r5.1 | none |
| items the panel gained over the union | none | none |

Any gain over the best vendor comes from asking three vendors. The discussion
added no item in either condition, and in capped it dropped one: agy found r5.1,
and only one of three panel messages held it.

What the panel did do was keep items that only some of the solos found. There
were seven such cases across the two conditions, involving r5.1, r5.2, r6.1 and
r8.1. The panel kept six of the seven, and in four of those six all three panel
seats affirmed the item.

**S3 — false alarms.** Counted per text, as registered:

| | capped solo | capped panel | free solo | free panel |
|---|---|---|---|---|
| `false` as scored | 24 (0.50) | 41 (0.85) | 56 (1.17) | 67 (1.40) |
| after the r6 correction | 26 (0.54) | 43 (0.90) | 58 (1.21) | 69 (1.44) |
| `hedged` | 0 | 3 | 5 | 15 |

These figures are not like-for-like. A panel message restates the solos' claims
it agrees with, so one solo false alarm can reappear in up to three panel texts.
The rates are also upper bounds, for a reason under Deviations: scorers did not
see the incidental list.

Hand check of 10 randomly drawn `false` verdicts per condition:

| | agree | partly | disagree |
|---|---|---|---|
| capped | 9 | 1 | 0 |
| free | 7 | 2 | 1 |

The one disagreement is free c4 S2803. It describes c4.1's aliasing mechanism
applied to the caller, and the key's incidental list treats that as real. The two
"partly" verdicts in free were defensible as `false`, but `hedged` (r7) and
`unverifiable` (r3) fit them better. Details are in `scoring/<cond>/s3_handcheck.md`
in the run record. The check was done by the agent running the experiment, in the
same session, not by a person.

**S5 — by set.**

| | claude | codex | agy | union | panel |
|---|---|---|---|---|---|
| C capped | 16 | 16 | 16 | 16 | 16 |
| C free | 14 | 16 | 16 | 16 | 16 |
| R capped | 4 | 3 | 5 | 6 | 5 |
| R free | 6 | 4 | 5 | 7 | 7 |

Set C hit the ceiling, as the trials predicted. Claude's 14 in free is not a
miss: its c2 solo turn posted nothing, so both c2 items were lost, which is the
registered no-retry rule.

**S4 — cost.**

| | capped solo | capped panel | free solo | free panel |
|---|---|---|---|---|
| turns | 48 | 48 | 48 | 48 |
| wake time | 23.9 min | 33.6 min | 31.8 min | 39.0 min |
| tokens out | 21,705 | 28,258 | 52,198 | 80,618 |

Tokens are counted only where a CLI reports them. A panel costs one more turn per
vendor per seed on top of the solos, so the council arm costs twice the turns of
asking three vendors.

**S6 — capped against free.** Lifting the limit added one item for claude in set R
(4 → 6, with r8.1 found only in free), one for codex (19 → 20), and none for agy.
The panel's lead over agy went from 0 to +2, but neither figure is significant.
This comparison is confounded: free ran second, against the same code, and
claude's silent c2 turn falls in free.

## Exploratory (not the result)

- **If claude's lost turns had posted.** Assume, generously, that the silent c2
  solo turn in free would have earned both c2 items. Claude then has 22 and becomes
  the best vendor. Against claude the panel wins r6.1 and loses nothing: 1–0, null.
  In capped, claude's two silent panel turns (c4, c5) did not cost the panel
  anything, because the other two messages earned every item.
- **The real bugs nobody found** are r1.1 (an MCP tool acting on a topic the seat
  has no seat on), r3.1 (photos, videos and voice notes match no handler), and
  r7.1 (`communicate()` waits for the pipes to close, so a grandchild process that
  holds them keeps the turn open until the timeout). Two of them need the reader to
  notice that something is missing, a check or a handler. The third needs knowing
  how the OS handles pipes. None of the three is a visibly wrong line, and no arm
  found any of them in either condition.
- **Reviewers found Mooting's live bug.** In r4, every text in both conditions
  flagged the `nudge` chooser's KeyError, the K6 bug that the trial had already
  turned up. The fix is PR #17.

## Deviations

None of these touches the primary test; they make S3 exploratory.

1. **Scorers did not see the incidental list.** `blind.py` puts only the planted
   rows on the sheet, and the scorer prompt said to judge from the code. Some
   `false` verdicts are therefore items the key calls real; the hand check found
   one. Read S3 as an upper bound in both arms.
2. **An invisible character.** `real/r6.py` keeps the literal ESC byte (0x1B) that
   Mooting's own `telegram.py` has in its `ANSI` pattern, so the excerpt is
   verbatim. Reviewers who read it as missing raised a false alarm, and scorers
   who could not see the byte marked those claims `real` (capped: 2 solo, 2 panel)
   or `unverifiable` (free: 2 solo, 2 panel). The score files are left as the
   scorers wrote them; the corrected row in S3 counts all eight as `false`.
3. **Some panel letters were not swapped.** `blind.py`'s patterns miss lists such
   as "(A, B, X)", "A/B/C" and "B3". A rough grep finds 62 such lines in the panel
   sections of 22 of the 32 sheets, so those references kept a vendor-rotation
   letter. That reveals no vendor: the scorer never
   saw the rotation. It does make a reference harder to resolve, which could only
   cost the panel credit. Checked on r5.1, the one item the panel lost: the
   messages that miss it do not mention the cause at all, so the letters did not
   decide it.
4. **The scorer prompt differs from `SCORER.md` in wording.** It added "do not run
   code, do not search", the sentence about the incidental list, and the output
   path, and it asked for a one-line reply. The rules for earning an item are
   verbatim.
5. **`per_item.py` was written after the run.** It produces the per-item tables
   above with analyze.py's rules and tests nothing.

## What this says about Mooting

At effort `low`, on these 26 bugs, **a council found what its best member found**,
plus at most what the other members found alone. The discussion step neither
lost much nor gained anything, and it cost one extra turn per vendor per seed.
What the council reliably did was keep a find that not every member made: six
times in seven. That is a property worth having when nobody knows in advance which vendor
will be the best one, and that question is the one these numbers can speak to.
"Councils find more bugs than the best single CLI" is still not a claim this
project can make.
