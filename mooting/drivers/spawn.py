"""Spawn-per-turn adapters for every CLI a seat can run.

All of them are the same shape -- build argv, run to completion, let the agent post
through its own Mooting MCP tools -- and differ only in flags. So they share a base
and contribute an `argv()` each, rather than being hand-written subprocess dances
that drift apart.

## Session continuity is optional, and that is a feature

Claude, Codex and Copilot can resume a specific prior session by id. Gemini cannot:
its `--resume` takes `latest` or an index, not the UUID that `--session-id`
accepts, and "latest" races the moment one CLI holds seats on two topics.

The fix is not to fight it. **The board is the shared memory.** A stateless seat is
handed its catch-up excerpt in the prompt and reconstructs context from the record
every turn -- which cannot drift from what was actually said, unlike a long-lived
session. Continuity buys fewer input tokens, nothing else. `stateful=False` is a
supported mode, not a degraded one.

## Blast radius

v0 seats deliberate; they do not edit files. Each adapter therefore asks its CLI
for the narrowest tool surface it offers, and `--tool-policy` is where that per-CLI
decision lives so it is reviewable in one place instead of scattered through argv
builders. The flags differ in strength, and honestly: Claude's `--strict-mcp-config`
plus an allowlist is the tightest; Copilot's and Gemini's are weaker. `mooting doctor`
verifies the restriction empirically rather than trusting that a flag did what its
name suggests.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
from pathlib import Path

from .base import Driver, Seat, WakeResult


def _in_git_tree(path: Path) -> bool:
    """True if `path`, or the nearest existing directory above it, is inside a
    git work tree. Read from the file system, so no `git` binary is needed."""
    for p in (path, *path.parents):
        if (p / ".git").exists():
            return True
    return False

#: Emitted by `codex exec --json` and friends; also matches Claude's session id.
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def mooting_mcp_config(agent: str, db: Path | str) -> dict:
    """The MCP server block handed to a CLI so it can reach the board.

    Injected per-run where the CLI supports it (Claude, Copilot), so participating
    in a council never mutates that CLI's global config.
    """
    return {
        "mcpServers": {
            "mooting": {
                "command": sys.executable,
                "args": ["-X", "utf8", "-m", "mooting.mcp_server",
                         "--agent", agent, "--db", str(db)],
                "env": {"PYTHONUTF8": "1", "MOOTING_AGENT": agent, "MOOTING_DB": str(db)},
            }
        }
    }


class SpawnDriver(Driver):
    kind = "spawn"

    #: Can this CLI resume a session we name? See the module docstring.
    stateful: bool = False
    #: argv[0]; overridable for testing or for a non-PATH install.
    binary: str = ""

    #: Deliver the prompt on stdin instead of as an argv element.
    #:
    #: This is not a stylistic choice. Three of these CLIs install as Windows
    #: `.CMD` batch shims, and cmd.exe cannot carry an argument containing
    #: newlines -- the prompt breaks apart at the first line ending and every
    #: flag after it is swallowed. The visible symptom is not a parse error: it
    #: is codex reporting `approval: never` (because `--approve-for-me` never
    #: arrived) and refusing every MCP call, which reads exactly like a
    #: misconfigured server. Council prompts are always multi-line markdown, so
    #: any adapter whose CLI can read a prompt from stdin should.
    prompt_via_stdin: bool = False

    def __init__(self, db: Path | str, *, timeout_s: float = 300.0, extra_argv: list[str] | None = None):
        self.db = str(db)
        self.timeout_s = timeout_s
        self.extra_argv = extra_argv or []

    # -------------------------------------------------------------- per-CLI API

    def argv(self, seat: Seat, prompt: str, session: str | None) -> list[str]:
        raise NotImplementedError

    def new_session(self, seat: Seat) -> str | None:
        """Session id to use when there is no prior one. None = CLI assigns it."""
        return None

    def extract_session(self, stdout: str, stderr: str, proposed: str | None) -> str | None:
        if proposed:
            return proposed
        m = _UUID.search(stdout) or _UUID.search(stderr)
        return m.group(0) if m else None

    def effort_argv(self, seat: Seat) -> list[str]:
        """This CLI's way of saying "think this hard". Empty when it has no knob."""
        return ["--effort", seat.effort] if seat.effort else []

    def resolve_binary(self) -> str:
        """Full path to the executable, because a bare name is not enough on Windows.

        Three of these four CLIs install as npm/winget shims -- `codex.cmd`,
        `gemini.cmd`. `shutil.which` finds them via PATHEXT, but the Windows
        `CreateProcess` that `create_subprocess_exec` calls does not apply PATHEXT,
        so a bare "codex" raises FileNotFoundError while `codex --version` works
        fine in a shell. The failure reads as "CLI not installed" and is not.
        """
        return shutil.which(self.binary) or self.binary

    # ------------------------------------------------------------------- driving

    async def wake(self, seat: Seat, prompt: str) -> WakeResult:
        session = seat.cli_session if self.stateful else None
        proposed = session or (self.new_session(seat) if self.stateful else None)
        if not self.prompt_via_stdin and len(prompt) > self.max_argv_prompt:
            return WakeResult.failure(
                f"prompt is {len(prompt):,} chars; {self.binary} takes it as a "
                f"command-line argument and Windows caps that near 32k. Lower "
                f"Caps.max_catchup_chars, or trim the topic.")
        argv = self.argv(seat, prompt, proposed)
        argv[0] = self.resolve_binary()
        stdin = prompt if self.prompt_via_stdin else None

        try:
            code, out, err = await self._run(argv, cwd=self.working_dir(seat), stdin=stdin,
                                             timeout=seat.timeout_s or self.timeout_s)
        except asyncio.TimeoutError:
            return WakeResult.failure(
                f"{self.binary} exceeded {seat.timeout_s or self.timeout_s}s")
        except FileNotFoundError as exc:
            # WinError 206 is also a FileNotFoundError, and reporting it as "not on
            # PATH" is how a too-long prompt disguises itself as a missing CLI.
            if getattr(exc, "winerror", None) == 206:
                return WakeResult.failure(
                    f"command line too long for {self.binary} ({len(prompt):,} "
                    f"chars of prompt)")
            return WakeResult.failure(f"{self.binary} is not on PATH")

        tail = (out[-4000:] + ("\n[stderr]\n" + err[-2000:] if err.strip() else ""))
        if code != 0:
            return WakeResult.failure(self.failure_detail(code, out, err), tail)
        return WakeResult(
            ok=True,
            cli_session=self.extract_session(out, err, proposed) if self.stateful else None,
            tail=tail,
            usage=self.usage_from(out, err),
        )


