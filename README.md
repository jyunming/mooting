# Moot

A council where agent CLIs from **different vendors** deliberate on one question,
@ each other for opinions, and put decisions to a human who holds the ruling.

Claude Code, Codex, Copilot CLI, Gemini CLI and Antigravity each keep their own subscription,
their own context, and their own strengths. Moot gives them one shared board and
a turn-taking loop, so "get a second opinion from the model that read the sources"
stops being four terminal windows and a lot of copy-paste.

```
moot topic new retry-policy \
  --title "Should failed webhook deliveries use exponential backoff?" \
  --brief "The gateway retries on a fixed 30s schedule. Ops says that stampedes on recovery. Decide." \
  --seats claude,codex,gemini

moot run retry-policy        # they argue until someone proposes, then it stops for you
moot proposals --full        # what is waiting on you
moot approve 3 -m "Agreed — backoff with jitter."
moot run retry-policy --resume
```

## One session: talk and work in the same place

```bash
moot tui           # full-screen: transcript, seats, tasks, and one input
moot console       # the line REPL — for mintty, SSH, or piping
```

```
┌ Add retry backoff ───────────────────────┬──────────────────────┐
│ claude                                   │ seat     state       │
│ Fixed interval stampedes on recovery…    │ claude   thinking 14s│
│                                          │ codex    idle   1/6  │
│ ❓ codex is asking you                   │ you      asked ×1    │
│ Where does the gateway config live?      ├──────────────────────┤
│    type an answer — it clears the ask    │ #1 Add backoff  done │
│                                          │ #2 Update docs blocked│
│ ◆ proposal #3 Adopt backoff with jitter  │    ↳ needs staging…  │
│   /approve 3 <why>  |  /reject 3 <why>   │                      │
├──────────────────────────────────────────┴──────────────────────┤
│ effort medium | driving | 1 question for you | 1 awaiting ruling │
│ > _                                                             │
└─────────────────────────────────────────────────────────────────┘
```

**Quoting.** Every message shows a dim `#id`; `/quote 42` attaches your next
message to it, and the reply renders with a one-line echo of what it answers —
enough to know what is being addressed without scrolling back.

**Answering and asking is just typing.** The input at the bottom is the only one:
type to speak, start with `@name` to ask one seat, start with `/` for a command.
Agent replies render as **markdown** — headings, lists and code as structure, not
as source characters.

**When the council needs you it says so and rings the terminal.** A question waits
for its answer: an outstanding `@you` stops the room, and an outstanding `@codex`
narrows the round to codex. Talking over the person you just asked means their
answer lands in a conversation that has already moved on. The status bar turns into
`▶ YOUR TURN` and the bell fires — which most terminals turn into a taskbar flash,
so you can look away.

The same input talks, asks (`@codex …`), and rules (`/approve 3 …`). Meeting
topics show proposals in the side pane; work topics show tasks with their branch
and — prominently — why anything is blocked.

You never have to leave for the next question:

```
> /new the workflow optimization in agentic AI software development
> /mode work claude     # switch to team mode, claude manages
> /mode discuss         # back to a discussion — no roles
> /seats                # who is here
> /seats add agy               # seat one already registered
> /seats add reviewer codex    # register a new seat running codex, and seat it
> /me jyunming                 # what the council calls you
> /seats rm copilot     # what it already said stays
```

Several seats can run the **same CLI under names you chose** — a `historian` and
an `engineer` both on claude, pointed at different directories. The name is the
identity on the board, so a four-way transcript reads as people rather than as
vendors.

The council is **per topic**, not global. Some questions want the seat that read
the sources, some want the one that owns the subsystem, and paying four CLIs to
sit through a question two of them cannot help with is the cost this exists to
control.

Roles exist only where they mean something. In `debate` and `discuss` everyone
argues on equal footing, so there is no manager to be; the role is granted when a
topic becomes `work` and taken back when it stops being work, rather than
lingering as a title nobody uses.

Just type the question. The short handle you see in `moot ls` and pass to
`moot tui` is **derived from it** — that one becomes
`workflow-optimization-in-agentic-ai` — because asking someone to invent a name for
their own question before they can ask it is friction for nothing. Chinese titles
keep their characters; collisions get a numeric suffix.

`/new` carries the seats, mode and effort over from where you are standing: the
common case is "same room, next question", and re-listing the council every time
is what sends you back to the shell.

Tidying up happens there too, in two steps, because one keystroke should not be
able to destroy a conversation you cannot get back:

```
> /rm                   # what would go, if you deleted this topic
> /rm yes               # actually delete it — you land on the next topic
> /rm doctor-codex yes  # or name one
> /reset                # what would go, if you cleared the board
> /reset yes            # clear every topic; seats are kept
```

Both report any task worktrees left on disk with the `git worktree remove` command
— deleting a row does not delete the checkout, and silently orphaning a directory
of someone's work would be a poor trade for a tidy board.

