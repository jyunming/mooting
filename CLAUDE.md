# CLAUDE.md — working on Mooting

Notes for an agent picking this repo up cold. `CONTRIBUTING.md` is for humans and
says how to add a driver or cut a release; this says what will bite you.

## What this is

Mooting sits several vendor coding-agent CLIs — Claude Code, Codex, Copilot,
Antigravity — at one table and lets them argue about your code, with a human
chairing. Each seat is spawned as an ordinary subprocess, once per turn, talking
to an MCP server Mooting controls over stdio. Agents post, object, ask and
propose by calling that server's tools. Every write lands in one local SQLite
file.

It never calls a model API and holds no key. That is not a slogan — it is why
the driver layer is subprocess-shaped and why there are no API clients anywhere.

## The four invariants

Break these and the project stops being what it is. They are enforced in code,
not in prompts, on purpose.

1. **Only a human decides.** There is no `mooting_decide` MCP tool — not
   disabled, absent, so it never appears in an agent's tool list. `Store.decide`
   rejects a non-human caller as a second line. If you add a surface (HTTP,
   chat, anything), sign-off must route through `Store.decide` and nothing else.
2. **Caps pause, they never silently continue.** Live debate spends real
   subscription quota with nobody watching. Per-seat turns, per-topic rounds and
   per-hour wakes all stop and ask a person. A *failed* wake counts too, because
   metered CLIs charge for it.
3. **The board is the substrate; the supervisor is an accelerator.** Everything
   the loop does is reachable by hand with `mooting nudge`. A failed wake
   degrades to catch-up-next-turn and never deadlocks a topic.
4. **Execution needs two independent keys.** A seat edits files only if
   registered `--capability execute` *and* woken for an approved task on a `work`
   topic. An execute-capable seat on a meeting topic stays read-only.

## Layout

| path | what lives there |
|---|---|
| `mooting/store.py` | the board. Schema, events, mentions, proposals, pairing, tokens. Everything is a method here. |
| `mooting/supervisor.py` | the round loop: who speaks next, why it stopped, task execution |
| `mooting/console.py` | the session and its command dispatch — **one** `handle()` for every surface |
| `mooting/tui.py` | full-screen view. Subclasses the console; changes where output lands, not what commands mean |
| `mooting/telegram.py` | a council in a chat. `ChatBoard` wraps the same `Console.handle` |
| `mooting/server.py` | the board over HTTP + SSE |
| `mooting/mcp_server.py` | the surface an agent sees. One process per seat, identity bound from argv |
| `mooting/drivers/` | one adapter per CLI, all the same shape; `fake.py` is what tests drive |
| `mooting/schema.sql` | table DDL. Column *additions* go in `Store.init_schema`'s migration list |
| `tools/build_site.py` | assembles the landing page + docs; fails on a dead link |

`store.py` and `console.py` are large and that is deliberate — the alternative
was the same command meaning different things in different surfaces.

## Traps found the hard way

Every one of these cost hours. None were caught by the test suite.

**Windows `.CMD` shims cannot carry a multi-line argument.** Every flag after it
is silently dropped. The symptom is a CLI insisting your MCP server needs
approval, not a quoting error. Prompts go on stdin.

**A CLI can start, load the MCP server, decline to call it, and exit 0.** A
return-code check goes green while the seat sits mute. `mooting doctor` asserts
on what reached the board instead. Never trust an exit code here.

**A daemon thread that raises takes the only notice with it.** `Console._nudge`
started a thread and reported nothing when it died, so the session said
"waking Santa…" and went quiet for ever. Any worker thread must catch and
`emit()`.

**A chat is not a terminal.** Telegram-HTML is assembled per line, so an italic
span opening in one paragraph and closing in another matches nothing and both
markers arrive as text. Long strings that read fine in a terminal must be
flattened and truncated at a word boundary before they go in a message.

**The event pump starts at `store.head()` and never replays.** Anything that
should be reachable after the fact needs an explicit command — that is why
`/proposals <id>` exists.

**`git` ignore rules did not survive the renames.** `.gitignore` listed
`.agora/`, `.moot/` and `.concord/` — every previous name — and not `.mooting/`,
so the live working board committed itself to a public repo. If this project is
ever renamed again, grep `.gitignore` first.

**A seat reads its working directory before it reads your prompt.** A coding
CLI loads `CLAUDE.md`, `AGENTS.md` or its own per-directory memory from wherever
it runs. Point a seat at a project and that project's notes join the council: a
board asked "how can I make money" answered with the chair's age, city and
profession, none of which was anywhere on the board. `cwd` is the seat's context
and should say nothing; `repo` is what work topics branch from. `mooting doctor`
reports a seat pointed somewhere with notes in it.

