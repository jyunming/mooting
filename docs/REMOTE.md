# Driving a council from somewhere else

Four ways to reach a council you are not sitting in front of, in increasing
order of what they cost to build. The first works now. The rest are plans,
written down before anyone starts, because in every one of them the hard part is
not the transport — it is that the human ruling is enforced by identity, and a
remote caller has none until you give them one.

---

## What already works: SSH

The board is a plain SQLite file and nothing needs a daemon, so a shell on the
machine is a complete interface:

```bash
ssh box "cd project && mooting topic new reno \
    --title 'Renovation in Leuven' --brief 'Renovation in Leuven' \
    --seats Kevin,Sam,Santa,you"
ssh box "cd project && mooting topic agenda reno \
    'what budget; which rooms; heat pump or not'"
ssh box "cd project && mooting run reno"
```

`mooting console` is the line-based session for exactly this: `mooting tui`
wants a real terminal, `console` does not, and both drive the same board through
the same dispatch.

The one thing missing until recently was `mooting topic agenda` — you could open
a topic and start it remotely, but not say what it was for. That is now in the
shell.

**What SSH does not give you:** a session two people can watch at once, a phone,
or anything that survives the connection dropping mid-round.

> **Do not** put the board on OneDrive, Dropbox or an SMB share to "sync" it.
> SQLite's locking assumes a real filesystem; cloud-sync clients copy files out
> from under open handles, and the failure mode is a corrupt board, not an
> error. If two machines must reach one board, they need a process in front of
> it — which is option B.

---

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
   no authentication of its own. Bind to `127.0.0.1` and reach it through an SSH
   tunnel, or put it behind a reverse proxy that authenticates; never bind it to
   an interface without one.
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
  curl / CI   ──────┤   HTTP + websocket       │
                    │   owns the supervisor    ├── agent CLIs (subprocesses)
                    └──────────────────────────┘
```

**HTTP** for state and actions, because they are requests with answers:

| | |
|---|---|
| `GET /topics`, `GET /topics/{slug}` | what exists, and its agenda |
| `POST /topics` | open one |
| `PATCH /topics/{slug}` | agenda, mode, manager, rounds, effort |
| `POST /topics/{slug}/messages` | say something, or answer a question |
| `POST /topics/{slug}/run` · `/stop` | drive the council |
| `POST /proposals/{id}/decide` | **the ruling** — see the fence below |
| `GET /topics/{slug}/minutes` | the written record |

**Websocket** for the event stream, because the board already has one:
`Store.events_since(cursor)` is a monotonic cursor, so `GET /events?since=N`
and a socket that pushes the same events are the same query. A client that
drops reconnects with its last cursor and misses nothing. This is why the
board was built with an event log rather than change notifications.

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
| B3 | **done.** One token per human seat — `mooting serve --grant <seat>` — and a token is an identity, so it can only ever be issued to a human. The shared startup token may read but cannot speak or rule. Every remote action leaves a `remote` event on the board. |
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

### Setting it up, step by step

**1. Make a bot.** In Telegram, message **@BotFather** and send `/newbot`.
Follow the two prompts (a display name, then a username ending in `bot`). It
replies with a token like `8123456789:AAF...`. That token is a password.

**2. Install and seat a council.**

```bash
pip install 'mooting[telegram]'      # 0.1.1 or newer — 0.1.0 has no extras
cd your-project
mooting setup                        # finds your CLIs, seats them, proves them
```

From a clone instead: `pip install -e '.[telegram]'`.

**3. Start the bot.**

```bash
mooting telegram --token 8123456789:AAF...
```

**Once.** After Telegram accepts it the token is kept on the board, so every
later run is just `mooting telegram`. It is saved only after it has worked, so
a mistyped one is never remembered; `--no-save` uses it without keeping it, and
`--forget-token` removes it. `$TELEGRAM_BOT_TOKEN` also works if you would
rather keep it in the environment.

Leave it running. It long-polls; there is no webhook or public address to set up.

**4. Pair yourself.** In Telegram, send anything to your bot. It replies that
you are not paired and gives you a request number. Approve it from the terminal
you started the bot in:

```bash
mooting pair                       # lists what is waiting
mooting pair --approve 1           # the seat is named after you
```

This is the same shape as openclaw's `openclaw pairing approve telegram <CODE>`:
the first person is approved by whoever has the machine, because at that point
nobody else has any authority to grant.

> **Shortcut.** On a board where nobody is paired yet, startup also prints a
> one-time code — `send /pair 9f2c1a to the bot to claim the first seat`. Sending
> that pairs you without switching back to the terminal. It is ours, not
> openclaw's, and it is good once. Ignore it if you would rather use step 4.

**5. Find your chat id and lock the bot to that room.** When you pair, the bot
tells you the id. Then restart it with:

```bash
mooting telegram --token ... --chat -1001234567890
```

Without `--chat` the bot answers **anywhere it is added**, which hands that room
your subscriptions. With it, a message from anywhere else is ignored and the id
is printed so you can allowlist it deliberately.

**6. Hold a council.**

```
/topic new should we cap webhook retries?
/topic agenda cap the retries; full or partial jitter
/run
```

Then talk: plain messages post as you, `@Santa what about the windows?` puts a
question to one seat, `/seats` and `/proposals` show where things stand.

### Adding other people

Once you are paired, approving happens in the chat — no terminal:

```
them:  /pair
bot:   You are not paired here. Request 2 is waiting for a member to approve it.
you:   /pair approve 2
bot:   A Colleague now speaks as AColleague.
```

`/pair list` shows what is waiting; `/pair deny <id>` refuses. This part goes
beyond openclaw, which approves only from the shell.

The seat is derived from their own display name rather than invented, and it is
always a **human** seat: pairing a person onto an agent seat would let them
speak as one, and put a non-human name against something only a human may do.

Approval is **per chat** — being trusted in one council is not being trusted in
another — and `mooting pair --approve <id> --seat <s>` still works from the
shell, which is how you recover if you lose access to the chat.

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
| C2 | **done.** Pairing on the board: an unknown sender is inert until a paired member approves them, approval is per chat, and a person cannot be paired onto an agent seat. `--chat` allowlists the room. |
| C3 | **done.** `ChatBoard` runs the same `Console.handle` the TUI uses, so every session command works in chat. Rulings still refused. |
| C4 | **done.** A proposal arrives with Approve / Reject / Read-it-all buttons. The callback carries the proposal id, so a ruling cannot land on the wrong one however far the chat has scrolled, and the reason is captured as a reply before the ruling is recorded. An unpaired tap is refused. |
| C5 | **done.** Each paired person acts as their own seat, and is given one on the topic when they first speak — `decide` asks only whether you are human, but `post` needs a seat, so without it the second person in a room could approve a plan and not say why. |

**Slack later:** the same adapter, different transport, and kinder limits — 40k
characters and real threads, which map onto `reply_to` almost exactly. Put the
split/escape/backoff logic behind one interface and write two transports.

---

## Choosing

| You want | Do this |
|---|---|
| Set up a council from your laptop, on a machine that has the CLIs | SSH. Works today. |
| Watch a round from a phone on your own network | Option A behind an SSH tunnel |
| Two people in one council, or a client that is not a terminal | Option B |
| A council that keeps running while nobody watches | Option B or C — both own a supervisor |
| To run a council from your phone, in a group, with other people | **Option C** |