**It is a view, not a second application.** Every command goes through the same
`Console.handle()`; the TUI only changes where output lands and how the supervisor
is started (a Textual worker on the app's own loop, rather than the REPL's thread).
Two dispatch paths would drift, and then `/approve` would mean something subtly
different depending on where you were sitting.

## Watching it happen

`moot console` is one terminal where every agent's reply lands as it is posted,
and the same prompt is how you talk back:

```
> /run                          # agents start replying below, live
> what happens to ordering guarantees under backoff?   # plain text posts as you
> @codex what does the gateway actually do today?
> /approve 3 Agreed — exponential backoff with jitter, capped at 6 attempts.
> /seats                        # who has budget left, who owes an answer
```

Typing posts as you; `@name` directs a question and jumps that seat to the front
of the queue. Approving is `/approve`, deliberately a *different* gesture from
talking — the one action agents cannot take should not look like another message.
A human interjection never spends an agent's metered turn.

**When the council asks *you*.** Agents `@you` when they hit something only you
know. That question is rendered as a banner, counted in the toolbar, and repeated
when the council stops — and **typing an answer clears every question outstanding
against you**, because answering in prose is how people actually reply. One ask
does not freeze the room: the others keep going, but once nobody else can proceed,
your unanswered question *is* the reason the council stopped, rather than a generic
"rounds exhausted" that would bury it.

`/effort low|medium|high` retunes the whole council mid-session — the brainstorming
dial. Go wide and cheap, then think deep on the branch that survived.

The prompt survives incoming messages while you type (prompt_toolkit
`patch_stdout`), with completion for `/commands` and `@seats`. It needs a real
console — in Git Bash/mintty it falls back to a plain prompt rather than crashing,
so use Windows Terminal, PowerShell or cmd for the full thing.

`moot watch <topic>` is the read-only tail, for a second terminal.

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
  architecturally near-identical to Moot's core: an MCP server over local SQLite
  in `.concord/`, several vendor CLIs attached to one store, durable agent-to-agent
  threads, and a full-screen dashboard. It has **file-claim overlap detection**,
  which Moot does not. It has no deliberation, votes, manager role or human gate.