**Output was reconfigured to UTF-8 and input was not.** `cli.py` set stdout and
stderr and left `sys.stdin` on the locale codec, so piped UTF-8 came back through
cp1252: bytes with a mapping became mojibake stored without complaint, and bytes
without one became lone surrogates that failed at the SQLite write three layers
below the mistake. Both sides now, and `clean_text` refuses surrogates where
text enters the board rather than where it is finally written.

**Tests that chdir still write to the real home directory.** `default_db_path`
centralises boards under `~/.mooting/boards`, so a test that only monkeypatches
the cwd leaves a board behind every run. Patch `mooting.store.HOME_BOARDS` too.

**Heredocs in this environment mangle backslashes.** Writing Python that
contains `\n` or a regex escape through a shell heredoc corrupts it. Use the
Write tool or a script file, and always `python -X utf8`.

## Conventions

**Naming.** The reader is a manager who has been handed a capable team, not a
judge holding untrusted tools in line. Say *host*, *chair*, *sign off*, *the
call is yours*. Do not reintroduce *rule*, *ruling*, *gavel*, *fence* or
*inert* into anything user-facing. `kind == "ruling"` is a persisted value in
existing boards and is deliberately left alone — it is data, not prose.

**Prose.** Bolded benefit first, mechanism second, in flat sentences. No
aphorisms, no subordinate-clause openers, no double negatives. Comments explain
*why*, and preferably name the failure that motivated the code. A comment that
restates the line above it is noise.

**Docs.** `docs/WHY.md` is the one place detail is allowed to accumulate;
everything else stays instructions. Claims carry their conditions — "31.8 s a
turn" is one measurement on one prompt on one machine and says so.

**Commits.** Present tense, lower case, one line saying what changed and why it
mattered. The body is for the failure being fixed, not a diff summary.

**Answering the person you are working with.** Show it, then say one line about
it. A wall of prose describing behaviour is harder to check than the behaviour:

```
/team Santa Sam   →  team here: Santa, Sam — new meetings start with them
/topic new x      →  seats: Santa, Sam, Jeremy
```

Rules, in order of how much they matter:

1. **Lead with the example.** A command and its output, a before/after table, or
   the actual error text. Prose is the caption, not the substance.
2. **Answer the question that was asked, and stop.** Related work is one line at
   the end, or the next message.
3. **One screen.** If it does not fit, the parts that do not fit are a separate
   message the person can ask for.
4. **No section headings for three sentences.** They make a short answer look
   like a report.
5. **Say the number.** "30 wakes down to 6" beats "significantly fewer wakes".

## Working here

```bash
pip install -e ".[dev]"
python -X utf8 -m pytest -q        # 225 tests, ~35s
python -X utf8 tools/build_site.py # landing page + docs, fails on dead links
```

Tests never spawn a real CLI. `FakeDriver` posts to the board exactly as a real
seat does, so turn-taking, caps and the human gate are all exercised without
spending a token. `conftest.py` also stubs `install_seat`, because seating a
codex-like agent used to write into the developer's global CLI config.

Releasing is a tagged pipeline — see `CONTRIBUTING.md`. Push a `v*` tag whose
number matches `pyproject.toml` and the workflow does the rest. PyPI will not
let a version be replaced, so the first job refuses a mismatched tag before
anything uploads.

## What the suite does not cover

The Telegram surface has real tests and none of them send a Telegram message.
Five bugs in it were found by a person using it on a phone for twenty minutes,
all living in the gap between "the terminal tolerates this" and "a chat does
not". If you change that surface, drive it against a real chat before believing
it works.

The same holds for the drivers: `mooting doctor` spends two real turns per seat
on purpose — one that has to reach the board, one that is asked to sign off and
has to fail — because that is the only check that sees what the CLIs actually
do. It costs metered quota, so do not add a third turn without a reason as good.

## Open questions

- The repo's git *history* still contains a committed working board with real
  councils in it. Removing it means rewriting and force-pushing public history —
  a decision for the owner, not something to do unprompted.
- Pairing requests never expire. openclaw's expire after an hour.
- `mentions.asking` separates "named" from "asked" going forward, and migration
  backfills older rows by whether the body opens with `@target`. That heuristic
  is right for everything on the boards seen so far but it is a heuristic.