# ------------------------------------------------------------------------ Claude

class ClaudeDriver(SpawnDriver):
    """Best-equipped of the four: resume by our own UUID, per-run MCP injection,
    and `--strict-mcp-config` to guarantee no other server is in scope."""
    binary = "claude"
    stateful = True

    def new_session(self, seat: Seat) -> str:
        import uuid
        return str(uuid.uuid4())

    #: What an execute seat may run in a shell. `acceptEdits` auto-approves file
    #: edits and nothing else, so `git commit` came back "requires approval" in a
    #: non-interactive run and the turn ended with the work sitting staged and
    #: uncommitted -- which the loop can only read as no work at all. Two live
    #: runs ended that way before the seat itself said why.
    #:
    #: Naming the verbs rather than widening the mode is what keeps the rest
    #: true: the prompt tells a worker not to merge, not to push and not to touch
    #: another branch, and this is what makes that a fact instead of a request.
    COMMIT_TOOLS = ("Bash(git add:*)", "Bash(git commit:*)", "Bash(git status:*)",
                    "Bash(git diff:*)", "Bash(git log:*)")

    def tool_profile(self, seat: Seat) -> list[str]:
        if seat.executing:
            # Editing is the job; the mooting tools stay available so the worker can
            # report back. `--strict-mcp-config` still keeps other servers out.
            return ["--permission-mode", "acceptEdits",
                    "--allowedTools", "mcp__mooting", *self.COMMIT_TOOLS]
        return ["--allowedTools", "mcp__mooting", "--permission-mode", "manual"]

    def argv(self, seat: Seat, prompt: str, session: str | None) -> list[str]:
        cfg = json.dumps(mooting_mcp_config(seat.agent, self.db), ensure_ascii=False)
        argv = [
            self.binary, "-p", prompt,
            "--mcp-config", cfg,
            "--strict-mcp-config",       # nothing but Mooting; no inherited servers
            *self.tool_profile(seat),
            "--output-format", "json",
            *self.effort_argv(seat),
        ]
        # Reuse the same session id across turns: first run creates it, later runs
        # resume it. Never --continue, which would race across topics.
        argv += ["--resume", session] if seat.cli_session else ["--session-id", session or ""]
        argv += self.extra_argv
        return [a for a in argv if a != ""]


