# Using it

A tour of the session. Every command is listed in
[COMMANDS.md](COMMANDS.md); this is the shape of the work.

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
│ effort low    | driving | 1 question for you | 1 awaiting ruling │
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

**When the council needs you it says so and rings the terminal** (the `▶ YOUR TURN`
bar is `moot tui`; the REPL rings and counts the questions). A question waits
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
