# Every command

Two surfaces, one dispatch. `mooting <command>` runs from the shell; `/command` runs
inside `mooting tui` or `mooting console`.

They are not equivalent. Opening a topic, setting its agenda, running rounds,
ruling on a proposal and exporting minutes all exist in both — enough to drive a
council over SSH. Retuning a topic once it is open is session-only: `/topic mode`,
`/topic manager`, `/seats`, `/capability`, `/effort`, `/rounds`, `/stop`,
`/reopen` and `/me` change the board and have no shell name yet. Scripts that
need those should drive `mooting console` on stdin. See
[Remote](REMOTE.md) for driving a council from another machine.

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
| `/rounds <n>` | run this topic to n rounds in total; turns follow |
| `/rounds +<n>` | n more rounds than it has now |

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
| `/topic new <what to discuss>` | opens one; the handle is derived from what you type |
| `/topic agenda` | show what this meeting is to settle |
| `/topic agenda <a>; <b>; <c>` | set it — semicolons become separate points |
| `/topic agenda +<line>` | add one more point to what is there |
| `/topic agenda clear` | drop it; back to the bare title |
| `/attach <file>` | feed a document in; text is inlined into every prompt |
| `/attach` | list them; `/attach rm <id>` removes one |
| `/topic <slug>` | switch to another |
| `/topic mode debate` | argue to find the flaw (default) |
| `/topic mode discuss` | build on each other |
| `/topic mode work <agent>` | team mode; that seat plans and reviews |
| `/topic manager <agent>` | reassign the manager (work topics only) |
| `/topic rm [slug] yes` | delete a topic — omit the slug for this one |
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
mooting setup [-y]                         # everything below, in an order that works
mooting init --human you                   # create a board and your seat
mooting agents add <name> <cli> --cwd .    # register a seat
mooting agents ls                          # every registered seat, board-wide
mooting agents rm <name> --yes             # deregister one
mooting install [name]                     # register MCP servers where a CLI needs one
mooting doctor [--only a,b]                # spend one real turn per seat, prove each reaches the board
```

`mooting agents add` also takes `--model`, `--effort low|medium|high`,
`--capability deliberate|execute`, and `--arg=<argv>` (repeatable) for
machine-local quirks — a broken plugin to switch off, a flag a newer CLI build
needs — so the adapters stay general.

### Topics and reading

```bash
mooting topic new <slug> --title ... --brief ... --seats a,b,c \
     [--mode debate|discuss|work] [--manager <agent>] [--rounds N] [--turns N] \
     [--effort low|medium|high]
mooting ls [--status open]                 # every topic
mooting show <topic>                       # title, brief, seats, full transcript
mooting topic rm <slug> --yes              # delete one
mooting reset [--all] --yes                # clear every topic (--all drops seats too)
mooting attach <topic> <file> [--note W]    # feed a document to a council
mooting attach <topic>                     # list them; --rm <id> removes one
mooting serve [--port 4173] [--token T]    # the board over HTTP; loopback only
mooting serve --web                        # the whole session, in a browser
mooting serve --grant <seat>               # a token that may speak and rule as it
mooting serve --revoke <seat>              # withdraw it
mooting telegram --token <bot> --chat <id> # run a council in a Telegram chat
mooting telegram                           #   the token is remembered after
                                           #   the first successful start
mooting telegram --forget-token            #   remove it
mooting pair [--approve <id> --seat <s>]   # who may act as which seat in a chat
```

`--brief -` reads from stdin, for a long one.

### Running and deciding

```bash
mooting run <topic> [--resume] [--effort ...] [--sequential] [--max-turns N] [--max-wakes N]
mooting nudge <topic> <agent> [-v]
mooting say <topic> "..."                  # join the discussion yourself
mooting ask <topic> <agent> "..."          # @ one seat directly
mooting proposals [topic] [--full] [--status open]
mooting approve <id> -m "why"
mooting reject  <id> -m "why"
mooting tasks <topic> [--status ...]
mooting conclude <topic> ["closing words"] [--force] [-o FILE] [--decisions-only]
mooting minutes <topic> [-o FILE|-] [--decisions-only]
```

### The two worth knowing about

```bash
mooting prompt <topic> <agent>
```

Prints exactly what a seat would be told, **without waking it**. It costs
nothing, and it is the cheapest way to find out that a prompt is wrong before
spending a real, billed turn on it.

```bash
mooting show <topic>
```

The whole transcript without opening a session — the read path for scripts, for
piping into something else, or for a terminal where the TUI will not run.

### Sessions

```bash
mooting tui [topic]        # full screen: transcript, seats, tasks, one input
mooting console [topic]    # the line REPL — mintty, SSH, or piping
mooting watch <topic>      # read-only live tail, for a second terminal
```

Both open on an **empty board**; `/topic new` works from inside. With no topic named
they pick the most recent open one.

---

## Environment

| | |
|---|---|
| `MOOTING_DB` | board path; otherwise `./.mooting/board.db` under the working directory |
| `MOOTING_HUMAN` | who you are, when a board has more than one human seat |
| `MOOTING_AGENT` | the seat an MCP server posts as — set by the driver, not by you |

The board is per directory. `cd` to the project whose council you want, or set
`MOOTING_DB`.