# ------------------------------------------------------------------------- Codex

class CodexDriver(SpawnDriver):
    """The one adapter whose containment comes from *where* it runs, not a flag.

    Codex offers no way to auto-approve MCP calls while staying read-only, and it
    is not for want of looking -- all of this was measured, not assumed:

      * `--sandbox read-only` blocks writes, but then every MCP call is refused
        ("approval policy is never"), so the seat cannot reach the board at all.
      * `--approve-for-me` lets MCP through, but its own help says it reviews
        approvals *using the workspace-write sandbox* -- and it does: instructed
        to write a file, a seat wrote it.
      * The two are mutually exclusive at the flag level, `-c sandbox_mode=` does
        not override it (verified: the file was still written), and no per-server
        trust/approval key is exposed.

    So a `deliberate` codex seat runs with its cwd pointed at an empty scratch
    directory. Workspace-write is then real but has nothing to reach. The cost is
    honest and worth stating: **that seat cannot read the repo**, which a council
    convened over a codebase may well want. Grant `--capability execute` (only
    honoured on work topics) or set `--arg` deliberately if you would rather have
    the reads and accept the write access.
    """
    binary = "codex"
    prompt_via_stdin = True
    #: Stateless, and the two facts force each other. The prompt must arrive on
    #: stdin (multi-line markdown cannot survive the .CMD shim as argv), but
    #: `codex exec resume <id> <PROMPT>` will not accept `-` in its prompt
    #: position -- it demands a literal positional. Resume and stdin are therefore
    #: mutually exclusive, and stdin is the one that is not optional.
    #:
    #: No loss worth fighting for: the board is the shared memory, so a fresh
    #: session rebuilds context from the record every turn and cannot drift from
    #: it. See the module docstring.
    stateful = False

    def argv(self, seat: Seat, prompt: str, session: str | None) -> list[str]:
        # No `-c mcp_servers...` injection here, and two reasons why not, both
        # found by probing rather than by reading docs:
        #   1. `-c key=value` parses the value as TOML and silently falls back to a
        #      raw string on failure -- after un-escaping `\\`, so a Windows path
        #      turns `\d` into an invalid escape and the args array arrives as a
        #      string ("expected a sequence").
        #   2. Even with that fixed via forward slashes, a server introduced *only*
        #      by `-c` is not launched. It must exist in the config file.
        # Both failures look identical from outside: a clean exit that posts
        # nothing. So codex is registered once via `mooting install` instead.
        # --approve-for-me is what makes an MCP tool call actually execute. Without
        # it `codex exec` runs with approval policy "never", which does not mean
        # "auto-approve" -- it means *refuse*: the call is dispatched, the server
        # runs it, and codex reports "MCP tool call requires approval, but approval
        # policy is never". From outside that is indistinguishable from the server
        # not being loaded at all, which is the wrong thing to go and debug.
        # It is mutually exclusive with --sandbox, so the read-only posture for a
        # deliberation seat comes from the prompt and the tool set, not a flag.
        # No --json: it silently defeats --approve-for-me. With both flags the
        # approval policy reverts to "never" and every MCP call is refused again,
        # with the same misleading "requires approval" message. The session id is
        # printed in the plain-text header anyway, so --json bought nothing.
        # `-` means "read the prompt from stdin", which is what makes a multi-line
        # council prompt survive the .CMD shim. See SpawnDriver.prompt_via_stdin.
        # A deliberation seat runs in a scratch directory we create under the
        # board (see working_dir), and codex refuses to start outside a git repo:
        # "Not inside a trusted directory and --skip-git-repo-check was not
        # specified." This went unnoticed for a long time because the scratch dir
        # sits under .mooting/, so whenever mooting itself was run from inside a repo the
        # sandbox inherited that repo and codex was satisfied. Run mooting from a
        # folder that is not a repo -- a home directory, say -- and every codex
        # wake dies. The flag is scoped to that case on purpose: for an executing
        # seat the working directory is the user's own tree, and codex declining to
        # write to something unversioned is the correct answer, not a nuisance.
        sandboxed = [] if seat.executing else ["--skip-git-repo-check"]
        return [self.binary, "exec", "-", "--approve-for-me", *sandboxed,
                *self.effort_argv(seat), *self.extra_argv]

    def effort_argv(self, seat: Seat) -> list[str]:
        # `codex exec` has no --effort; the reasoning level is a config override.
        return ["-c", f'model_reasoning_effort="{seat.effort}"'] if seat.effort else []

    def working_dir(self, seat: Seat) -> str:
        """Empty scratch dir for deliberation; the real workspace only when the
        two-key rule has actually turned."""
        if seat.executing:
            return seat.cwd
        sandbox = Path(self.db).parent / "sandbox" / seat.agent
        sandbox.mkdir(parents=True, exist_ok=True)
        return str(sandbox)

    _SESSION_LINE = re.compile(r"session id:\s*([0-9a-f-]{16,})", re.I)

    def extract_session(self, stdout: str, stderr: str, proposed: str | None) -> str | None:
        m = self._SESSION_LINE.search(stdout) or self._SESSION_LINE.search(stderr)
        if m:
            return m.group(1)
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            for key in ("session_id", "conversation_id", "thread_id", "id"):
                val = obj.get(key) or (obj.get("msg") or {}).get(key)
                if isinstance(val, str) and _UUID.fullmatch(val):
                    return val
        return super().extract_session(stdout, stderr, proposed)


