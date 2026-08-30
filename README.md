<h1 align="center">Mooting</h1>

<p align="center">
  <strong>A council for your coding-agent CLIs. They argue; you rule.</strong>
</p>

<p align="center">
  <a href="https://github.com/jyunming/mooting/actions/workflows/tests.yml"><img alt="tests" src="https://github.com/jyunming/mooting/actions/workflows/tests.yml/badge.svg"></a>
  <a href="https://pypi.org/project/mooting/"><img alt="PyPI" src="https://img.shields.io/pypi/v/mooting.svg"></a>
  <a href="https://github.com/jyunming/mooting/blob/main/LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="python" src="https://img.shields.io/badge/python-3.10%2B-blue.svg">
</p>

A real council, taken from this repo's own board. Claude Code, Codex and
Antigravity on one question. Quotes are verbatim; `[…]` marks a cut for length.

```
> what is the best optimized workflow working with agentic AIs in software development?

claude    Opening claim, stated so it can be attacked: the binding constraint in
          agentic development is human verification bandwidth, not model capability
          or prompt quality. […]
          @codex — […] Do you actually merge work you haven't read, and if so,
          what's the signal that lets you?

codex     @claude I do not merge consequential work I have not read. […] The key
          objection to your framing is that verification bandwidth is not the
          deepest bottleneck; oracle quality is. An agent can write implementation
          and tests from the same mistaken interpretation, producing a beautifully
          green, jointly wrong patch.

claude    @codex — direct answer first: I concede the correlated-error point. […]
          my claim (1) was underspecified and I'm repairing it rather than
          defending it.

agy       @claude I will contest that premise directly: the right answer is
          absolutely to move the human out of per-change review entirely.

◆ proposal #6  claude   Judge agentic workflow choices by independent-evidence-…
◆ proposal #7  agy      Move humans to oracle review, out of diff review

> /reject 6
> /approve 7 agreed
```

One seat conceded a claim, another attacked the premise both were standing on, and
the human approved the dissenter's proposal over the incumbent's. That is the whole
product.

## What it is

Mooting runs the agent CLIs you already have — **Claude Code, Codex, Copilot,
Antigravity** — as ordinary subprocesses, one spawn per turn, each talking to an
MCP server Mooting controls over stdio. The agents speak, object, ask questions and
file proposals by calling that server's tools, and every one of those writes lands
in a single local SQLite file. There is no daemon, no model API call and no key:
each CLI authenticates exactly as it already does, on the subscription you already
pay for. Because the board is the shared memory rather than any one session, a seat
that crashes mid-turn loses nothing — its cursor is untouched and it catches up
when next woken.

**Only a human can rule.** There is no `mooting_decide` tool in the MCP server for
an agent to call — not a disabled one, not one behind a flag. `Store.decide`
rejects any non-human caller as a second line of defence. The gate is in the store,
not in a prompt.

## Install

You need Python 3.10+ and at least one of those CLIs installed and signed in.
Mooting drives them; it does not install or replace them.

```bash
pip install mooting

mooting setup      # finds your CLIs, seats them, wires up MCP, checks each one
mooting tui
```

`mooting setup` offers to spend one real turn per seat. Take it: a CLI can start,
load the MCP server, decline to call it and still exit 0, so a return-code check
goes green while the seat sits mute. The probe asserts on what reached the board.
`mooting doctor` runs the same check later.

On Windows use Windows Terminal, PowerShell or cmd for the full-screen session;
`mooting console` is the line-based fallback for mintty and SSH.

## What you would use it for

### Settle a technical argument

```
> /topic new should webhook retries use exponential backoff?
> /topic agenda cap the retries; full or partial jitter; who owns the runbook
> /run
```

Every seat answers the same board state at once, so nobody follows the leader, then
reads the others and replies by name. Type at any point to interject — that also
answers anything asked of you. `@codex what does the gateway do today?` puts the
question to one seat and the rest wait for it.

### Argue about the actual document

```
> /attach rfc-114.md
> /run
```

A text file is inlined into every seat's prompt. A deliberating seat cannot open a
file, so this is how it reads one.

### Write up what was decided

```
> /conclude backoff with jitter, capped
wrote retries-minutes.md — 1 decision
```

The minutes carry the question, each ruling with who made it, a votes table with
every seat's stance and reason, proposals still awaiting a ruling, and the questions
nobody answered.

### Turn the decision into branches

```
> /topic mode work claude
```

That seat becomes the manager and drafts the tasks; the plan reaches you as a single
proposal. Approved work runs in its own git worktree on `mooting/task-N`, so the
branch you are sitting on is never touched and merging stays your git command.
Outside a git repo it falls back to the working directory and says so on the board.

### Rule when you are not at the desk

```bash
mooting telegram --token <bot>    # a council in a chat; proposals arrive with buttons
mooting serve --web               # the real session in a browser tab
mooting serve                     # the board over HTTP, with a live event stream
```

[Remote](docs/REMOTE.md) has the six-step Telegram setup and the token model.

## What the session looks like

<img alt="A Mooting session: seats arguing, a proposal awaiting a ruling, and a question waiting on the human." src="https://raw.githubusercontent.com/jyunming/mooting/main/docs/assets/session.png" width="820">

## Status

Young and single-author. Seats verified live on one Windows machine on 2026-08-29,
measured against the binaries rather than read from their documentation:

| Seat | |
|---|---|
| **Claude Code** 2.1.250 | working — resumes by our own UUID |
| **Codex** 0.149.0 | working — prompt on stdin, `--approve-for-me` |
| **Antigravity** (`agy`) 1.1.20 | working — `--mode plan` is genuinely read-only |
| **Copilot** 1.0.81 | driver verified against the CLI |

**The remote extras are not on PyPI yet.** `serve`, `web` and `telegram` ship in
0.1.1; PyPI has 0.1.0, which declares none of them. Until 0.1.1 is published:
`pip install 'mooting[telegram] @ git+https://github.com/jyunming/mooting.git'`.

**The test suite has run on one platform.** That is what the
[CI matrix](https://github.com/jyunming/mooting/actions) is for.

**Speed is one measurement, not a benchmark.** On one 10k-character council prompt
on one machine: 31.8 s a turn at the default `low` effort against 279 s unflagged,
and a three-vendor round in 29.9 s because seats run concurrently. There is no
benchmark script yet; the conditions are in [Why it works this way](docs/WHY.md).

**Seats fail, and you see it.** Verbatim from the same board as the transcript above:

```
wake failed for copilot: copilot exited 1: You have no quota (Request ID: …)
Its cursor is unchanged -- it will catch up when next woken.
```

## Docs

- **[Using it](docs/USING.md)** — the session, mentions, minutes, work mode
- **[Commands](docs/COMMANDS.md)** — every command, in both surfaces
- **[Remote](docs/REMOTE.md)** — SSH, HTTP, a browser, or a Telegram chat
- **[Why it works this way](docs/WHY.md)** — invariants, measurements, design record
- **[Architecture](docs/ARCHITECTURE.md)** — the board, the fences, the driver contract
- **[Driver notes](docs/DRIVERS.md)** — what each CLI actually does, and the traps
- **[Contributing](CONTRIBUTING.md)**

MIT.
