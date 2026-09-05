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
for `-p` at all, so it is narrowed by denial instead. `mooting doctor` verifies
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

`mooting doctor` answers it for your machine rather than from this table: it
already spends real turns on each seat, and now says what they reported.