# ----------------------------------------------------------------------- Copilot

class CopilotDriver(SpawnDriver):
    """Resumes by id via `-r/--resume=<session-id>`.

    Not `--continue`, which means "most recent session" and is wrong the moment
    this CLI holds seats on two topics. `--resume=<id>` does not appear in the
    flag list under a `--continue` grep -- it is in the examples block -- and the
    id is printed on the run's own summary line, which is where it is captured.

    `--allow-all-tools` is mandatory for `-p`, so the surface is narrowed by
    disabling built-in servers and denying the dangerous tools instead of by an
    allowlist. That is weaker than Claude's `--strict-mcp-config`; `mooting doctor`
    is what confirms the seat can only reach the board.
    """
    binary = "copilot"
    stateful = True

    _RESUME = re.compile(r"--resume=([0-9a-f-]{16,})", re.I)

    def effort_argv(self, seat: Seat) -> list[str]:
        """Only when a model is named.

        Copilot's default model is `auto`, which rejects the flag outright --
        'Model "auto" does not support reasoning effort configuration' -- and
        fails the run. Setting a model on the seat is the user saying they know
        it takes one.
        """
        # `auto` named explicitly is the same model as no model, and rejects the
        # flag the same way. It is also a reasonable thing to ask for: it is
        # Copilot's own default and the cheap one on a metered plan -- 0.25
        # premium requests against 27 for a frontier model on one measured
        # prompt. An effort setting is a preference, so an unsupported one is
        # dropped rather than allowed to cost a seat its turn.
        if seat.effort and seat.cfg.get("model") not in (None, "", "auto"):
            return ["--effort", seat.effort]
        return []

    def tool_profile(self, seat: Seat) -> list[str]:
        return [] if seat.executing else ["--deny-tool", "shell", "--deny-tool", "write"]

    def argv(self, seat: Seat, prompt: str, session: str | None) -> list[str]:
        cfg = json.dumps(mooting_mcp_config(seat.agent, self.db), ensure_ascii=False)
        argv = [
            self.binary,
            "-p", prompt,
            "--additional-mcp-config", cfg,
            "--disable-builtin-mcps",     # no github-mcp-server in a debate seat
            "--allow-all-tools",          # required for non-interactive; narrowed below
            *self.tool_profile(seat),
            "--no-ask-user",              # nothing is watching; never block on a question
            "--no-color",
            *self.effort_argv(seat),
        ]
        if seat.cli_session:
            argv.append(f"--resume={seat.cli_session}")
        else:
            argv += ["-n", f"mooting-{seat.topic_slug}-{seat.agent}"]
        return argv + self.extra_argv

    def extract_session(self, stdout: str, stderr: str, proposed: str | None) -> str | None:
        m = self._RESUME.search(stdout) or self._RESUME.search(stderr)
        if m:
            return m.group(1)
        return super().extract_session(stdout, stderr, proposed)


