# Using it

A tour of the session. Every command is listed in
[COMMANDS.md](COMMANDS.md); this is the shape of the work.

## Two ways to run it

```bash
mooting tui           # full-screen: transcript, seats, tasks, one input
mooting console       # the line REPL — for mintty, SSH, or piping
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

Both are the same session. Every command goes through the same
`Console.handle()`; the TUI only changes where output lands and how the
supervisor is started. There is one dispatch path, so `/approve` means the
same thing in either view.

## The input

There is one input line. Type to speak. Start with `@name` to ask one seat
a question. Start with `/` for a command. Agent replies render as markdown
(headings, lists, code), not as raw source characters.

`#id` marks every message. `/quote 42` attaches your next message to it, and
the reply shows a one-line echo of what it answers.

**When the council needs you, it says so and rings the terminal.** The
`▶ YOUR TURN` bar (`mooting tui`) or the ring-and-count (`mooting console`)
means a question is outstanding. An open `@you` question stops the room;
an open `@codex` question narrows the round to codex. Typing an answer
clears every question outstanding against you — that is how an answer is
recognized, so replying in prose is enough.

`/effort low|medium|high` retunes the whole council mid-session. Default
is `low`.

`/attach <file>` feeds a document to the council — its text is inlined
into every seat's prompt.

## Topics

Meeting topics show proposals in the side pane; work topics show tasks
with their branch and why anything is blocked.

```
> /topic new the workflow optimization in agentic AI software development
> /topic switch retry-policy    # move to another topic
> /topic rename retry-policy Retry policy for the gateway
> /topic agenda fix retry storms; decide backoff cap
> /topic mode work claude       # switch to team mode, claude manages
> /topic mode discuss           # back to a discussion — no roles
```

A topic's short handle — the one you see in `mooting ls` and pass to
`mooting tui` — is derived from its title, so "the workflow optimization in
agentic AI software development" becomes `workflow-optimization-in-agentic-ai`.
Titles keep their characters; collisions get a numeric suffix.

`/topic new` carries the current seats, mode, and effort over, so opening
the next question does not mean re-listing the council.

Roles exist only where they mean something: `debate` and `discuss` have
everyone arguing on equal footing, with no manager; the role appears when a
topic switches to `work` and disappears when it stops being work.

Deleting is two steps, so one keystroke cannot destroy a conversation you
cannot get back:

```
> /topic rm              # preview: what would go, if you deleted this topic
> /topic rm yes          # delete it — you land on the next topic
> /topic rm doctor-codex yes   # or name one, and confirm in the same line
> /reset                 # preview: what would go, if you cleared the board
> /reset yes             # clear every topic; seats are kept
```

Both report any task worktrees left on disk, with the `git worktree remove`
command to clear them — deleting a topic does not delete its checkouts.

## Seats

```
> /seats                       # who is here
> /seats add agy                # seat one already registered
> /seats add reviewer codex     # register a new seat running codex, and seat it
> /me jyunming                  # what the council calls you
> /seats rm copilot              # remove one; what it already said stays
```

Several seats can run the same CLI under names you chose — a `historian`
and an `engineer` both on claude, pointed at different directories. The
name is the identity on the board, so a transcript reads as people, not
vendors.

The council is per topic, not global: only the seats a question actually
needs sit in on it.

## @mentions

```
mooting ask retry-policy codex "What does the gateway actually do today?"
```

or write `@codex` inside any message. A mention is a directed wake: it
jumps the round-robin so the person asked answers next. It buys priority,
not budget — a capped seat still will not be woken, and `@`-ing someone who
holds no seat on the topic is just plain text; adding a seat is a separate,
explicit call.

## Console specifics

```
> /run                          # agents start replying below, live
> what happens to ordering guarantees under backoff?   # plain text posts as you
> @codex what does the gateway actually do today?
> /approve 3 Agreed — exponential backoff with jitter, capped at 6 attempts.
> /seats                        # who has budget left, who owes an answer
```

Approving is `/approve`, a different gesture from talking — the one action
agents cannot take should not look like another message. A human message
never spends an agent's metered turn.

The prompt keeps working while replies arrive (prompt_toolkit
`patch_stdout`), with completion for `/commands` and `@seats`. It needs a
real console — in Git Bash/mintty it falls back to a plain prompt instead
of crashing; use Windows Terminal, PowerShell, or cmd for the full thing.

`mooting watch <topic>` opens a read-only tail, for a second terminal.

## Minutes

```bash
mooting minutes ship-it                 # writes ship-it-minutes.md
mooting minutes ship-it --decisions-only
```

or `/minutes` from inside the session. It reports what was asked, what was
decided and by whom, who objected and why, and what was left unanswered —
plus, on a work topic, a work log of every task: its branch, what the
worker reported, and how many commits actually landed. The report is a
claim; the commit count is the evidence, and both are shown.

It does not summarize — every position appears in the words the seat used,
not paraphrased. It does not decide what the conclusion was: that is
whatever a human approved, or "nothing was approved" if that's the case.

## From discussion to work

A topic that has argued its way to an answer becomes a team without
leaving the session:

```
> /capability Algae execute D:/proj    # Algae may edit files, in that repo
> /topic mode work Santa               # Santa plans and reviews
> /run                                  # Santa drafts tasks, then stops
> /proposals                           # the plan, as one proposal
> /approve 9 go                        # only this releases any work
> /run                                  # workers execute, in their own worktrees
> /tasks                               # where each one got to
> /conclude shipped                    # closes it and writes the minutes + work log
```

The seats keep the discussion behind them, so the manager plans from what
was actually argued, not a fresh brief.

The same flow works from the CLI, outside any live session:

```bash
mooting agents add mgr    claude --cwd ~/proj --effort low
mooting agents add worker codex  --cwd ~/proj --capability execute

mooting topic new ship-it --mode work --manager mgr --seats mgr,worker \
  --title "Add farewell() to app.py" --brief "..."

mooting run ship-it        # the manager plans, then stops
mooting approve 2 -m "go"  # only you can release work
mooting run ship-it --resume
mooting tasks ship-it
```

The manager drafts tasks; the whole plan goes to you as an ordinary
proposal; approval is the only thing that turns a draft into runnable
work (`Store.decide` is the single code path out of `draft`).

Work runs in a git worktree per task, on `mooting/task-N` — concurrent
workers on one checkout would overwrite each other, and this keeps your
working branch untouched; merging stays a manual git action. Nothing is
pushed.

A task assigned to a seat without execute capability comes back blocked,
with the reason. If a worker finishes without reporting, its branch is
checked for commits: evidence over claim.
