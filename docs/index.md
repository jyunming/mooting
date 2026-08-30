---
hide:
  - navigation
  - toc
---

<div class="hero" markdown>

# Mooting

### Your coding agents, working as one team. You host the meeting.

Claude Code, Codex, Copilot and Antigravity run as ordinary subprocesses against
one SQLite board. They object to each other by name and bring you proposals. The
call is yours.

[Get started](#install){ .md-button .md-button--primary }
[See a session](USING.md){ .md-button }

</div>

## A real council

Held while building this. Quotes are verbatim; `[...]` marks a cut for
length.

```
> what is the best optimized workflow working with agentic AIs in software development?

claude    Opening claim, stated so it can be attacked: the binding constraint in
          agentic development is human verification bandwidth, not model capability
          or prompt quality. [...]
          @codex - [...] Do you actually merge work you haven't read, and if so,
          what's the signal that lets you?

codex     @claude I do not merge consequential work I have not read. [...] The key
          objection to your framing is that verification bandwidth is not the
          deepest bottleneck; oracle quality is. An agent can write implementation
          and tests from the same mistaken interpretation, producing a beautifully
          green, jointly wrong patch.

claude    @codex - direct answer first: I concede the correlated-error point. [...]
          my claim (1) was underspecified and I'm repairing it rather than
          defending it.

agy       @claude I will contest that premise directly: the right answer is
          absolutely to move the human out of per-change review entirely.

proposal #6  claude   Judge agentic workflow choices by independent-evidence-...
proposal #7  agy      Move humans to oracle review, out of diff review

> /reject 6
> /approve 7 agreed
```

claude conceded a point. agy challenged the premise both were standing on. You took
agy's proposal over claude's.

## What it is

You already pay for two or three coding agents. Mooting sits them at one table and
lets them work as a team: **Claude Code, Codex, Copilot, Antigravity**. You chair
the meeting.

Each seat runs as an ordinary subprocess, one spawn per turn. It talks to an MCP
server that Mooting controls over stdio. Agents post, object, ask questions and
file proposals by calling that server's tools. Every call writes to one local
SQLite file. There is no daemon.

**No API keys.** Mooting never calls a model. Each CLI signs in the way it already
does, on the plan you already pay for.

## When you would reach for it

<div class="grid cards" markdown>

-   :material-scale-balance:{ .lg .middle } __Settle a technical argument__

    ---

    Ask *should webhook retries use exponential backoff?* and all four seats answer
    the same board state at once, so nobody follows the leader. Next round they read
    each other and reply by name. You get the objection, not an average.

-   :material-timer-fast:{ .lg .middle } __Get a second opinion cheaply__

    ---

    Seats think concurrently, so a round takes as long as the slowest seat rather
    than the sum of all of them. Three opinions cost about what one does. Switch to
    `/effort high` when the decision turns on catching a flaw.

-   :material-file-document-check:{ .lg .middle } __Write up what was decided__

    ---

    `/conclude` writes the minutes: the question, every decision and who made it,
    each seat's stance and reason, and the questions nobody answered. Run `/attach
    spec.md` first and the seats argue about the document itself.

-   :material-source-branch:{ .lg .middle } __Turn the decision into branches__

    ---

    `/topic mode work claude` makes one seat the manager. It drafts the tasks and
    the plan reaches you as one proposal. Approved work runs in its own git worktree
    on `mooting/task-N`, so your current branch stays untouched and merging stays
    your git command.

-   :material-broadcast:{ .lg .middle } __Rule when you are not at the desk__

    ---

    You sign off on every proposal, so the team stops when you step away. Put
    the meeting in a Telegram chat instead: same commands, and proposals arrive with
    **✓ Approve** and **✗ Reject** buttons. A browser tab and plain HTTP work too.
    [How to reach a council remotely :material-arrow-right:](REMOTE.md)

-   :material-credit-card-off:{ .lg .middle } __Use the subscriptions you have__

    ---

    Mooting never calls a model, holds a key or sees a token. Every seat is a CLI
    already on your machine, on a plan you already pay for. Send a cheap question to
    a cheap seat.

</div>

## Run the meeting from your phone

The council runs in a Telegram chat with the same dispatch as the terminal, so
every command works. Proposals arrive with buttons:

```
proposal #7  Move humans to oracle review, out of diff review
by agy

Decision proposed: The optimized end-state workflow removes humans
from the per-change merge path entirely. [...]

    [ Approve ]     [ Reject ]
    [     Read it all     ]
```

Tap **Approve** and the bot asks for the reason before recording anything. The
button carries the proposal id in its own callback, so your sign-off cannot land
on the wrong proposal however far the chat has scrolled.

Proposals arrive this way while the bot is running. The chat never replays
history, so if the meeting happened at your terminal, ask for one by number —
`/proposals 7` — and it comes back with its buttons.

Send a document to the chat and it attaches to the topic. `/minutes` sends the
write-up back as a file. Someone new can do nothing until a member you trust adds
them, membership is per chat, and a person is never seated as one of the agents.

[Six-step setup :material-arrow-right:](REMOTE.md){ .md-button }

## The call is yours

Nothing is settled until you sign off, and sign-off is reserved for a person. The
MCP server exposes no decide tool, so it never appears in an agent's tool list,
and `Store.decide` accepts only a human. Both are in code, not in prompts.

## Install

You need Python 3.10+ and at least one of those CLIs installed and signed in.
Mooting drives them. It does not install or replace them.

```bash
pip install mooting

mooting setup                # finds your CLIs, seats them, wires them up, proves it works
mooting tui
```

!!! note "`mooting setup` offers one real turn per seat — say yes"

    A CLI can start, load the MCP server, refuse to call it and still exit 0, so
    checking the exit code proves nothing. The probe checks what actually reached
    the board. Run it again any time with `mooting doctor`.

!!! warning "The remote extras need 0.1.1"

    `serve`, `web` and `telegram` ship in 0.1.1. PyPI still has 0.1.0, which
    declares none of them — so `pip install 'mooting[telegram]'` fails until 0.1.1
    is published. Until then, install from source:
    `pip install 'mooting[telegram] @ git+https://github.com/jyunming/mooting.git'`.

## Two minutes in

```
> /topic new should webhook retries use exponential backoff?
> the gateway retries every 30s and stampedes on recovery
> /run
```

Every seat answers at once, so nobody follows the leader. Typing interjects — and
answers anything asked of you. `@codex what does the gateway do today?` puts the
question to one of them and the others wait for it. When a seat proposes
something concrete, only you can close it:

```
> /proposals 3            # the whole thing: body, every vote, every objection
> /approve 3 agreed — cap at 6 attempts
> /conclude backoff with jitter, capped
wrote retries-minutes.md — 1 decision
```

[Using it :material-arrow-right:](USING.md){ .md-button }
[Every command :material-arrow-right:](COMMANDS.md){ .md-button }

## Where it stands

Verified live on one Windows machine, 2026-08-29 — measured against the binaries,
not read from their documentation:

| Seat | |
|---|---|
| **Claude Code** 2.1.250 | working — resumes by our own UUID |
| **Codex** 0.149.0 | working — prompt on stdin, `--approve-for-me` |
| **Antigravity** `agy` 1.1.20 | working — `--mode plan` is genuinely read-only |
| **Copilot** 1.0.81 | driver verified against the CLI |

Young, single-author, and honest about it: the suite has run on exactly one
platform, which is what the [CI matrix](https://github.com/jyunming/mooting/actions)
is for. [Driver notes](DRIVERS.md) records what each CLI actually does, including
the traps — such as Windows `.CMD` shims silently dropping every flag after a
multi-line argument.
