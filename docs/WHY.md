# Why it works the way it does

Design decisions, and the measurements behind them. None of this is needed
to use the tool — see [Home](index.md) for that.

## Three invariants

1. **The board is the substrate; the supervisor is an accelerator.** Everything the
   loop does, you can do by hand with `mooting nudge`. A failed wake degrades to
   catch-up-on-next-turn — a flaky adapter never deadlocks a topic.
2. **Caps pause, they never silently continue.** Live debate spends real
   subscription quota with nobody watching. Per-seat turn ceilings and per-hour wake
   ceilings park the topic for a human instead of burning a monthly allowance on
   chatter. A *failed* wake counts too, because metered CLIs charge for it.
3. **Execution needs two independent keys.** A seat edits files only if it was
   registered `--capability execute` **and** it is woken for an approved task on a
   `work` topic. An execute-capable seat sitting on a meeting topic stays read-only.
   Each adapter narrows itself in one place — a `tool_profile()` method, except
   Codex, whose containment is *where* it runs rather than a flag, because it
   offers no way to auto-approve an MCP call while staying read-only.

## Latency: measured, not guessed

A council is only useful if a round finishes while you are still looking at it.
On a real 10k-character council prompt:

| | wall-clock |
|---|---|
| one turn at default effort | **279 s** |
| the same turn at `--effort low` | **31.8 s** |
| process spawn + MCP handshake | ~5 s (≈2% of a default turn) |
| **a real 3-vendor round** (claude + codex + agy, concurrent, `low`) | **29.9 s** |

Against a sequential default-effort baseline of ~837 s for the same three seats,
that is roughly **28x** — all of it from effort and concurrency, none from transport.

Two conclusions, both counter-intuitive:

- **Latency is inference, not transport.** Persistent sessions and ACP look like
  the fix and would save about 2%. Effort is 8.8x.
- **A round should run concurrently.** Seats answer the same board state at once
  and react to each other next round, so round time is `max(seat)` instead of
  `sum(seat)`. This is the default; `mooting run --sequential` restores one-at-a-time
  when same-round rebuttal order matters.

Effort is set per council (`mooting run --effort`), per seat
(`mooting agents add --effort`), or per topic (`mooting topic new --effort`), resolving
topic → seat → council. The default is `low`, because that is what most sessions
are — a question, a few readings, keep moving — and at 31.8s a turn against 279s
it is the difference between a conversation and a wait. The tradeoff is real in
the other direction: the sharpest argument in our first live debate came from a
default-effort turn. Raise it with `/effort high` when the ruling hangs on
catching a flaw.

## Debate or discussion

A topic is framed one of two ways, and it changes what the seats do:

```
mooting topic new api-shape --mode discuss --title ... --brief ... --seats claude,codex
```

- **`debate`** (default) — *disagreement is the product*. Right when a decision
  turns on finding the flaw.
- **`discuss`** — *build on what others said; agreeing is a real contribution*.

The difference is only in the prompt, and it is not cosmetic: a capable model told
that disagreement is the product will **manufacture** disagreement to justify
spending a turn. That is exactly what you want when you are stress-testing a
decision, and exactly wrong when the room is trying to design something.

## Prior art — read this before adding to it

An earlier version of this file claimed that message buses move messages,
orchestrators fan out worktrees, and neither does structured deliberation with a
human arbiter. **That was wrong.** A proper survey found the space is crowded and
several projects are ahead of this one:

