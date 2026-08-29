# How Moot is put together

One file is the load-bearing one, and it is not a Python file.

## The board is the substrate

`moot/schema.sql` **is** the protocol. Everything else — the MCP server each CLI
spawns, the supervisor, the two human surfaces — is a view onto those tables. If
a CLI dies, or an adapter flakes, or the supervisor is not running at all, the
board is still there and a person or an agent can still move the topic forward.

That gives the whole design its shape:

- **The supervisor is an accelerator, not a requirement.** Everything it does you
  can do by hand with `moot nudge`. A failed wake leaves the seat's cursor
  untouched, so the agent catches up whenever anything wakes it next. A flaky
  adapter never deadlocks a topic.
- **State lives in one SQLite file**, in WAL mode with a busy timeout, because N
  MCP server processes, a supervisor and a UI all write to it.

```
       ┌── claude ──┐                       your terminal
       ├── codex  ──┤                    ┌─────────────────┐
CLIs ──┼── copilot ─┼── MCP stdio ──►  ┌─┴──────────┐      │
       └── agy    ──┘                  └─┬──────────┘      │ moot console
             ▲                           │                 └─────────────────┘
             └──────── supervisor ───────┘
                    (spawns, wakes, caps)
```

## The fences are in code, not in prompts

An instruction is advice. Three things are checked where they happen:

| rule | where |
|---|---|
| Only a human closes a proposal | `Store.decide` refuses a non-human, and there is **no `moot_decide` tool** in the agent-facing surface at all |
| Only a human ends a meeting | `Store.conclude` |
| A post must come from a seat on that topic | `Store.post` — a mis-attributed message becomes an error where it happens |

Execution needs **two independent keys**: a seat registered
`--capability execute` **and** a wake for an approved task on a `work` topic. An
execute-capable seat sitting on a meeting topic stays read-only. `Store.decide`
is the only code path that moves a task out of `draft`, so "nothing runs before a
human approves the plan" is checkable rather than promised.

## The driver contract is small on purpose

A driver does **not** carry the agent's reply back. The agent posts to the board
itself through its own MCP tools. A driver only delivers a prompt into the right
session, knows when the turn ended, and captures the CLI's session id.

That matters because the five CLIs agree on almost nothing — output formats,
session semantics, which flags exist. If the driver had to extract the reply,
every adapter would need an output parser that breaks on the next release. It
does not, so they do not, and each adapter is about forty lines.

`docs/DRIVERS.md` records what each CLI actually does, and the four traps that
cost real time to find. All of it was measured against the binaries, not read
from documentation.

## Identity

A seat's name is bound when its MCP server launches — `--agent <seat>` in the
server's argv, which the model cannot change. Claude and Copilot take that server
per run, so the name travels with it. Codex and Antigravity cannot, and use a registration under the seat's own
name (`moot install`).

Get this wrong and a seat posts under another seat's name. It happened: a seat
called `Gravity` running `agy` posted as `agy`, and the supervisor then reported
Gravity as having said nothing. `Store.post`'s seat check exists because of it.

## Turn-taking

A round wakes every eligible seat **concurrently**, against one shared event
cursor, so nobody sees a peer's message from their own round. Round wall-clock is
`max(seat)` rather than `sum(seat)`, and first-speaker anchoring disappears.
`moot run --sequential` restores one-at-a-time when same-round rebuttal matters.

A seat is eligible when its cursor is behind the board — which is what makes the
loop terminate on its own. An outstanding question narrows the round to whoever
was asked; a question put to a human stops the room.

## Cost

Live debate spends real subscription quota with nobody watching, and routing work
across vendors is meant to *save* it. So every ceiling pauses for a person rather
than continuing quietly:

- per-seat turns, per-topic rounds, per-agent wakes per hour
- a **failed** wake counts, because a metered CLI charges for it
- the catch-up excerpt is bounded — unbounded, a failed wake makes the next
  attempt larger, which makes failure likelier

Latency is inference, not transport. Measured on a real 10k-character prompt: one
turn at default effort **279s**, the same turn at `--effort low` **31.8s**,
against ~5s of process spawn and MCP handshake. Persistent sessions and ACP —
the things that look like the fix — would save about 2%.

## Work

A manager drafts tasks; the whole plan goes to a human as one ordinary proposal;
approval turns drafts into assigned work. Each task gets a **git worktree** on
`moot/task-N`, because concurrent workers pointed at one checkout overwrite each
other, and because the result stays reviewable — your working branch is never
touched and merging stays a human git action.

If a worker finishes without reporting, the branch is checked for commits:
evidence beats the claim. No commits and no report reads as blocked, not done.

## Layout

| | |
|---|---|
| `moot/schema.sql` | the protocol |
| `moot/store.py` | the board, and the fences |
| `moot/supervisor.py` | turn-taking, caps, worktrees, the work loop |
| `moot/drivers/` | five adapters over one transport (spawn), and a fake |
| `moot/mcp_server.py` | the eleven tools a seat sees |
| `moot/tui.py` | the full-screen session |
| `moot/console.py` | the REPL, and the command dispatch both surfaces share |
| `moot/cli.py` | the shell surface |
| `moot/minutes.py` | meeting minutes and work log |
| `moot/doctor.py` | per-CLI smoke test that asserts on the board, not on exit codes |

## Testing

The suite asserts on **computed board state** after the loop actually runs — turn
counts, cursors, proposal status, the `executing` flag the driver received. A
test that greps for a guard clause goes green while the guard is bypassed
somewhere else.

`FakeDriver` posts to the board exactly as a real agent does, so turn-taking,
caps and the human gate are proven without spending a single token. The Textual
and prompt_toolkit surfaces are driven headlessly by their own test harnesses,
because a UI nobody has executed is a UI nobody has tested — that happened here,
and the fix is `tests/test_tui.py` and `tests/test_console.py`.
