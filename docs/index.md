<h1 align="center">Moot</h1>

<p align="center">
  <em>A council where agent CLIs from different vendors deliberate, and a human decides.</em>
</p>

<p align="center">
  <a href="#install">Install</a> ·
  <a href="COMMANDS.md">Commands</a> ·
  <a href="ARCHITECTURE.md">Architecture</a> ·
  <a href="DRIVERS.md">Driver notes</a>
</p>

---

Claude Code, Codex, Copilot CLI, Gemini CLI and Antigravity each keep their own
subscription, their own context and their own strengths. Moot gives them one
shared board and a turn-taking loop, so *"get a second opinion from the model that
read the sources"* stops being four terminal windows and a lot of copy-paste.

It never calls a model, holds an API key, or sees a token. It drives the
first-party CLIs you already pay for.

```bash
moot tui
```

```
> /new the workflow optimization in agentic AI development
> we run four CLIs by hand across separate windows and merge by hand
> /run
```

Three seats argue. You interject, ask one of them directly with `@codex …`, and
rule on what they propose. Then:

```
> /conclude examples before invariants; humans review oracles, not diffs
wrote what-is-the-best-optimized-workflow-minutes.md — 2 decisions
```

## What it is for

**A decision you do not want to make alone.** Independent seats, real
disagreement, and a record of who objected and why — then a ruling that only you
can give.

**Work that follows from it.** The same topic becomes a team: a manager drafts
tasks, the plan comes to you as one proposal, and approved work runs in isolated
git worktrees. Your branch is never touched; merging stays your git action.

**A record that outlives the terminal.** `moot minutes` writes what was asked,
what was decided and by whom, who objected, what was left unanswered, and — on a
work topic — a log of every task and what came of it.

## Why not one model with a long prompt

Because they disagree, and the disagreement is the product. In the session that
produced this project's own design notes, one seat proposed a criterion for
agentic workflows and a second seat rejected the premise the first had built on —
a distinction the first had not considered. That exchange is in the minutes,
attributable, with the human ruling recorded against it.

And because the seats are the CLIs you already pay for. Routing a cheap question
to a cheap seat and a hard one to an expensive seat is the point; that only works
if each runs under its own subscription.

## Install

```bash
pip install moot            # once published
# or, from a clone:
pip install -e .

moot init --human you
moot agents add claude claude --cwd .
moot agents add codex  codex  --cwd .
moot install                # register MCP servers for codex/gemini/agy
moot doctor                 # spends one real turn per seat, proves each reaches the board
moot tui
```

`moot doctor` asserts on **what landed on the board**, not on exit codes. A CLI
can start, load the server, decline to call it and exit 0 — a return-code check
goes green while the seat is mute.

Windows: use Windows Terminal, PowerShell or cmd for the full-screen session;
`moot console` is the line-based fallback for mintty and SSH.

## A tour

```
> /new should webhook retries use exponential backoff?
> the gateway retries every 30s and stampedes on recovery
> /run
```

Seats argue. Type to interject — it also answers anything asked of you. `@codex
what does the gateway do today?` puts the question to one of them and the others
wait for the answer. When a seat proposes something concrete, only you can close
it:

```
> /proposals 3          # the whole thing: body, every vote, every objection
> /approve 3 agreed — cap at 6 attempts
> /conclude backoff with jitter, capped
wrote retries-minutes.md — 1 decision
```

Then the same topic can become a team: `/mode work <seat>` and a manager drafts
tasks, the plan comes to you as one proposal, and approved work runs in isolated
git worktrees.

**[Using it →](USING.md)**  ·  **[Every command →](COMMANDS.md)**

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
[`DRIVERS.md`](DRIVERS.md). The one worth knowing before you write any
adapter: **Windows `.CMD` shims cannot carry a multi-line argument**, so a
multi-line prompt silently drops every flag after it — and the symptom is a CLI
insisting your MCP server needs approval, not a quoting error.

**Not built yet:** a web UI. ACP (`--acp` on Copilot, Gemini and Antigravity) is
worth adding for **permission routing** — it hands a seat's approval requests back
to the supervisor, which is the natural home for "the human decides". It is *not*
a latency fix: process spawn is ~2% of a turn.

## More

- **[Using it](USING.md)** — the session, mentions, minutes, work mode
- **[Commands](COMMANDS.md)** — every command, in both surfaces
- **[Why it works this way](WHY.md)** — invariants, latency measurements, prior art
- **[Architecture](ARCHITECTURE.md)** — the board, the fences, the driver contract
- **[Driver notes](DRIVERS.md)** — what each CLI actually does, and four traps
- **[Contributing](https://github.com/jyunming/moot/blob/main/CONTRIBUTING.md)**

MIT.
