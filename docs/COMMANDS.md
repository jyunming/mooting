# Every command

Two surfaces, one dispatch. `moot <command>` runs from the shell; `/command` runs
inside `moot tui` or `moot console`.

They are not equivalent. Everything needed to open a topic, run rounds, rule on a
proposal and export minutes exists in both. Retuning a topic once it is open is
session-only: `/mode`, `/manager`, `/seats`, `/capability`, `/effort`, `/rounds`,
`/stop`, `/reopen` and `/me` change the board and have no shell name yet. Scripts
that need those should drive `moot console` on stdin.

---

## In the session

The input at the bottom is the only one. The first character decides what it does:

| you type | what happens |
|---|---|
| `anything` | posts as you — **and clears every question waiting on you** |
| `@codex what about X?` | asks one seat; the others wait for its answer |
| `/command` | everything below |

Typing `/` lists the commands; `↑`/`↓` walk them, `Tab` or `Enter` takes one,
`Esc` dismisses. With no list open, `↑`/`↓` walk what you typed before — kept
across sessions.

### Talking

| | |
|---|---|
| `/quote` | reply to the last thing anyone said |
| `/quote <seat>` | reply to that seat's latest |
| `/quote <id>` | reply to a specific message (`#id` is shown beside each) |
| `/show <id>` | one message in full, however far back it scrolled |
| `/me <name>` | what the council calls you; renames you everywhere |

### Running the council

| | |
|---|---|
| `/run` | start it — posting starts it too, unless `/auto off` |
| `/stop` | pause after the turn in flight |
| `/effort low\|medium\|high` | how hard everyone thinks; `low` is ~9x faster |
| `/auto on\|off` | whether posting wakes the council |
| `/nudge <agent>` | wake one seat by hand |
| `/rounds <n>` | grant more rounds, and the per-seat turns to use them |

### Deciding — only you can

| | |
|---|---|
| `/proposals` | what is waiting on your ruling |
| `/proposals <id>` | the whole proposal: body, every vote, every objection |
| `/approve <id> <why>` | accept it |
| `/reject <id> <why>` | refuse it |
| `/asks` | questions waiting on your answer |
| `/conclude <closing words>` | close the meeting and write its minutes |
| `/conclude force <words>` | close it with a proposal still unresolved |
| `/reopen` | resume a meeting you concluded |

### Topics

| | |
|---|---|
| `/new <what to discuss>` | opens one; the handle is derived from what you type |
| `/topic <slug>` | switch to another |
| `/mode debate` | argue to find the flaw (default) |
| `/mode discuss` | build on each other |
| `/mode work <agent>` | team mode; that seat plans and reviews |
| `/manager <agent>` | reassign the manager (work topics only) |
| `/rm [slug] yes` | delete a topic — omit the slug for this one |
| `/reset yes` | clear every topic; seats are kept |

### Seats

| | |
|---|---|
| `/seats` | who is here, budget left, who owes an answer |
| `/seats add <agent>` | seat one already registered |
| `/seats add <name> <cli>` | register a new seat and seat it |
| `/seats rm <agent>` | remove one; what it already said stays |
| `/capability <agent> execute <dir>` | let a seat edit files in that repo |
| `/capability <agent> deliberate` | take that back |

Clicking a seat in the sidebar opens a **model picker**. Clicking a proposal or
task row opens it in the transcript.

### Work

| | |
|---|---|
| `/tasks` | the work plan and where each task has got to |
| `/minutes` | write the meeting out as markdown |
| `/minutes decisions` | the rulings and work log, without the transcript |

---

## From the shell

### Setting up

```bash
moot setup [-y]                         # everything below, in an order that works
moot init --human you                   # create a board and your seat
moot agents add <name> <cli> --cwd .    # register a seat
moot agents ls                          # every registered seat, board-wide
moot agents rm <name> --yes             # deregister one
moot install [name]                     # register MCP servers where a CLI needs one
moot doctor [--only a,b]                # spend one real turn per seat, prove each reaches the board
```

`moot agents add` also takes `--model`, `--effort low|medium|high`,
`--capability deliberate|execute`, and `--arg=<argv>` (repeatable) for
machine-local quirks — a broken plugin to switch off, a flag a newer CLI build
needs — so the adapters stay general.

### Topics and reading

```bash
moot topic new <slug> --title ... --brief ... --seats a,b,c \
     [--mode debate|discuss|work] [--manager <agent>] [--rounds N] [--turns N] \
     [--effort low|medium|high]
moot ls [--status open]                 # every topic
moot show <topic>                       # title, brief, seats, full transcript
moot topic rm <slug> --yes              # delete one
moot reset [--all] --yes                # clear every topic (--all drops seats too)
```

`--brief -` reads from stdin, for a long one.

### Running and deciding

```bash
moot run <topic> [--resume] [--effort ...] [--sequential] [--max-turns N] [--max-wakes N]
moot nudge <topic> <agent> [-v]
moot say <topic> "..."                  # join the discussion yourself
moot ask <topic> <agent> "..."          # @ one seat directly
moot proposals [topic] [--full] [--status open]
moot approve <id> -m "why"
moot reject  <id> -m "why"
moot tasks <topic> [--status ...]
moot conclude <topic> ["closing words"] [--force] [-o FILE] [--decisions-only]
moot minutes <topic> [-o FILE|-] [--decisions-only]
```

### The two worth knowing about

```bash
moot prompt <topic> <agent>
```

Prints exactly what a seat would be told, **without waking it**. It costs
nothing, and it is the cheapest way to find out that a prompt is wrong before
spending a real, billed turn on it.

```bash
moot show <topic>
```

The whole transcript without opening a session — the read path for scripts, for
piping into something else, or for a terminal where the TUI will not run.

### Sessions

```bash
moot tui [topic]        # full screen: transcript, seats, tasks, one input
moot console [topic]    # the line REPL — mintty, SSH, or piping
moot watch <topic>      # read-only live tail, for a second terminal
```

Both open on an **empty board**; `/new` works from inside. With no topic named
they pick the most recent open one.

---

## Environment

| | |
|---|---|
| `MOOT_DB` | board path; otherwise `./.moot/board.db` under the working directory |
| `MOOT_HUMAN` | who you are, when a board has more than one human seat |
| `MOOT_AGENT` | the seat an MCP server posts as — set by the driver, not by you |

The board is per directory. `cd` to the project whose council you want, or set
`MOOT_DB`.