# ------------------------------------------------------------------------ Gemini

class GeminiDriver(SpawnDriver):
    """Stateless: `--session-id` starts a *new* session with our UUID, and
    `--resume` wants `latest` or an index, so there is no resume-by-id to use.
    `--approval-mode plan` is the read-only mode -- the right posture for a seat
    that deliberates and must not edit files.

    Gemini has no per-run MCP injection, so `mooting doctor` checks that the server
    is registered (`gemini mcp add mooting ...`) instead of injecting it here."""
    binary = "gemini"
    stateful = False

    def effort_argv(self, seat: Seat) -> list[str]:
        return []          # no reasoning-effort knob on this CLI

    def tool_profile(self, seat: Seat) -> list[str]:
        return ["--approval-mode", "auto_edit" if seat.executing else "plan"]

    def argv(self, seat: Seat, prompt: str, session: str | None) -> list[str]:
        import uuid
        return [
            self.binary,
            "-p", prompt,
            "--session-id", str(uuid.uuid4()),
            *self.tool_profile(seat),
            "--allowed-mcp-server-names", f"mooting-{seat.agent}",
            "-o", "text",
            *self.extra_argv,
        ]


# --------------------------------------------------------------------- Antigravity

class AgyDriver(SpawnDriver):
    """Antigravity's CLI (`agy`). Resumes by id via `--conversation <ID>`.

    `--mode plan` is a real read-only mode, which makes it one of the two seats
    (with Gemini) that cannot edit files even if it decided to. Like Gemini it has
    no per-run MCP injection, so it is registered once by `mooting install`.
    """
    binary = "agy"
    #: Stateless by measurement, not by limitation. `--conversation <id>` resumes
    #: correctly, but a resumed council seat carries its whole history forward: a
    #: probe conversation reached 132k input tokens and one resumed turn took 800
    #: seconds, against 13 for a fresh one. Since the board already holds the
    #: shared memory, resuming buys nothing here and costs a timeout.
    stateful = False

    _CONV = re.compile(r'"conversation_id"\s*:\s*"([^"]+)"')

    #: Which efforts this CLI will actually accept. The flag advertises
    #: low|medium|high, but the *model* decides: gemini-3.1-pro refuses "medium"
    #: ("available: low, high") and the whole run fails. An effort setting is a
    #: preference, so an unsupported one is dropped rather than allowed to cost a
    #: seat its turn.
    efforts = frozenset({"low", "high"})

    def effort_argv(self, seat: Seat) -> list[str]:
        return ["--effort", seat.effort] if seat.effort in self.efforts else []

    def failure_detail(self, code: int, out: str, err: str) -> str:
        """agy reports errors as JSON on stdout and leaves stderr empty."""
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("error"):
                    return f"{self.binary}: {str(obj['error'])[:300]}"
        return super().failure_detail(code, out, err)

    def tool_profile(self, seat: Seat) -> list[str]:
        if seat.executing:
            # agy runs shell commands in its own scratch directory and ignores
            # the process working directory entirely -- asked where it was, it
            # answered `~\.gemini\antigravity-cli\brain\<uuid>\scratch` and
            # "fatal: not a git repository". So a worker could not see, let
            # alone commit to, the worktree it had been given. `--add-dir` is
            # what puts that directory in scope; with it the same seat commits.
            # `seat.cwd` is the worktree here: the loop rewrites it per task.
            return ["--mode", "accept-edits", "--add-dir", seat.cwd]
        return ["--mode", "plan"]

    def argv(self, seat: Seat, prompt: str, session: str | None) -> list[str]:
        # --mode plan is Antigravity's read-only mode: it cannot edit files, which
        # is the right posture for a seat that deliberates. It still calls MCP
        # tools, so the board stays reachable.
        return [self.binary, "-p", prompt, *self.tool_profile(seat),
                "--output-format", "json", *self.effort_argv(seat), *self.extra_argv]

    def extract_session(self, stdout: str, stderr: str, proposed: str | None) -> str | None:
        m = self._CONV.search(stdout) or self._CONV.search(stderr)
        if m:
            return m.group(1)
        return super().extract_session(stdout, stderr, proposed)