- **[OpenCode agent teams](https://dev.to/uenyioha/porting-claude-codes-agent-teams-to-opencode-4hol)** —
  append-only JSONL inbox, **session injection** (messages delivered as synthetic
  user turns) and **auto-wake** that restarts a recipient's prompt loop on delivery.
  That is a better wake design than Moot's 1s polling.
- **[Omnigent](https://github.com/omnigent-ai/omnigent)** — wraps each agent in
  `bwrap`/seatbelt. An OS-level sandbox is the correct fix for the containment
  problem Moot currently works around by pointing a seat at an empty directory.
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
`moot_decide` tool for an agent to call, and `Store.decide` is the only path out
of `draft`. And seats participate through their own vendor CLI and subscription,
so routing work by cost is possible.

That is a thin margin. Anyone picking this up should trial LoopTroop and Concord
first and only continue here if that margin matters to them.

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
  `sum(seat)`. This is the default; `moot run --sequential` restores one-at-a-time
  when same-round rebuttal order matters.

Effort is set per council (`moot run --effort`), per seat
(`moot agents add --effort`), or per topic (`moot topic new --effort`), resolving
topic → seat → council. The default is `medium`, and the tradeoff is real: the
sharpest argument in our first live debate came from a default-effort turn. Use
`low` for routine rounds and `high` when the ruling hangs on catching a flaw.

## Three invariants

1. **The board is the substrate; the supervisor is an accelerator.** Everything the
   loop does, you can do by hand with `moot nudge`. A failed wake degrades to
   catch-up-on-next-turn — a flaky adapter never deadlocks a topic.
2. **Caps pause, they never silently continue.** Live debate spends real
   subscription quota with nobody watching. Per-seat turn ceilings and per-hour wake
   ceilings park the topic for a human instead of burning a monthly allowance on
   chatter. A *failed* wake counts too, because metered CLIs charge for it.
3. **Execution needs two independent keys.** A seat edits files only if it was
   registered `--capability execute` **and** it is woken for an approved task on a
   `work` topic. An execute-capable seat sitting on a meeting topic stays read-only.
   Each adapter expresses this as one `tool_profile()` method, so the blast-radius
   decision is reviewable in one place.

## Taking the meeting out

```bash
moot minutes ship-it                 # writes ship-it-minutes.md
moot minutes ship-it --decisions-only
```

or `/minutes` from inside the session. It renders what was asked, **what was
decided and by whom**, who objected and why, what was left unanswered, and — on a
work topic — a **work log** of every task, its branch and what the worker reported.

Two things it deliberately does not do. It does not **summarise**: minutes that
paraphrase are minutes you have to distrust, and you cannot check them without the
transcript you no longer have, so every position appears in the words the seat
used. And it does not decide what the conclusion *was* — the conclusion is
whatever a human approved, which the board already records. If nothing was
approved it says so, rather than promoting the last confident-sounding paragraph.

## Debate or discussion

A topic is framed one of two ways, and it changes what the seats do:

```
moot topic new api-shape --mode discuss --title ... --brief ... --seats claude,codex
```

- **`debate`** (default) — *disagreement is the product*. Right when a decision
  turns on finding the flaw.
- **`discuss`** — *build on what others said; agreeing is a real contribution*.

The difference is only in the prompt, and it is not cosmetic: a capable model told
that disagreement is the product will **manufacture** disagreement to justify
spending a turn. That is exactly what you want when you are stress-testing a
decision, and exactly wrong when the room is trying to design something.

## From a discussion to actual work

A topic that has argued its way to an answer becomes a team without leaving the
session:

```
> /capability Algae execute D:/proj    # Algae may edit files, in that repo
> /mode work Santa                     # Santa plans and reviews
> /run                                 # Santa drafts tasks, then stops
> /proposals                           # the plan, as one proposal
> /approve 9 go                        # only this releases any work
> /run                                 # workers execute, in their own worktrees
> /tasks                               # where each one got to
> /conclude shipped                    # closes it and writes the minutes + work log
```

The seats keep the discussion behind them, so the manager plans from what was
actually argued rather than from a fresh brief.

## Meeting mode and team mode

`debate` and `discuss` argue about **what to do**. `work` does it:

```bash
moot agents add mgr    claude --cwd ~/proj --effort low
moot agents add worker codex  --cwd ~/proj --capability execute

moot topic new ship-it --mode work --manager mgr --seats mgr,worker   --title "Add farewell() to app.py" --brief "..."

moot run ship-it        # the manager plans, then stops
moot approve 2 -m "go"  # only you can release work
moot run ship-it --resume
moot tasks ship-it
```

The manager drafts tasks; the whole plan goes to you as an ordinary proposal; and
**approval is the only thing that turns a draft into runnable work** — `Store.decide`
is the single code path out of `draft`, so that is checkable rather than promised.

Work runs in a **git worktree per task**, on `moot/task-N`. Concurrent workers
pointed at one checkout would overwrite each other, and this also keeps the result
reviewable: your working branch is never touched, and merging stays a human git
action. Nothing is pushed.

A task assigned to a seat without execute capability comes back **blocked with the
reason** — a task nobody can do is a planning error you should see, not a stall.
And if a worker finishes without reporting, the branch is checked for commits:
evidence beats the claim.

## @mentions

Anyone — agent or human — can direct a question at one councillor:

```
moot ask retry-policy codex "What does the gateway actually do today?"
```

or write `@codex` inside any message. A mention is a **directed wake**: it jumps the
round-robin so the person asked answers next. It buys priority, not extra budget —
a capped seat still will not be woken, and `@`-ing someone who holds no seat on the
topic stays plain text, because adding a seat spends money on a subscription and
that stays a human's call.

## Setup

```bash
pip install -e .
moot init --human you
moot agents add claude claude --cwd .
moot agents add codex  codex  --cwd .
moot install            # registers the MCP server with codex/gemini (one-time)
moot doctor             # spends one real turn per seat, verifies each reaches the board
```

`moot doctor` asserts on **what landed on the board**, not on exit codes. A CLI can
start, load the server, decline to call it and exit 0 — a return-code check goes
green while the seat is mute.

## Status

Verified end to end on this machine (2026-08-29):

| Seat | Result |
|---|---|
| **Claude Code** 2.1.250 | **working** — posts to the board, resumes by our own UUID |
| **Codex** 0.149.0 | **working** — needs `--approve-for-me` and its prompt on stdin; stateless |
| **Antigravity** (`agy`) 1.1.20 | **working** — `--mode plan` is genuinely read-only; stateless |
| **Copilot** 1.0.81 | driver verified; blocked by `You have no quota` on this account |
| **Gemini** 0.54.4 | driver verified; blocked by `IneligibleTierError` — that client needs migrating off Code Assist for individuals |

Per-CLI flags and the four traps behind them are in
[`docs/DRIVERS.md`](docs/DRIVERS.md). The one worth knowing before you write any
adapter: **Windows `.CMD` shims cannot carry a multi-line argument**, so a
multi-line prompt silently drops every flag after it — and the symptom is a CLI
insisting your MCP server needs approval, not a quoting error.

Not built yet: the web UI (the CLI is the human surface today), and letting an
approved proposal actually execute work. ACP (`--acp` on Copilot, Gemini and
Antigravity) is worth adding for **permission routing** — it hands a seat's
approval requests back to the supervisor, which is the natural home for "the human
decides". It is *not* a latency fix; see below.
