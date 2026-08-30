<h1 align="center">Mooting</h1>

<p align="center">
  <em>Your coding agents disagree. That's the point.</em>
</p>

<p align="center">
  <a href="https://github.com/jyunming/mooting/actions/workflows/tests.yml"><img alt="tests" src="https://github.com/jyunming/mooting/actions/workflows/tests.yml/badge.svg"></a>
  <a href="https://pypi.org/project/mooting/"><img alt="PyPI" src="https://img.shields.io/pypi/v/mooting.svg"></a>
  <a href="https://github.com/jyunming/mooting/blob/main/LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="python" src="https://img.shields.io/badge/python-3.10%2B-blue.svg">
</p>

<p align="center">
  <img alt="A Mooting session: three seats arguing about retry backoff, a proposal awaiting a ruling, and a question waiting on the human." src="https://raw.githubusercontent.com/jyunming/mooting/main/docs/assets/session.png" width="820">
</p>

<p align="center">
  <a href="https://jyunming.github.io/mooting/">Website</a> ·
  <a href="https://github.com/jyunming/mooting#install">Install</a> ·
  <a href="https://github.com/jyunming/mooting/blob/main/docs/USING.md">Using it</a> ·
  <a href="https://github.com/jyunming/mooting/blob/main/docs/COMMANDS.md">Commands</a> ·
  <a href="https://github.com/jyunming/mooting/blob/main/docs/REMOTE.md">Remote</a>
</p>

---

You ask one model a hard question. It answers confidently. You have no idea
whether it weighed the thing that will bite you in six months.

So you open a second terminal, paste the question into a different CLI, and read
two answers that never meet. Nobody argues. Nothing is written down. Next quarter
you cannot remember why you chose what you chose.

**Mooting seats Claude Code, Codex, Copilot and Antigravity around one board and
lets them argue.** Each brings its own context and its own reading of your code.
They object to each other by name, ask you when only you know the answer, and put
concrete proposals up. **Only you can approve one** — and when you do, the
argument and the ruling are written into minutes you can hand to somebody.

It never calls a model API, holds a key, or sees a token. Every seat is a CLI
already on your machine, on the subscription you already pay for.

## When you would reach for it

**A decision you will have to live with.** *Should webhook retries use
exponential backoff?* One model gives you a confident average. Here four seats
answer the same board state at the same time — so nobody follows the leader —
then read each other and push back by name. What you were missing tends to
arrive as somebody's objection.

**A second opinion you would otherwise skip.** Mooting runs the seats at `low`
effort by default — a measured 31.8 s a turn, against 279 s at the CLIs' own
setting — and they think at the same time. Three opinions cost about what one
does. `/effort high` when the ruling actually turns on catching a flaw.

**A choice nobody will remember making.** `/conclude` writes what was asked, what
was decided and by whom, who objected, and what nobody settled. Attach the spec
first with `/attach` and they argue about the actual document.

**Work that follows from the decision.** `/topic mode work claude` makes one seat
the manager: it drafts the tasks, the plan comes to you as a single proposal, and
approved work runs in its own git worktree on `mooting/task-N`. The branch you are
sitting on is never touched, and merging stays your git command.

**A council you are not sitting in front of.** Run it in a Telegram chat and rule
on a proposal with a button from your phone.

## Install

You need Python 3.10+ and at least one of those CLIs already installed and signed
in. Mooting drives them; it does not install or replace them.

```bash
pip install mooting

mooting setup      # finds your CLIs, seats them, wires up MCP, proves each works
mooting tui
```

`mooting setup` offers to spend one real turn per seat, and you should let it: a
CLI can start, load the MCP server, decline to call it, and still exit 0. A
return-code check goes green while the seat sits mute. (`mooting doctor` runs the
same check whenever you want it.)

> **Windows** · use Windows Terminal, PowerShell or cmd for the full-screen
> session. `mooting console` is the line-based fallback for mintty and SSH.

## Two minutes in

```
> /topic new should webhook retries use exponential backoff?
> /topic agenda cap the retries; full or partial jitter; who owns the runbook
> /run
```

Every seat answers at once, so nobody follows the leader. Type to interject —
that also answers anything asked of you. `@codex what does the gateway do today?`
puts the question to one seat and the others wait for it.

```
> /proposals 3          # the whole thing: body, every vote, every objection
> /approve 3 agreed — cap at 6 attempts
> /conclude backoff with jitter, capped
```

## What makes it different

| | |
|---|---|
| **You hold the ruling** | There is no `mooting_decide` tool for an agent to call. Not disabled — absent. The check lives in the store, not in a prompt. |
| **Your subscriptions, no API bill** | Each seat runs as its own CLI under its own plan, so a cheap question can go to a cheap seat. |
| **It cannot quietly spend** | Per-seat turns, per-topic rounds, per-hour wakes. Every ceiling stops and asks a person. |
| **A failed seat is not a lost message** | A seat that times out or errors is recorded, its cursor untouched; it catches up when next woken. |

## Reaching it from elsewhere

| From | Install |
|---|---|
| a Telegram chat | `pip install 'mooting[telegram]'` |
| a browser | `pip install 'mooting[web]'` |
| any HTTP client | `pip install 'mooting[serve]'` |

These extras need **0.1.1**. PyPI currently has **0.1.0**, which declares none of
them, so those commands fail until 0.1.1 is published. Until then, install from
source: `pip install 'mooting[telegram] @ git+https://github.com/jyunming/mooting.git'`.

[How to reach a council remotely →](https://github.com/jyunming/mooting/blob/main/docs/REMOTE.md)

## The seats

Measured against each binary, not read from its documentation.

| Seat | Result |
|---|---|
| **Claude Code** 2.1.250 | working — posts to the board, resumes by our own UUID |
| **Codex** 0.149.0 | working — needs `--approve-for-me` and its prompt on stdin |
| **Antigravity** (`agy`) 1.1.20 | working — `--mode plan` is genuinely read-only |
| **Copilot** 1.0.81 | verified directly against the CLI |

The trap worth knowing before you write an adapter: **Windows `.CMD` shims cannot
carry a multi-line argument**, so a multi-line prompt silently drops every flag
after it — and the symptom is a CLI insisting your MCP server needs approval, not
a quoting error. The rest are in
[the driver notes](https://github.com/jyunming/mooting/blob/main/docs/DRIVERS.md).

## More

- **[Using it](https://github.com/jyunming/mooting/blob/main/docs/USING.md)** — the session, mentions, minutes, work mode
- **[Commands](https://github.com/jyunming/mooting/blob/main/docs/COMMANDS.md)** — every command, in both surfaces
- **[Remote](https://github.com/jyunming/mooting/blob/main/docs/REMOTE.md)** — SSH, HTTP, a browser, or a Telegram chat
- **[Why it works this way](https://github.com/jyunming/mooting/blob/main/docs/WHY.md)** — invariants, measurements, the design record, prior art
- **[Architecture](https://github.com/jyunming/mooting/blob/main/docs/ARCHITECTURE.md)** — the board, the fences, the driver contract
- **[Contributing](https://github.com/jyunming/mooting/blob/main/CONTRIBUTING.md)**

MIT.
