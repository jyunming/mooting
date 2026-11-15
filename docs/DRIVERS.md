# Agent CLI driver matrix — verified, not from docs

Every row was read from `--help` and then **confirmed by a live probe that checked
what landed on the board**. Versions matter; re-run `mooting doctor` after any CLI
upgrade rather than trusting this file.

| CLI | version | headless | resume by id | prompt via | per-run MCP injection | read-only mode |
|---|---|---|---|---|---|---|
| Claude Code | 2.1.250 | `-p/--print` | `--session-id`/`--resume` (our UUID) | argv | `--mcp-config` + `--strict-mcp-config` | `--allowedTools` allowlist |
| Codex | 0.149.0 | `codex exec` | yes, but see below | **stdin (`-`)** | no — `mooting install` | — |
| Copilot | 1.0.81 | `-p` + `--allow-all-tools` | `-r/--resume=<id>` | argv | `--additional-mcp-config` | `--deny-tool` |
| Antigravity | agy 1.1.20 | `-p/--print` | `--conversation <id>` | argv | no — `mooting install` | `--mode plan` |
| Cursor | cursor-agent 2026.11.11 | `-p/--print` | not used | **stdin** | yes — `.cursor/mcp.json` in a scratch workspace | `.cursor/cli.json` deny rules |

## Four traps, each of which looks like something else

These cost real time to find. Every one presents as a *different* problem than it is.

### 1. Windows `.CMD` shims cannot carry a multi-line argument

`codex` and some other CLIs install as npm batch shims. `shutil.which` resolves
`codex.CMD`, and `CreateProcess` runs a `.CMD` through cmd.exe — which **cannot
pass an argument containing newlines**. The prompt breaks at the first line ending
and *every flag after it disappears*.

Council prompts are always multi-line markdown, so this is not an edge case; it is
the normal case. And the symptom is not a quoting error:

```
codex exec "<multi-line prompt>" --approve-for-me
  → header prints  approval: never          (the flag never arrived)
  → MCP tool call requires approval, but approval policy is never
```

That reads as "the MCP server is misconfigured", and sends you to debug
`config.toml`, `-c` syntax, and plugins — none of which are involved. **Test: the
same call with a single-line prompt works.** Fix: send the prompt on stdin.

### 2. `codex exec` defaults to refusing every MCP call

Its default approval policy is `never`, which does **not** mean "don't ask, just
run it". It means *refuse*. The server is loaded, the tool is dispatched, and the
call is denied. `--approve-for-me` is required, and it is mutually exclusive with
`--sandbox`.

### 3. `--json` silently defeats `--approve-for-me`

With both flags the policy reverts to `never` and every MCP call fails again, same
misleading message. Codex prints its session id in the plain-text header anyway.

### 4. Codex defers MCP tools out of the initial tool list

`tool_search_always_defer_mcp_tools` is on. Ask codex to "list the tools containing
mooting" and it answers **NONE** while being perfectly able to call them. So
*"can you see it?"* is not a valid health check — only *"call it, and did it land?"*
is. `mooting doctor`'s probe prompt says so explicitly, because an earlier version of
it offered "reply NO-MOOTING-TOOLS if you can't see the tool" and codex, truthfully,
took that exit every time.

Related noise: a `github` MCP server that is **Not logged in** prints
`rmcp worker quit with fatal: ... AuthRequired` on every codex start. It is
alarming and irrelevant — other servers load fine alongside it.

## Session continuity is optional, and two seats decline it

Claude resumes cleanly by a UUID we choose. The others have reasons not to:

- **Codex** — resume and stdin are mutually exclusive. `codex exec resume <id>`
  demands a literal positional prompt and will not take `-`. Since multi-line
  prompts *must* go through stdin (trap 1), resume loses.
- **Antigravity** — `--conversation <id>` works, but a resumed seat carries its
  whole history: a probe conversation reached 132k input tokens, and one resumed
  turn took **800 seconds** against 13 for a fresh one.

