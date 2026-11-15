# G4 report — a three-seat council against one seat, on five seeded defects

Registration: `docs/science-superpowers/preregistrations/2026-11-15-g4-council-vs-solo.md`,
frozen at `c8ceee0` before any real turn. `prereg.sh audit`: FROZEN, STAMP and
INTEGRITY pass. Its DATA check reports the seed files "missing" because it stored
MSYS absolute paths (`/c/dev/...`); their git blob hashes were compared by hand and
all six match. Transcripts: `experiments/g4/results/`. One run, one machine
(Windows 11, UTC+1), 2026-11-15, `low` effort, 35 wakes, all `ok`.

## Primary result: a tie, 5-5. Null, as predicted.

| seed | council (claude + codex + agy, 2 rounds) | solo (claude, 1 turn) |
|---|---|---|
| s1 `page` | found: "start = number * size should be (number - 1) * size" (g4claude) | found: "`start = number * size` skips the first page" |
| s2 `safe_join` | found: "startswith(\"/srv/www\") is True, so a sibling directory is served" (g4claude) | found: "the prefix check `path.startswith(root)` has no path-separator boundary" |
| s3 `retry` | found: "`return last` returns the exception object instead of raising it" (g4claude) | found: "when every attempt fails it does `return last` instead of `raise last`" |
| s4 `is_valid` | found: "compares local wall time against a naive UTC expiry" (g4codex) | found: "compares a naive UTC time (`utcfromtimestamp`) with naive *local* time" |
| s5 `incr` | found: "`current = self._counts.get(name, 0)` runs outside `self._lock`" (g4agy) | found: "the read `self._counts.get(name, 0)` happens outside the lock" |

Council 5, solo 5. Under the registered rule that is **null**: on these five
defects a council found nothing one claude seat did not. The seeds were at the
ceiling for one seat at `low` effort, which the registration named as the likely
outcome. This says the five were too easy to separate the arms. It does not say
councils never help.

Cost for the same five finds: 30 council turns against 5 solo turns. Wall-clock
per seed: council about 70-110 s (two rounds, seats concurrent), solo 21-46 s.

## Exploratory (not the result)

- **Every seat found every defect in round 1.** No find came from round 2.
- **Round 1 was not fully independent.** Seats start together, but a slower one
  can read what a faster one has already posted. agy opened s3 with "I agree with
  @g4claude", and codex's s4 post arrived first. A design that needs independent
  first opinions has to hold posts back until the round closes.
- **Round 2 corrected false alarms.** It did not add finds:
  - s1: agy's `page_count(-1)` point was challenged by claude ("at most a request
    for extra validation"), and agy withdrew it.
  - s2: claude refuted agy's absolute-path escape: "join gives "/etc/passwd", but
    startswith("/srv/www") is False, so it raises". agy then made a different,
    correct point: `requested="/index.html"` is refused rather than served under
    root.
  - No seat was talked out of a correct find.
- **Extra issues only the council raised, both on s2.** One is codex's TOCTOU
  symlink race between `realpath` and `open`, stated as conditional on an attacker
  who can write the served tree. The other is agy's refusal of absolute URL paths.
  The solo seat raised neither. Neither was seeded, and neither is scored.
- **Tokens.** agy reported 20.7-24.7k input per turn. claude reported 6-12 input
  tokens, because cached input is not counted, and 262-806 output. codex reported
  none.

## Deviations
None to seeds, prompt, harness or rubric. The registered rerun rule was never
triggered, because no wake failed.

## What would separate the arms next time
Harder seeds, so one seat misses some. Independent first rounds. Several solo
seats from different vendors, so "council" is compared with "the best single
vendor" and not only with claude. That would be a new registration, not an edit
of this one.

## Found later (2026-11-15, while preparing G4b)

The seats were registered with no working folder, so claude and agy ran from
where `run.py` was started, the repository root, and loaded its `CLAUDE.md`. That
file describes none of G4's five seeds, so the result is unlikely to move; it is
still not the clean room the design assumed. G4b gives every seat its own empty
folder and checks it before the freeze.
