# G4b answer key — set C (written seeds)

Written before any vendor turn. `check_seeds.py` shows every planted item happen.
The second small-model trial (Haiku, 2026-11-15) saw this code before the key was
finished, and some *incidental* items below were first raised by it; none of the
*planted* items changed after it.

## How a review earns an item

A review **finds** a planted item when it names the function (or plainly the same
place) **and** gives the mechanism — what in the code causes it — or a triggering
input whose stated wrong outcome follows from that mechanism. Naming the right
function with a different cause does not count. "Might be" counts only when the
mechanism is stated; a bare "check thread safety" does not.

## Planted (the 16 scored items)

| id | where | mechanism | shown by |
|---|---|---|---|
| c1.1 | `Store._shard_for` | built-in `hash()` of a str is salted per process, so the worker and the batch job (or a restart) pick different shards | 20/20 keys lost across two processes |
| c1.2 | `Store.get` | returns the **first** record of a key in an append-only log, so the oldest value (or an old tombstone) wins | put old, put new → old |
| c2.1 | `escape_html` | `&` is replaced **last**, so the entities just written are escaped again | `<` → `&amp;lt;` |
| c2.2 | `render` | the customer's value is a `re.sub` replacement string, so backslashes are interpreted (`\d` raises, `\f` becomes a form feed) | `Bob\d` → re.error |
| c3.1 | `page` | `created_at > cursor` with only the time in the cursor and whole-second times, so rows sharing the boundary second are skipped | 4 events → 3 exported |
| c3.2 | `encode_cursor` / `decode_cursor` | padding is stripped on encode and never restored, so a real cursor fails to decode | every 10-digit cursor → "bad cursor" |
| c4.1 | `merge` + `settings_for` | the merge is shallow below the first changed level, so `+=` on `bcc` extends the cached defaults' list: it grows every request and reaches every tenant | bcc grows by one per call |
| c4.2 | `upload_limit_bytes` | `^` is xor, not a power (`* 1024 ^ 2` → `(mb*1024) xor 2`) | 10 MB → 10242 bytes |
| c5.1 | `Scheduler._push` / heap | `(run_at, job)` tuples compare `Job` on a tie, and `Job` has no ordering | two jobs at t=100 → TypeError |
| c5.2 | `tick` + `cancel` | a repeating job is re-pushed as a **new** `Job` with a new id, so `cancel(original id)` flags a dead object, returns True, and the job keeps running | ran 3 times after cancel |
| c6.1 | `Limiter.allow` | refill truncated with `int()` while `last` advances every call, so a client polling faster than 1/rate never refills | 0 of ~25 allowed |
| c6.2 | `client_id` | takes the **leftmost** X-Forwarded-For entry, which the client controls; the proxy appends, so the rightmost is the trustworthy one | 50/50 allowed with forged XFF |
| c7.1 | `fold` | `lower()` instead of `casefold()` despite "as Unicode defines case" (ß/SS, final sigma) | Straße and STRASSE both accepted |
| c7.2 | `search` | `%` and `_` in the typed text are LIKE wildcards, not escaped | `search('%')` → everyone |
| c8.1 | `row` / `export` | employee-typed text starting `= + - @` reaches Excel as a formula (CSV injection) | `=HYPERLINK(...)` written raw |
| c8.2 | `export` | `open()` without `newline=""` on Windows writes `\r\r\n` (a blank row after each in Excel) | bytes show `\r\r\n` |

## Incidental — real, not scored for finding, counted as *not* false alarms

- c1: `compact` races an append from the other process (lost write); a torn last line makes `json.loads` fail for the whole shard; `delete` writes to the wrong shard for the same reason as c1.1; `put → delete → put → get` returns the default (same root as c1.2).
- c2: a value containing `{other_field}` is filled by a later pass (fields leak into other places); `preview` shows escaped entities as text; `lookup` renders `None` as "None".
- c3: a late-committed row with an earlier time is never served; a crafted cursor raises OverflowError, not "bad cursor"; ties have no stable order.
- c4: `extra_bcc` with no `bcc` → KeyError; the cached tenant file is mutated the same way as c4.1; a float `upload_mb` makes `^` raise TypeError.
- c5: `_by_id` keeps every old id of a repeating job (leak, same root as c5.2); a job that cancels itself while running is re-pushed; catch-up burst after a long gap.
- c6: `_buckets` grows without bound (worse with c6.2); `retry_after` ignores refill since `last`; empty XFF entry gives every such client one shared bucket; header lookup is case-sensitive.
- c7: `rename` skips the 2–80 length check and ignores a missing id; any IntegrityError (e.g. NULL team) is reported as NameTaken; dotted capital İ folds to `i` + U+0307.
- c8: rows are rounded per row but totals are not (total can disagree with the rows shown); Excel converts typed text such as `3-4` into dates.

## Adjudicating a claim not on this list

A claimed bug not listed here is **real** if a concrete input, run against the
seed, produces behaviour that contradicts the module's or function's own text, or
crashes on input the text allows. It is a **false alarm** otherwise. It is
**hedged** when the review itself makes it conditional on an assumption the text
does not support (e.g. "if this is called from several threads" where nothing
says so). Each adjudication is recorded with the input that was run.