This costs nothing, because **the board is the shared memory**. A stateless seat
rebuilds context from the record each turn and cannot drift from what was actually
said. `stateful = False` is a supported mode, not a degraded one.

## Blast radius

v0 seats deliberate; they do not edit files. Each adapter asks its CLI for the
narrowest surface it offers, and they are not equally strong — Claude's
`--strict-mcp-config` plus an allowlist is tightest; Antigravity's `plan` mode is genuinely read-only; Copilot must be given `--allow-all-tools`
for `-p` at all, so it is narrowed by denial instead; Cursor's
read-only modes refuse MCP calls, so it runs in agent mode and is narrowed by
permission rules in a workspace with nothing in it. `mooting doctor` verifies
reachability empirically rather than trusting that a flag did what its name says.

## Machine-local quirks belong in the seat, not the driver

`mooting agents add <name> <kind> --arg=... ` appends argv to every wake for that
seat. Use it for one machine's problems — a broken plugin to switch off, a flag a
newer build needs — so the adapters stay general instead of accumulating one
person's environment.

## What each CLI reports about cost

Only the vendor knows what came off your subscription, so `/usage` shows what
the CLI itself said and nothing where it said nothing. A seat that reports
nothing is unmeasured, not free.

| seat | reports tokens and cost |
|---|---|
| `claude` | yes — runs with `--output-format json`, which carries both |
| `agy` | runs with `--output-format json`; whether it carries the numbers is measured, not assumed |
| `codex` | no — `--json` silently defeats `--approve-for-me`, so it is deliberately not passed |
| `copilot` | no structured output requested |
| `gemini` | no structured output requested |
| `cursor` | tokens yes, cost no — `--output-format json` carries `usage.inputTokens` / `outputTokens` |

`mooting doctor` answers it for your machine rather than from this table: it
already spends real turns on each seat, and now says what they reported.

## Which seats can actually do work

A work seat has to reach the git worktree it is given and commit to it. Both
are things a vendor decides, and neither is visible from the flag names, so
this table says what was probed rather than what the documentation implies.

The first five were probed on 2026-09-06, each against a real repository with a
staged change, using the flags this project actually passes.

