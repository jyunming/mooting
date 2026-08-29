# Agent CLI driver matrix — verified, not from docs

Every row was read from `--help` and then **confirmed by a live probe that checked
what landed on the board**. Versions matter; re-run `moot doctor` after any CLI
upgrade rather than trusting this file.

| CLI | version | headless | resume by id | prompt via | per-run MCP injection | read-only mode |
|---|---|---|---|---|---|---|
| Claude Code | 2.1.250 | `-p/--print` | `--session-id`/`--resume` (our UUID) | argv | `--mcp-config` + `--strict-mcp-config` | `--allowedTools` allowlist |
| Codex | 0.149.0 | `codex exec` | yes, but see below | **stdin (`-`)** | no — `moot install` | — |
| Copilot | 1.0.81 | `-p` + `--allow-all-tools` | `-r/--resume=<id>` | argv | `--additional-mcp-config` | `--deny-tool` |
| Gemini | 0.54.4 | `-p` | no (index/`latest` only) | argv | no — `moot install` | `--approval-mode plan` |
| Antigravity | agy 1.1.20 | `-p/--print` | `--conversation <id>` | argv | no — `moot install` | `--mode plan` |

## Four traps, each of which looks like something else

These cost real time to find. Every one presents as a *different* problem than it is.

### 1. Windows `.CMD` shims cannot carry a multi-line argument

`codex` and `gemini` install as npm batch shims. `shutil.which` resolves
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
moot" and it answers **NONE** while being perfectly able to call them. So
*"can you see it?"* is not a valid health check — only *"call it, and did it land?"*
is. `moot doctor`'s probe prompt says so explicitly, because an earlier version of
it offered "reply NO-MOOT-TOOLS if you can't see the tool" and codex, truthfully,
took that exit every time.

Related noise: a `github` MCP server that is **Not logged in** prints
`rmcp worker quit with fatal: ... AuthRequired` on every codex start. It is
alarming and irrelevant — other servers load fine alongside it.

## Session continuity is optional, and two seats decline it

Claude resumes cleanly by a UUID we choose. The others each have a reason not to:

- **Codex** — resume and stdin are mutually exclusive. `codex exec resume <id>`
  demands a literal positional prompt and will not take `-`. Since multi-line
  prompts *must* go through stdin (trap 1), resume loses.
- **Antigravity** — `--conversation <id>` works, but a resumed seat carries its
  whole history: a probe conversation reached 132k input tokens, and one resumed
  turn took **800 seconds** against 13 for a fresh one.
- **Gemini** — `--resume` takes `latest` or an index, not the UUID `--session-id`
  accepts, and "latest" races when one CLI holds two seats.

This costs nothing, because **the board is the shared memory**. A stateless seat
rebuilds context from the record each turn and cannot drift from what was actually
said. `stateful = False` is a supported mode, not a degraded one.

## Blast radius

v0 seats deliberate; they do not edit files. Each adapter asks its CLI for the
narrowest surface it offers, and they are not equally strong — Claude's
`--strict-mcp-config` plus an allowlist is tightest; Gemini's and Antigravity's
`plan` modes are genuinely read-only; Copilot must be given `--allow-all-tools`
for `-p` at all, so it is narrowed by denial instead. `moot doctor` verifies
reachability empirically rather than trusting that a flag did what its name says.

## Machine-local quirks belong in the seat, not the driver

`moot agents add <name> <kind> --arg=... ` appends argv to every wake for that
seat. Use it for one machine's problems — a broken plugin to switch off, a flag a
newer build needs — so the adapters stay general instead of accumulating one
person's environment.
