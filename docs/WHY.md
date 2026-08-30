# Why it works the way it does

Design decisions, and the measurements behind them. None of this is needed
to use the tool — see [Home](index.md) for that.

## Three invariants

1. **The board is the substrate; the supervisor is an accelerator.** Everything the
   loop does, you can do by hand with `mooting nudge`. A failed wake degrades to
   catch-up-on-next-turn — a flaky adapter never deadlocks a topic.
2. **Caps pause, they never silently continue.** Live debate spends real
   subscription quota with nobody watching. Per-seat turn ceilings and per-hour wake
   ceilings park the topic for a human instead of burning a monthly allowance on
   chatter. A *failed* wake counts too, because metered CLIs charge for it.
3. **Execution needs two independent keys.** A seat edits files only if it was
   registered `--capability execute` **and** it is woken for an approved task on a
   `work` topic. An execute-capable seat sitting on a meeting topic stays read-only.
   Each adapter narrows itself in one place — a `tool_profile()` method, except
   Codex, whose containment is *where* it runs rather than a flag, because it
   offers no way to auto-approve an MCP call while staying read-only.

## Latency: measured, not guessed

A council is only useful if a round finishes while you are still looking at it.
On a real 10k-character council prompt:

| | wall-clock |
|---|---|
| one turn at default effort | **279 s** |
| the same turn at `--effort low` | **31.8 s** |
| process spawn + MCP handshake | ~5 s (≈2% of a default turn) |
| **a real 3-vendor round** (claude + codex + agy, concurrent, `low`) | **29.9 s** |

Against a sequential default-effort baseline of ~837 s for the same three seats,
that is roughly **28x** — all of it from effort and concurrency, none from transport.

Two conclusions, both counter-intuitive:

- **Latency is inference, not transport.** Persistent sessions and ACP look like
  the fix and would save about 2%. Effort is 8.8x.
- **A round should run concurrently.** Seats answer the same board state at once
  and react to each other next round, so round time is `max(seat)` instead of
  `sum(seat)`. This is the default; `mooting run --sequential` restores one-at-a-time
  when same-round rebuttal order matters.

Effort is set per council (`mooting run --effort`), per seat
(`mooting agents add --effort`), or per topic (`mooting topic new --effort`), resolving
topic → seat → council. The default is `low`, because that is what most sessions
are — a question, a few readings, keep moving — and at 31.8s a turn against 279s
it is the difference between a conversation and a wait. The tradeoff is real in
the other direction: the sharpest argument in our first live debate came from a
default-effort turn. Raise it with `/effort high` when the ruling hangs on
catching a flaw.

## Debate or discussion

A topic is framed one of two ways, and it changes what the seats do:

```
mooting topic new api-shape --mode discuss --title ... --brief ... --seats claude,codex
```

- **`debate`** (default) — *disagreement is the product*. Right when a decision
  turns on finding the flaw.
- **`discuss`** — *build on what others said; agreeing is a real contribution*.

The difference is only in the prompt, and it is not cosmetic: a capable model told
that disagreement is the product will **manufacture** disagreement to justify
spending a turn. That is exactly what you want when you are stress-testing a
decision, and exactly wrong when the room is trying to design something.

## Prior art — read this before adding to it

An earlier version of this file claimed that message buses move messages,
orchestrators fan out worktrees, and neither does structured deliberation with a
human arbiter. **That was wrong.** A proper survey found the space is crowded and
several projects are ahead of this one:

- **[LoopTroop](https://github.com/looptroop-ai/LoopTroop)** (MIT, ~1.3k commits) —
  the closest by far. Its *LLM Council* has independent models draft plans, score
  each other on a weighted rubric and **vote on proposals**; the winner synthesises
  the losing drafts; **a human approves before execution**; then "beads" execute in
  isolated git worktrees with Ralph-style retry. That is this project's meeting
  mode, plan gate and work mode, already built, with rubric scoring on top.
- **[Concord MCP](https://github.com/Get-Concord-AI/concord-mcp)** (MIT, TS) —
  architecturally near-identical to Mooting's core: an MCP server over local SQLite
  in `.concord/`, several vendor CLIs attached to one store, durable agent-to-agent
  threads, and a full-screen dashboard. It has **file-claim overlap detection**,
  which Mooting does not. It has no deliberation, votes, manager role or human gate.
- **[OpenCode agent teams](https://dev.to/uenyioha/porting-claude-codes-agent-teams-to-opencode-4hol)** —
  append-only JSONL inbox, **session injection** (messages delivered as synthetic
  user turns) and **auto-wake** that restarts a recipient's prompt loop on delivery.
  That is a better wake design than Mooting's 1s polling.
- **[Omnigent](https://github.com/omnigent-ai/omnigent)** — wraps each agent in
  `bwrap`/seatbelt. An OS-level sandbox is the correct fix for the containment
  problem Mooting currently works around by pointing a seat at an empty directory.
- **[Wit](https://github.com/amaar-mc/wit)** — symbol-level locks via Tree-sitter,
  finer-grained than worktree-per-task.
- **[CLITrigger](https://github.com/HyperAITeam/CLITrigger)**, **[Multica](https://github.com/multica-ai/multica)**,
  **[Paseo](https://github.com/getpaseo/paseo)**, **[ORCH](https://github.com/oxgeneral/ORCH)**,
  **[Agent Teams AI](https://github.com/777genius/agent-teams-ai)** — parallel vendor-CLI
  runners with approval gates, debate modes and Kanban control planes.
  The [awesome-cli-coding-agents](https://github.com/bradAGI/awesome-cli-coding-agents)
  index lists dozens more.

**What is arguably still distinctive here**, stated narrowly: the human-only
decision is a *structural* property rather than a UI gate — there is no
`mooting_decide` tool for an agent to call, and `Store.decide` is the only path out
of `draft`. And seats participate through their own vendor CLI and subscription,
so routing work by cost is possible.

That is a thin margin. Anyone picking this up should trial LoopTroop and Concord
first and only continue here if that margin matters to them.
