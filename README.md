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
  <a href="#install">Install</a> ·
  <a href="https://github.com/jyunming/mooting/blob/main/docs/USING.md">Using it</a> ·
  <a href="https://github.com/jyunming/mooting/blob/main/docs/COMMANDS.md">Commands</a> ·
  <a href="https://github.com/jyunming/mooting/blob/main/docs/ARCHITECTURE.md">Architecture</a>
</p>

---

Ask one model and you get a confident average. Mooting puts **Claude Code, Codex,
Copilot and Antigravity** in one room, each with its own context and its own
reading of the sources, and lets them argue about your code. You settle it — and
the argument is written down.

It never calls a model, holds an API key, or sees a token. Every seat is a CLI
already on your machine, running on the subscription you already pay for.

## Install

```bash
pip install mooting

mooting setup      # finds your CLIs, seats them, wires up MCP, proves each one works
mooting tui
```

Reaching a council from elsewhere — a browser, an HTTP client, a Telegram chat —
needs an extra and **0.1.1 or newer**: `pip install 'mooting[web]'`,
`'mooting[serve]'`, `'mooting[telegram]'`. See
[Remote](https://github.com/jyunming/mooting/blob/main/docs/REMOTE.md).

`mooting setup` spends one real turn per seat, because a CLI can start, load the
MCP server, decline to call it and still exit 0 — a return-code check goes green
while the seat is mute.

> **Windows** · use Windows Terminal, PowerShell or cmd for the full-screen
> session. `mooting console` is the line-based fallback for mintty and SSH.

## Ask. Argue. Rule.

```
> /topic new should webhook retries use exponential backoff?
> the gateway retries every 30s and stampedes on recovery
> /run
```

Every seat answers at once, so nobody follows the leader. They object, cite, and
question each other — `@codex what does the gateway do today?` puts it to one of
them and the others wait for the answer. A question put to *you* stops the room.

When a seat proposes something concrete, only you can close it:

```
> /proposals 3          # the whole thing: body, every vote, every objection
> /approve 3 agreed — cap at 6 attempts
> /conclude backoff with jitter, capped
wrote retries-minutes.md — 1 decision
```

The minutes record what was asked, what was decided and by whom, who objected,
and what nobody settled.

## Then put them to work

A decision nobody builds is just a nice conversation. `/topic mode work <seat>` makes
one seat the manager: it breaks the decision into tasks and brings you the plan
as a **single proposal**. Nothing runs before you approve it — there is no path
to a running task that does not go through you.

Approved work runs in isolated **git worktrees**, one branch per task. Your
branch is never touched, and merging stays your git action. The work log is
written the same way the minutes are.

## Conversation, or deliberation

Most of the time you are thinking out loud and want the room to keep up.
Sometimes it is the decision you will still be living with next year.
`/effort` switches mid-session.

| Setting | One turn | Reach for it when |
|---|---|---|
| **Conversation** `low` — the default | ~30 seconds | Brainstorming, sanity checks, narrowing a shortlist |
| **Deliberation** `high` | a few minutes | Design reviews, architecture calls, hard trade-offs |

And they think at the same time, so three opinions cost about what one does.

## The seats

Measured against each binary, not read from its documentation.

| Seat | Result |
|---|---|
| **Claude Code** 2.1.250 | **working** — posts to the board, resumes by our own UUID |
| **Codex** 0.149.0 | **working** — needs `--approve-for-me` and its prompt on stdin |
| **Antigravity** (`agy`) 1.1.20 | **working** — `--mode plan` is genuinely read-only |
| **Copilot** 1.0.81 | driver verified against the CLI |

The trap worth knowing before you write any adapter: **Windows `.CMD` shims
cannot carry a multi-line argument**, so a multi-line prompt silently drops every
flag after it — and the symptom is a CLI insisting your MCP server needs
approval, not a quoting error. The rest are in
[the driver notes](https://github.com/jyunming/mooting/blob/main/docs/DRIVERS.md).

**Not built yet:** a web UI. ACP (`--acp` on Copilot and Antigravity) is worth
adding for **permission routing** — it hands a seat's approval requests back to
the supervisor, which is the natural home for "the human decides". It is *not* a
latency fix: process spawn is ~2% of a turn.

## More

- **[Using it](https://github.com/jyunming/mooting/blob/main/docs/USING.md)** — the session, mentions, minutes, work mode
- **[Commands](https://github.com/jyunming/mooting/blob/main/docs/COMMANDS.md)** — every command, in both surfaces
- **[Why it works this way](https://github.com/jyunming/mooting/blob/main/docs/WHY.md)** — invariants, latency measurements, prior art
- **[Architecture](https://github.com/jyunming/mooting/blob/main/docs/ARCHITECTURE.md)** — the board, the fences, the driver contract
- **[Driver notes](https://github.com/jyunming/mooting/blob/main/docs/DRIVERS.md)** — what each CLI actually does, and four traps
- **[Remote](https://github.com/jyunming/mooting/blob/main/docs/REMOTE.md)** — over SSH, over HTTP, or from a Telegram chat
- **[Contributing](https://github.com/jyunming/mooting/blob/main/CONTRIBUTING.md)**

MIT.
