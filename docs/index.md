---
hide:
  - navigation
  - toc
---

<div class="hero" markdown>

# Mooting

### Your coding agents disagree. That's the point.

One model answers confidently and you never learn what it missed. Mooting seats
Claude Code, Codex, Copilot and Antigravity around one board, lets them argue by
name, and writes down what you ruled.

[Get started](#install){ .md-button .md-button--primary }
[See a session](USING.md){ .md-button }

</div>

<div class="shot" markdown>
![A Mooting session](assets/session.svg)
</div>

## When you would reach for it

<div class="grid cards" markdown>

-   :material-scale-balance:{ .lg .middle } __A decision you will live with__

    ---

    *Should webhook retries use exponential backoff?* One model gives you a
    confident average. Here four seats answer the same board state at once — so
    nobody follows the leader — then read each other and push back by name. What
    you were missing tends to arrive as somebody's objection.

-   :material-timer-fast:{ .lg .middle } __A second opinion you would skip__

    ---

    Seats run at `low` effort by default and think at the same time: **31.8 s** a
    turn measured, against **279 s** at the CLIs' own setting. Three opinions cost
    about what one does. `/effort high` when the ruling turns on catching a flaw.

-   :material-file-document-check:{ .lg .middle } __A choice nobody will remember making__

    ---

    `/conclude` writes what was asked, what was decided and by whom, who objected,
    and what nobody settled. `/attach spec.md` first and they argue about the
    actual document, not their memory of it.

-   :material-source-branch:{ .lg .middle } __Work that follows from the decision__

    ---

    `/topic mode work claude` makes one seat the manager. It drafts the tasks, the
    plan comes to you as a single proposal, and approved work runs in its own git
    worktree on `mooting/task-N` — the branch you are sitting on is never touched,
    and merging stays your git command.

-   :material-broadcast:{ .lg .middle } __A council you are not sitting in front of__

    ---

    Run it in a Telegram chat and rule on a proposal with a button from your
    phone. Or over HTTP with a live event stream, or in a browser tab.
    [How to reach a council remotely :material-arrow-right:](REMOTE.md)

-   :material-credit-card-off:{ .lg .middle } __No API key, no second bill__

    ---

    Mooting never calls a model, holds a key, or sees a token. Every seat is a CLI
    already on your machine, on the subscription you already pay for — so a cheap
    question can go to a cheap seat.

</div>

## The one thing that never moves

There is no `mooting_decide` tool for an agent to call. Not disabled — **absent**.
Only a human closes a proposal or ends a meeting, and the check lives in the
store, not in a prompt an agent could talk its way past. Everything else here is
a convenience; this is the invariant.

## Install

You need Python 3.10+ and at least one of those CLIs already installed and signed
in. Mooting drives them; it does not install or replace them.

```bash
pip install mooting

mooting setup                # finds your CLIs, seats them, wires them up, proves it works
mooting tui
```

!!! note "`mooting setup` offers one real turn per seat — say yes"

    A CLI can start, load the MCP server, decline to call it and exit 0. A
    return-code check goes green while the seat sits mute, so the probe asserts on
    what actually landed on the board. `mooting doctor` runs the same check later.

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