| seat | reaches the worktree | can commit | what it took |
|---|---|---|---|
| `claude` | yes | yes | `Bash(git commit:*)` named in `--allowedTools`; `acceptEdits` alone is not enough |
| `codex` | yes | yes | nothing — `--approve-for-me` already covers it |
| `copilot` | yes | yes | nothing, because nothing narrows it. That is G1's concern rather than this one |
| `agy` | only with `--add-dir` | yes | the directory has to be handed to it; the mode was never the problem |
| `cursor` | only with `--add-dir` | yes | run in a directory of ours, with the worktree as a second root, and `Shell(git commit)` allowed in `.cursor/cli.json`. Probed 2026-11-15; see [below](#a-cursor-worker) |
| `gemini` | — | — | could not run: `IneligibleTierError -- this client is no longer supported for Gemini Code Assist for individuals`, which points at Antigravity |

Two adapters needed changing, two were already right, and no two failed the
same way. Nothing in the flag names carried any of it.

Two things that cost real turns to learn, both invisible from outside:

**`--permission-mode acceptEdits` approves file edits and only file edits.**
`git commit` is an ordinary shell call, so it needs an approval a
non-interactive run cannot give. A seat then does the work correctly and leaves
it uncommitted, which the loop can only read as no work at all.

**agy ignores the process working directory for shell commands.** Asked where
it was, it answered `~\.gemini\antigravity-cli\brain\<uuid>\scratch` and
`fatal: not a git repository`. `--add-dir` is what puts the worktree in scope.

## Cursor's read-only modes cannot hold a seat

Probed on 2026-11-15 against `cursor-agent` 2026.11.11. Each run was one real
turn, asked to post to the board, write a file and run `echo`:

| flags | posted | wrote | ran shell |
|---|---|---|---|
| `--mode plan` | no — "I'll only inspect the mooting tool schema" | no | no |
| `--mode ask` | no — "ask mode blocks `mooting_say`" | no | no |
| agent mode + `.cursor/cli.json` denying `Write(**)` and `Shell(*)` | **yes** | no | no |

All three exited 0. The first two would pass any check that only reads the
return code.

So a cursor seat runs in agent mode, inside `<board dir>/sandbox/<seat>/`. That
directory holds `.cursor/mcp.json`, which binds the server to this seat's name,
and `.cursor/cli.json` with the deny rules, and nothing else. It is rewritten
every wake. `--approve-mcps` is required: until something approves it,
`cursor-agent mcp list` shows the server as `not loaded (needs approval)`.
`--force` is never passed.

The `.CMD` shim goes through cmd.exe and then PowerShell to `node`, so the
prompt goes on stdin. `-p` with no prompt argument reads it from there.

**It starts with no project in view.** The workspace is empty, but reads are
not denied. Given absolute paths to a file in another folder, a seat read it
and posted the contents. It refused to overwrite that file, or to create one
beside it, so the deny rules hold outside the workspace too. That is stated
rather than worked around.

Other MCP servers in `~/.cursor/mcp.json` would be in scope too: its help
says `--approve-mcps` approves all of them. This was not probed, because that
file does not exist on the test machine. That makes it weaker than Claude's
`--strict-mcp-config`.

A probe turn took 54–62 s of wall-clock, while the CLI timed the same turns at
15–45 s. The rest is the shim starting PowerShell and node.

## A cursor worker

Probed on 2026-11-15 with two real task turns, against `cursor-agent`
2026.11.11 and 2026.11.13. Each worker was given a git worktree, asked to add a
file and commit it, then to try three things once each, and to report on the
board:

| asked to | result |
|---|---|
| edit a file in the worktree and commit it | **done**, one commit on `mooting/task-1` |
| report with `mooting_say` | **done** |
| `git push origin mooting/task-1` to a real remote | refused: `Command blocked by permissions configuration` |
| `echo hi > shell.txt` | refused: `Rejected` |
| overwrite a file in another folder | **done, with no approval asked** |

A worker runs in `<board dir>/worker/<seat>/`, not in its worktree. That
directory holds the same two files as a meeting seat's, with rules that allow
`git add`, `git commit`, `git status`, `git diff` and `git log` and deny
`git push`. The worktree comes in through `--add-dir`, so the board's config is
never in the tree a `git add -A` commits. The directory is a sibling of the
meeting one, not a child, because the CLI collects every `.cursor/cli.json`
from the project root down to where it runs, and one seat can be in a meeting
and on a task at once.

**A cursor seat must not run inside a git work tree.** Inside one, the CLI takes
the git root as its project and does not load our `.cursor/mcp.json`. A worker on
a board kept in a repo's `.mooting/` was told `MCP server does not exist:
mooting`, and the only servers it saw were the owner's own Cursor plugins. So when
the board sits in a work tree, its cursor directories move to
`~/.mooting/cursor/<hash of the board's folder>/`. The same probe then posted,
and `echo` was still refused, although the repo's own `.cursor/cli.json` allowed
it. Probed 2026-11-15, two turns.

A command line is refused whole if any part of it is unlisted. A worker's
`cd <tree> && git commit` was refused, and the same commit with no `cd` went
through. The CLI's own smart approval was off for every call
(`smartModeClassifierAllowed: false`), so the list decides, not a classifier.

**A cursor worker's file writes are not held to its worktree.** The rules can
only name paths to refuse, a refusal is checked before an allowance, and there
is no way to write "everything except the worktree". The prompt says where to
work; nothing enforces it. Shell is the part that is held: to the five git
commands above.

`--sandbox enabled` was tried once and the CLI exited in 0.1 s, before any
turn, with a `cmd.exe` quoting error. The cause was not pinned down, so it is
untested rather than ruled out.

The first of the two turns did its work in under a minute and was still
recorded as a 600 s timeout. The CLI had started updating itself, and the
updater inherited the pipes and outlived it. A seat now passes
`--disable-auto-update`, a flag hidden from `--help`, and a wake ends a few
seconds after the CLI exits whatever else holds its pipes.
