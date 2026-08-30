# Driving a council from somewhere else

Four ways to reach a council you are not sitting in front of. All four work
today: SSH needs nothing extra; a browser tab, an HTTP API, and a Telegram
chat all run on top of one board server. In every one of them the hard part is
never the transport — it is that sign-off is enforced by identity, and
a remote caller has none until you give them one.

---

## Choosing

| You want | Do this |
|---|---|
| Set up a council from your laptop, on a machine that has the CLIs | SSH — works today, see below |
| Watch a round from a phone on your own network | Browser (`mooting serve --web`), behind an SSH tunnel |
| Two people in one council, or a client that is not a terminal | HTTP + events (`mooting serve`) |
| A council that keeps running while nobody watches | HTTP + events or Telegram — both own a supervisor |
| To run a council from your phone, in a group, with other people | Telegram — `mooting telegram` |

---

## Using it

### SSH

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

**What SSH does not give you:** a session two people can watch at once, a phone,
or anything that survives the connection dropping mid-round.

> **Do not** put the board on OneDrive, Dropbox or an SMB share to "sync" it.
> SQLite's locking assumes a real filesystem; cloud-sync clients copy files out
> from under open handles, and the failure mode is a corrupt board, not an
> error. If two machines must reach one board, they need a process in front of
> it — see HTTP + events, below.

### Browser — `mooting serve --web`

```bash
mooting serve --web
```

Runs the real session in a browser through `textual-serve` — the transcript,
the seat panel, and the input that decides. Nothing is reimplemented.

Whoever reaches that port **is** the human sitting there: they can approve
plans, grant execute capability, and conclude meetings. `textual-serve` has no
authentication of its own, so bind to `127.0.0.1` and reach it through an SSH
tunnel, or put it behind a reverse proxy that authenticates. A non-loopback
bind is refused outright unless you also pass `--allow-remote`.

### HTTP + events — `mooting serve`

```bash
pip install 'mooting[serve]'
mooting serve
```

One process owns the board and the supervisor; a TUI, a web page, a phone, or
a script all talk to it the same way.

| | |
|---|---|
| `GET /topics`, `GET /topics/{slug}` | what exists, and its agenda |
| `POST /topics` | open one |
| `PATCH /topics/{slug}` | agenda, mode, manager, rounds, effort |
| `POST /topics/{slug}/messages` | say something, or answer a question |
| `POST /topics/{slug}/run` · `/stop` | drive the council |
| `POST /proposals/{id}/decide` | sign-off — people's seats only |
| `GET /topics/{slug}/minutes` | the written record |
| `GET /events?since=N` | the event stream, as Server-Sent Events; resumes from a cursor, misses nothing |

`mooting serve --grant <seat>` issues a bearer token for a human seat (never
send it as a query string — it ends up in logs). Every decision carries the seat
whose token made it.

### Telegram — `mooting telegram`

A council in a Telegram chat, with buttons for sign-off and the same commands
as the terminal session.

#### Setting it up

**1. Make a bot.** In Telegram, message **@BotFather** and send `/newbot`. It
asks for a display name, then a username ending in `bot`, and replies with a
token like `8123456789:AAF...`. Treat it as a password.

**2. Install, and seat a council.**

```bash
pip install 'mooting[telegram]'
cd your-project
mooting setup
```

**3. Start the bot.** The token is needed this once; it is remembered after
Telegram accepts it.

```bash
mooting telegram --token 8123456789:AAF...
```

Leave it running. It prints a one-time pairing code:

```
  menu    10 commands registered — type / in the chat to see them
  pair    send  /pair 9f2c1a  to the bot to claim the first seat
```

**4. Pair yourself.** Send that line to your bot:

```
/pair 9f2c1a
```

It replies with the seat you speak as and this chat's id.

**5. Lock it to that room.** Stop the bot, start it again with the id — no
token, it is remembered:

```bash
mooting telegram --chat -1001234567890
```

Without `--chat` the bot answers **anywhere it is added**, which hands that room
your subscriptions. The id is remembered too, so from now on it is just
`mooting telegram`.

**6. Hold a council.** Type `/` for the commands.

```
/topic new should we cap webhook retries?
/topic agenda cap the retries; full or partial jitter
/run
```

Plain messages post as you. `@Santa what does the gateway do today?` puts a
question to one seat and the others wait for the answer.

#### Once it is running

- A proposal arrives with **Approve / Reject** buttons. The reason is the reply
  it asks you for, and the button carries the proposal id, so a sign-off cannot
  land on the wrong one.
- That happens for proposals opened **while the bot is running** — the chat picks
  up from the board's head and never replays. `/proposals 7` fetches any proposal
  by number, buttons included, whenever it was opened.
- `/minutes` sends the write-up as a file; `/minutes decisions` returns the
  decisions as text; `/conclude` ends the meeting and delivers both.
- Send a file to the chat and it is attached to the topic. Text goes into every
  seat's next prompt.
- `/topic agenda`, `/rounds`, `/effort`, `/seats` — the same commands as the
  terminal session, because it is the same dispatch behind them.

#### Adding other people

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

---

The reasoning behind all of this — why SSE and not a websocket, why HTML and not
MarkdownV2, what was deliberately left unbuilt — is in
[Why it works this way](WHY.md#reaching-a-council-from-elsewhere).