- **[LoopTroop](https://github.com/looptroop-ai/LoopTroop)** (MIT, ~1.3k commits) —
  the closest by far. Its *LLM Council* has independent models draft plans, score
  each other on a weighted rubric and **vote on proposals**; the winner synthesises
  the losing drafts; **a human approves before execution**; then "beads" execute in
  isolated git worktrees with Ralph-style retry. That is this project's meeting
  mode, plan gate and work mode, already built, with rubric scoring on top.
- **[Concord MCP](https://github.com/Get-Concord-AI/concord-mcp)** (MIT, TS) —
  architecturally near-identical to Mooting's core: an MCP server over local SQLite
  in `.concord/`, several vendor CLIs attached to one store, durable agent-to-agent
  threads, and a full-screen dashboard. It has **file-claim overlap detection**,
  which Mooting does not. It has no deliberation, votes, manager role or human gate.
- **[OpenCode agent teams](https://dev.to/uenyioha/porting-claude-codes-agent-teams-to-opencode-4hol)** —
  append-only JSONL inbox, **session injection** (messages delivered as synthetic
  user turns) and **auto-wake** that restarts a recipient's prompt loop on delivery.
  That is a better wake design than Mooting's 1s polling.
- **[Omnigent](https://github.com/omnigent-ai/omnigent)** — wraps each agent in
  `bwrap`/seatbelt. An OS-level sandbox is the correct fix for the containment
  problem Mooting currently works around by pointing a seat at an empty directory.
- **[Wit](https://github.com/amaar-mc/wit)** — symbol-level locks via Tree-sitter,
  finer-grained than worktree-per-task.
- **[CLITrigger](https://github.com/HyperAITeam/CLITrigger)**, **[Multica](https://github.com/multica-ai/multica)**,
  **[Paseo](https://github.com/getpaseo/paseo)**, **[ORCH](https://github.com/oxgeneral/ORCH)**,
  **[Agent Teams AI](https://github.com/777genius/agent-teams-ai)** — parallel vendor-CLI
  runners with approval gates, debate modes and Kanban control planes.
  The [awesome-cli-coding-agents](https://github.com/bradAGI/awesome-cli-coding-agents)
  index lists dozens more.

**What is arguably still distinctive here**, stated narrowly: the human-only
decision is a *structural* property rather than a UI gate — there is no
`mooting_decide` tool for an agent to call, and `Store.decide` is the only path out
of `draft`. And seats participate through their own vendor CLI and subscription,
so routing work by cost is possible.

That is a thin margin. Anyone picking this up should trial LoopTroop and Concord
first and only continue here if that margin matters to them.

---

## Reaching a council from elsewhere

Three ways to reach a council you are not sitting in front of, and why each
looks the way it does. The instructions live in [Remote](REMOTE.md); this is
the record of what was hard and what was left undone.

## Option A — serve the existing TUI to a browser

`textual-serve` runs a Textual app on a host and renders it in a browser over a
websocket. Mooting's TUI is an ordinary Textual app, so this is plausibly a very
small change.

**Sketch**

```python
# mooting/serve.py
from textual_serve.server import Server

def run(host: str, port: int, db: str, topic: str, me: str) -> int:
    Server(f"mooting tui {topic} --db {db} --as {me}",
           host=host, port=port).serve()
```

plus a `mooting serve --web` subcommand.

**Estimate:** a day, most of it not the serving.

### What makes it more than an afternoon

1. **Authentication is the fence, not a feature.** `Store.decide` refuses any
   non-human actor, and that check is the whole safety story: an agent cannot
   approve its own proposal. It is an *identity* check. The moment the session
   is on a port, whoever reaches that port **is** the human — they can approve
   plans, grant execute capability, and conclude meetings. `textual-serve` has
   no authentication of its own — see Using it, above, for how that is
   contained.
2. **One app instance per connection.** `Server` launches the command per
   session, so two viewers get two `mooting tui` processes on one board. The
   board tolerates that — WAL, `BEGIN IMMEDIATE`, one writer at a time — but
   both will spawn supervisors if both press Run. Needs a lock, or a rule that
   only one session drives.
3. **The bell and the clipboard do not cross.** `notify_turn` rings the
   terminal, which is what makes "it is your turn" reach you when you are not
   looking. In a browser tab that bell goes nowhere; it needs a visible
   substitute.
4. **Subprocess spawning stays on the host.** Seats run as CLIs on the serving
   machine with that machine's credentials. That is a feature — one set of
   subscriptions — but it means the host is doing the work and the browser is
   only a screen.

### Milestones

| | |
|---|---|
| A1 | **done.** `mooting serve --web` runs the real session in a browser through `textual-serve` — the transcript, the seat panel, the input that rules. Nothing is reimplemented. |
| A2 | **done.** A non-loopback bind is refused outright, naming what it would hand out; `--allow-remote` is the second saying-so. |
| A3 | **done.** A drive lock on the board: a second session cannot start a second supervisor, and an abandoned claim goes stale after 15 minutes rather than holding a topic for ever. |
| A4 | **done.** The `YOUR TURN` banner is recovered from board state rather than from the live event, so a session opened later still shows it, and `idle` reads `waiting on you` whenever something is outstanding. The bell remains, but nothing depends on it. |

---

## Option B — a board server, and thin clients

The real remote feature, and a genuine project rather than an afternoon.

One process owns the board. Everything else — a TUI, a web page, a phone, a
script — talks to it. This is also the only correct answer for two machines on
one council, because it restores a single writer.

### Shape

```
                    ┌──────────────────────────┐
  mooting tui ──────┤                          │
  browser     ──────┤   mooting serve          ├── SQLite board
  curl / CI   ──────┤   HTTP + SSE             │
                    │   owns the supervisor    ├── agent CLIs (subprocesses)
                    └──────────────────────────┘
```

The endpoints themselves are listed under **HTTP + events**, above. The reason
the event stream is SSE rather than a websocket: `Store.events_since(cursor)`
is already a monotonic cursor, so `GET /events?since=N` serves both the poll
and the live stream from the same query — a client that drops reconnects with
its last cursor and misses nothing. This is why the board was built with an
event log rather than change notifications.

### The three things that are actually hard

**1. Identity, because it is the fence.**
`HUMAN_KINDS` and `Store.decide` assume the caller's identity is known and
trustworthy — locally it comes from the OS. Over a socket it has to be proven.
Minimum viable: a bearer token per human seat, issued by
`mooting serve --grant <seat>`, checked on every request, and *never* accepted
from a query string (they end up in logs). A ruling must carry the seat it was
made by, and the server must refuse a token that maps to a non-human seat — the
same check as `Store.decide`, enforced one layer earlier so a compromised token
cannot even reach it.

**2. One supervisor, not N.**
Today the supervisor lives in whichever process is driving. With many clients,
the server owns it and clients ask it to run. `Supervisor` already takes a
`Store` and a driver map, so this is a lifecycle change rather than a rewrite:
one task per topic, started on `/run`, cancelled on `/stop`, and cancelled
properly on shutdown — the loop must not close with wakes in flight.

**3. The agents still run somewhere.**
Seats are subprocesses with the host's credentials. A server on a shared
machine means everyone on it spends *those* subscriptions. That is a policy
decision to make explicitly, not a detail: `--capability execute` grants file
writes in a named directory, and a remote caller who can grant capability can
write to that machine.

### Milestones

| | |
|---|---|
| B1 | **done.** `mooting serve` — loopback, one bearer token, `GET /topics`, `GET /topics/{slug}`, `POST /messages`, an SSE stream that resumes from a cursor, and a read-only page that follows a live round. |
| B2 | **done.** `PATCH /topics/{slug}` (agenda, effort, rounds), `POST .../run` and `.../stop` with one supervisor per topic — a second start is refused — and `GET /proposals/{id}`. Rulings still have no route. |
| B3 | **done.** `mooting serve --grant <seat>` issues one token per human seat. The shared startup token may read but cannot speak or rule. Every remote action leaves a `remote` event on the board. |
| B4 | **not built, and probably should not be.** It needs a `Store` implemented over HTTP — some fifty methods — to gain what SSH, `--web` and the Telegram bot already give: a council reachable from elsewhere. Worth revisiting only if a terminal client against a remote board is wanted for its own sake. |
| B5 | **done.** Two people are two seats: each speaks under their own name, holds their own token, and rules as themselves. Telegram pairing maps each person to a seat the same way. |

### What not to build

- **No hosted service.** The value is that it drives *your* CLIs on *your*
  subscriptions. A server someone else runs is a different product with a
  different threat model.
- **Do not replace SQLite.** One writer with WAL is sufficient for a council;
  the board is not the bottleneck, inference is — 31.8s a turn against ~5s of
  process spawn and handshake.
- **Do not put agent-callable actions on the HTTP surface.** Agents reach the
  board through MCP, which is where their fence is enforced. A second write
  path is a second place to get it wrong.

---

## Option C — a chat channel (Telegram first)

### What openclaw actually is, and what is worth copying

[openclaw](https://github.com/openclaw/openclaw) is a **trusted gateway**, in
three parts:

| openclaw | what it does | the same thing here |
|---|---|---|
| **Gateway** | "the local control plane for sessions, tools, events, and channel connections" | Option B — owns the board, the supervisor, the event stream |
| **Channels** | WhatsApp, Telegram, Slack, Discord, Signal, iMessage, Google Chat | this option |
| **Execution** | tools, skills, plugins, model providers | our drivers — first-party CLIs, on your subscriptions |

Two things are worth taking directly:

- **Pairing.** "DM-capable channels pair unknown senders by default; approve a
  pairing request with `openclaw pairing approve`." That is exactly the fence
  problem, solved the right way round: an unknown sender is inert until a human
  who already has authority approves them. Copy this rather than inventing
  an allowlist format.
- **One gateway, personal or shared.** "the same gateway runs as a personal
  assistant on one laptop or as a shared team deployment." Worth designing for
  from the start, because it is a change to identity, not to plumbing.

One thing is worth *not* taking: openclaw abstracts **model providers**. Mooting
drives first-party CLIs so every seat runs on a subscription you already pay for,
and that is the whole point of it. The execution layer stays as it is.

**So: B or C?** If Telegram is the only surface you want, C alone is enough —
the bot process can own the board directly. If you want what openclaw has —
several channels, a shared deployment, a web view later — then B is the gateway
those attach to, and C is the first channel on it. B first is the more expensive
order, and the right one if you mean "the same as openclaw".

### Why the adapter itself is small

The session is already two surfaces over one dispatch. `Board(Console)` in the
TUI describes itself as "Console's command set, wired to a widget instead of
stdout" — it swaps `emit` and how long work starts, nothing else. A chat adapter
is a third subclass:

```python
class ChatBoard(Console):
    def __init__(self, db, topic, me, send):
        super().__init__(db, topic, me)
        self.emit = send          # -> a message in the chat
```

Incoming text goes to `handle()` unchanged, so `/topic agenda`, `/approve`,
`@Santa …` and plain speech work the day the transport does. Outgoing events
come from `events_since(cursor)` — the cursor the TUI already polls, which is
why a bot that drops resumes without losing a round.

### The constraints, measured

These are from the Bot API, not from memory, because they decide the design:

| Limit | Value | Consequence |
|---|---|---|
| Message length | **4096 characters** | A single real reply from this project's own council ran past 2000. Two of them do not fit in one message. |
| Per chat | **~1 message/second**, bursts then `429` | A concurrent three-seat round finishes at once and must be drip-fed. |
| Per group | **20 messages/minute** | A 10-round council with 3 seats is 30 replies. Naive posting hits the wall in round 7. |
| Broadcast | ~30/second across all chats | Irrelevant for one council, relevant for a shared deployment. |

**MarkdownV2 is the real trap.** These must all be backslash-escaped outside an
entity:

```
_ * [ ] ( ) ~ ` > # + - = | { } . !
```

That list contains `.`, `-` and `!` — a full stop, a bullet, and an exclamation
mark. Every sentence an agent writes contains at least one, and an unescaped one
does not degrade: Telegram rejects the whole message. There are no tables in
MarkdownV2 at all, and agents produce them unprompted — one appeared in the
first real debate on this board.

So the renderer is not a nicety, it is the component:

1. Convert CommonMark → Telegram entities, or escape aggressively and send plain.
2. Tables become fenced monospace (they survive in `pre`), or an image.
3. Split on paragraph boundaries under 4096, never mid-entity.
4. On `429`, respect `retry_after` and queue — do not drop a seat's turn.
5. If a message is rejected, **fall back to plain text and send it anyway**. A
   reply that cannot be formatted must still arrive.

### Library

`aiogram` 3.31 — asyncio-native and classified as an AsyncIO framework, which
matches the supervisor. `python-telegram-bot` 22.8 is the mature alternative and
would also do; the deciding factor is that the bot process runs the same event
loop as `Supervisor.run_topic`.

Long polling to start. Webhooks need a public HTTPS endpoint, which is a
deployment problem to solve later, not a first milestone.

### Milestones

| | |
|---|---|
| C1 | **done.** Event pump, HTML renderer, splitter and throttle — all tested without a bot token. |
| C2 | **done.** Pairing lands on the board, approval is per chat, and `--chat` allowlists the room. |
| C3 | **done.** `ChatBoard` runs the same `Console.handle` the TUI uses, so every session command works in chat. Rulings still refused. |
| C4 | **done.** A proposal arrives with Approve / Reject / Read-it-all buttons. The callback carries the proposal id, so a ruling cannot land on the wrong one however far the chat has scrolled, and the reason is captured as a reply before the ruling is recorded. An unpaired tap is refused. |
| C5 | **done.** Each paired person acts as their own seat, and is given one on the topic when they first speak — `decide` asks only whether you are human, but `post` needs a seat, so without it the second person in a room could approve a plan and not say why. |

**Slack later:** the same adapter, different transport, and kinder limits — 40k
characters and real threads, which map onto `reply_to` almost exactly. Put the
split/escape/backoff logic behind one interface and write two transports.