# ------------------------------------------------------------------------ Cursor

class CursorDriver(SpawnDriver):
    """Cursor's CLI (`cursor-agent`). Contained by a workspace we write, not a mode.

    Cursor has two read-only modes and neither can hold a seat. Both were tried
    on a real turn asked to post: `--mode plan` answered "I'll only inspect the
    mooting tool schema" and `--mode ask` answered "ask mode blocks mooting_say".
    Each exited 0 with nothing on the board.

    So the seat runs in agent mode inside a scratch workspace that holds two
    files and nothing else. `.cursor/mcp.json` hands it this seat's server, per
    run, with no global registration. `.cursor/cli.json` allows the mooting tools
    and denies every write and every shell command. Asked to post, write a file
    and run `echo`, a seat posted and reported the other two "blocked by
    permissions configuration", and neither file existed afterwards.

    The deny rules hold outside the workspace as well. Handed absolute paths to
    a file in another folder, a seat refused to overwrite it or create a file
    beside it. **Reading is not denied**: it read that file and posted the
    contents. So a cursor seat starts with no project in view and reads one
    only when it is given the path.

    A worker runs from a directory of ours too, with its worktree added as a
    second root, and may run git's bookkeeping commands and nothing else. Its
    file writes are not contained: see `WORK_PERMISSIONS`.
    """
    binary = "cursor-agent"
    #: The `.CMD` shim goes through cmd.exe and then PowerShell's `$args`, so a
    #: multi-line prompt cannot survive argv. `-p` with no prompt reads stdin.
    prompt_via_stdin = True
    #: `--resume <chatId>` exists, but the board is the shared memory and a
    #: fresh turn cannot drift from it. See the module docstring.
    stateful = False

    #: The mooting tools are allowed and every write and shell command denied.
    #: `--force` is never passed: its help says it allows any command not
    #: explicitly denied, which makes this list the only thing standing.
    PERMISSIONS = {"allow": ["Mcp(mooting:*)"], "deny": ["Write(**)", "Shell(*)"]}

    #: A worker edits, so the write deny goes, and commits, so git's bookkeeping
    #: verbs are named -- the same ones as `ClaudeDriver.COMMIT_TOOLS`. Every
    #: other command is refused, and a command line is refused whole if any part
    #: of it is unlisted: a live worker's `cd <tree> && git commit` came back
    #: "Rejected", the same commit with no `cd` went through, and
    #: `echo hi > shell.txt` and `git push` were both refused.
    #:
    #: **Writes are not contained.** Two live workers asked to overwrite a file
    #: outside the workspace both did it, with no approval asked. The rules here
    #: can only name paths to refuse, never "everything but the worktree", and a
    #: refusal is checked before an allowance. A cursor worker is held to git by
    #: the shell rules and to its worktree only by the prompt.
    WORK_PERMISSIONS = {
        "allow": ["Mcp(mooting:*)", "Shell(git add)", "Shell(git commit)",
                  "Shell(git status)", "Shell(git diff)", "Shell(git log)"],
        "deny": ["Shell(git push)"],
    }

    def effort_argv(self, seat: Seat) -> list[str]:
        # No --effort; reasoning is a bracket parameter on the model name,
        # e.g. `--model 'name[effort=high]'`, which belongs to the seat's model.
        return []

    def _base(self) -> Path:
        """Where this board's cursor directories go: beside it, unless that is
        inside a git work tree.

        cursor-agent takes the git root as its project when it runs inside
        one, and then does not load our `.cursor/mcp.json`. A live worker on a
        board kept in a repo's `.mooting/` was told "MCP server does not exist:
        mooting" and could not post. So such a board's seats run from a
        directory under `~/.mooting/cursor/` named for the board, and if that
        is in a work tree too, from the temp directory.
        """
        import hashlib
        import tempfile

        beside = Path(self.db).resolve().parent
        if not _in_git_tree(beside):
            return beside
        name = hashlib.sha1(str(beside).encode("utf-8")).hexdigest()[:12]
        for parent in (Path.home() / ".mooting" / "cursor", Path(tempfile.gettempdir()) / "mooting-cursor"):
            if not _in_git_tree(parent):
                return parent / name
        return beside           # nowhere outside a repo; `mooting doctor` will show a mute seat

    def working_dir(self, seat: Seat) -> str:
        # A worker gets a directory of its own. One seat can sit in a meeting
        # and work a task at once, and sharing a directory would let the task's
        # wake rewrite `cli.json` under the meeting's, putting a deliberating
        # seat under rules that let it edit. A sibling, not a child: cursor-agent
        # collects every `.cursor/cli.json` from the project root down to where
        # it runs, so a child would inherit its parent's.
        home = "worker" if seat.executing else "sandbox"
        sandbox = self._base() / home / seat.agent
        conf = sandbox / ".cursor"
        conf.mkdir(parents=True, exist_ok=True)
        # Rewritten every wake: the board path is in it, and a stale file from a
        # moved board would post somewhere else without complaint.
        (conf / "mcp.json").write_text(
            json.dumps(mooting_mcp_config(seat.agent, self.db), ensure_ascii=False),
            encoding="utf-8")
        perms = self.WORK_PERMISSIONS if seat.executing else self.PERMISSIONS
        (conf / "cli.json").write_text(
            json.dumps({"permissions": perms}), encoding="utf-8")
        # A worker runs here too, not in its worktree. Our config has to sit in
        # a workspace root, and the worktree is the user's tree, where a
        # `git add -A` would commit it.
        return str(sandbox)

    def argv(self, seat: Seat, prompt: str, session: str | None) -> list[str]:
        # --approve-mcps: `cursor-agent mcp list` reports a workspace server as
        # "not loaded (needs approval)" until something approves it. --trust:
        # a workspace trust prompt would wait for a person who is not there.
        # --disable-auto-update: a CLI that updates itself starts helper
        # processes that inherit our pipes and outlive it. One did, 30 s into a
        # live task, and the wake sat at the 600 s limit after the work was
        # done. The flag is hidden from `--help` (found in 2026.11.13's source),
        # so it may go away; `_run` no longer hangs on a held pipe either way.
        argv = [self.binary, "-p", "--approve-mcps", "--trust", "--disable-auto-update",
                "--output-format", "json"]
        if seat.executing:
            # The worktree joins as a second root, so it can be edited without
            # our config moving into it. The run stays in our directory, the
            # first root, whose `.cursor/mcp.json` is the one two live workers
            # loaded the board from.
            argv += ["--add-dir", seat.cwd]
        return argv + self.extra_argv


DRIVER_CLASSES = {
    "claude": ClaudeDriver,
    "codex": CodexDriver,
    "copilot": CopilotDriver,
    "gemini": GeminiDriver,
    "agy": AgyDriver,
    "cursor": CursorDriver,
}
