# G4b answer key — set R (real bugs from Mooting's history)

Each excerpt in `real/` is the code as it stood at `<fix>^`, cut by
`build_real.py`. The fix commit is the evidence that the bug was real: each was
found in use (a person on a phone, a live seat, a failing review) and its
message says what was seen. Written before any vendor turn; the same rule as set C
decides whether a review earns an item (function + mechanism, or a triggering
input whose wrong outcome follows from the mechanism).

| id | fix | where | mechanism |
|---|---|---|---|
| r1.1 | a35d5af | `_topic_id` and the tools that use it (`mooting_read`, `mooting_propose`, `mooting_vote`, `mooting_tasks`, ...) | nothing checks that the calling agent holds a seat on the topic it names, so an agent seated on one meeting can read, propose on and vote in any other on the board |
| r2.1 | f274e4a | `_quote` in `serve_web` | an argument is quoted only when it holds a space, and the command is run through a shell, so a board path or topic containing `&`, `;`, `|` (or a `"`) is read as shell syntax |
| r3.1 | c0fc652 | the `m.document is not None` handler | a phone's share sheet sends a picture as `photo`, a clip as `video`, a recording as `voice`; none is a `document`, so the message matches no handler and nothing is said |
| r4.1 | 153ef8c | `send_choices` rounds buttons + the callback that builds `/rounds {value}` | the buttons read "+1/+3/+5" under "How many more rounds?" but send `/rounds n`, which `_rounds` treats as *set the total*; on a topic already at n it changes nothing and reports "round x of n" as if it worked |
| r5.1 | 3dd9041 | `_nudge` | the name is used as typed; from a chat it arrives as `@Santa` (the other commands strip the `@`, this one does not), so the wake fails for a seat that exists |
| r5.2 | 3dd9041 | `_nudge`'s `work()` thread | an exception inside the daemon thread is caught by nothing, so the room is told "waking …" and then nothing ever again |
| r6.1 | 69dd76f | the stop reason (`… is waiting on you: {question[:200]}`) wrapped as `_council stopped: {reason}_` | the reason carries raw newlines from somebody's turn; `_prose` renders paragraph by paragraph and the italic pattern does not cross a newline, so the underscores arrive as literal text (and the cut lands mid-word) |
| r7.1 | 61c09cf | `Driver._run` | `communicate()` waits for EOF on stdout and stderr, not for the CLI to exit; a grandchild that inherited the pipes (a self-updater) keeps the turn open after the CLI has exited, until the timeout |
| r7.2 | 2c5d44f | `Driver._run` timeout path | on Windows the binary is usually a `.CMD` shim run by `cmd.exe`; `proc.kill()` ends the shim and leaves the real CLI running and spending quota, which the comment beside it says is what the kill exists to prevent |
| r8.1 | 4f702e9 | module set-up in `cli.py` + every `sys.stdin.read()` | stdout and stderr are reconfigured to UTF-8 and stdin is not, so piped UTF-8 is decoded with the locale codec (cp1252 on Windows): mojibake stored silently, or lone surrogates that fail at the SQLite write |

## Incidental (real, not scored; not false alarms)

- r3: the file name comes from the sender and is joined onto a temp path unchecked.
- r6: a question containing `_` or `*` breaks the same italics within one line.
- r7: `await proc.wait()` after `kill()` has no bound, so the same pipe holder can hang the timeout path too.
- r8: `cmd_attach` and others read files with the platform default encoding where they do not name one.

Anything else claimed is adjudicated as in set C: real if it can be shown
against the excerpt's own code and text, hedged if conditional on an assumption
the excerpt does not support, otherwise a false alarm. A claim that depends on
code the excerpt leaves out (`# ...`) is **unverifiable** and counted separately.
