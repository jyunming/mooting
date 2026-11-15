"""Driver interface: how the supervisor wakes one seat.

The key simplification, and the reason this interface is small:

    A driver does NOT carry the agent's reply back.

The agent posts to the board itself, through the Mooting MCP tools it was given.
The driver's only jobs are (a) deliver a prompt into the right CLI session,
(b) know when that turn is over, (c) capture the CLI's session identifier so the
next wake resumes the same conversation. Content never flows through here.

That matters because the four CLIs disagree about everything else. Their output
formats differ, their session semantics differ, and only two of them speak ACP
(see docs/DRIVERS.md). If the driver had to extract the reply, every adapter
would need an output parser that breaks on the next CLI release. It doesn't, so
they don't.

Consequence worth stating: a wake that fails is not a lost message. The seat's
`last_seen` cursor is untouched, so the agent catches up on whatever wakes it
next -- including a human running `mooting nudge`. The board is the substrate;
this whole module is an accelerator.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any


#: Names the vendors use for the same two numbers. Checked in order; the first
#: that is present wins, and anything unrecognised is ignored rather than guessed.
_TOKENS_IN = ("input_tokens", "prompt_tokens", "tokens_in", "inputTokens")
_TOKENS_OUT = ("output_tokens", "completion_tokens", "tokens_out", "outputTokens")
_COST = ("total_cost_usd", "cost_usd", "costUsd", "total_cost")

#: The server name in a config handed to a CLI per run. One seat, one server.
INJECTED_SERVER = "mooting"


def _kill_tree(pid: int) -> None:
    """Kill a process and everything it started, on Windows.

    Every vendor CLI there starts as a `.CMD` shim, so the process we hold is
    cmd.exe and the CLI is two levels under it. Killing the shim alone left the
    CLI running and spending quota after its turn had been given up. POSIX
    needs nothing here: the CLIs run directly, not through a shim.
    """
    if sys.platform != "win32":
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=10,
                       creationflags=subprocess.CREATE_NO_WINDOW)


def _first(blob: dict, keys) -> object | None:
    for key in keys:
        if isinstance(blob.get(key), (int, float)):
            return blob[key]
    return None


def usage_in(text: str) -> dict:
    """Tokens and cost from whatever JSON a CLI printed, or an empty dict.

    Scans rather than parsing one shape: `--output-format json` gives one object
    on claude, a stream of them elsewhere, and neither promises to keep its
    shape. Missing is missing -- a turn nobody costed is left unpriced instead of
    recorded as free.
    """
    import json as _json

    found: dict = {}
    for chunk in _candidates(text or ""):
        try:
            blob = _json.loads(chunk)
        except (ValueError, TypeError):
            continue
        if not isinstance(blob, dict):
            continue
        pools = [blob]
        if isinstance(blob.get("usage"), dict):
            pools.append(blob["usage"])
        for pool in pools:
            for name, keys in (("tokens_in", _TOKENS_IN), ("tokens_out", _TOKENS_OUT),
                               ("cost_usd", _COST)):
                if name not in found:
                    value = _first(pool, keys)
                    if value is not None:
                        found[name] = value
    return found


def _candidates(text: str):
    """The whole thing first, then line by line: one object or a stream."""
    body = text.strip()
    if body.startswith("{"):
        yield body
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("{"):
            yield line


@dataclass(frozen=True)
class Seat:
    """Everything a driver needs to reach one agent, flattened out of the DB."""
    topic_id: int
    topic_slug: str
    agent: str
    kind: str                       # claude|codex|copilot|gemini|...
    cli_session: str | None
    cfg: dict[str, Any] = field(default_factory=dict)
    #: Reasoning effort for this turn, resolved by the supervisor from the topic
    #: and the seat. This is the single biggest lever on wall-clock: measured on a
    #: real 10k-char council prompt, default effort took 279s and `low` took 31.8s
    #: -- 8.8x, against ~5s of process spawn and MCP handshake. Latency here is
    #: inference, not transport.
    effort: str | None = None
    #: True only when BOTH keys turned: the seat was registered with
    #: `--capability execute` AND it is being woken for an approved task on a work
    #: topic. Never inferred from one alone -- an execute-capable seat sitting on a
    #: meeting topic stays read-only.
    executing: bool = False
    #: Per-wake ceiling. A real task runs for many minutes; the deliberation
    #: default would kill legitimate work part-way through.
    timeout_s: float | None = None

    @property
    def cwd(self) -> str:
        """Where the CLI runs while it is deliberating.

        Not the repository. A coding CLI reads whatever its working directory
        means to it -- `CLAUDE.md`, `AGENTS.md`, its own per-directory memory --
        before it ever sees the prompt this project wrote, so a seat pointed at a
        working project brings that project's notes into an unrelated council.
        Found live: a council asked "how can I make money" came back with the
        person's age, city and profession, none of which was on the board.
        """
        return self.cfg.get("cwd") or os.getcwd()

    @property
    def repo(self) -> str | None:
        """The repository this seat works in, when it has been given one.

        Separate from `cwd` because the two want opposite things: deliberation
        wants a directory that says nothing, and work wants the project. One
        setting could not be both, and the shared one silently chose leaking.
        """
        return self.cfg.get("repo") or None

    @property
    def capability(self) -> str:
        """`deliberate` (default) or `execute`. Never inferred from the topic."""
        return self.cfg.get("capability", "deliberate")


@dataclass
class WakeResult:
    ok: bool
    cli_session: str | None = None
    detail: str = ""
    #: Raw tail of the CLI's output. Diagnostics only -- never parsed for content.
    tail: str = ""
    #: Tokens and cost the CLI reported for this turn. Empty when it said
    #: nothing, which is not the same as free.
    usage: dict = field(default_factory=dict)

    @classmethod
    def failure(cls, detail: str, tail: str = "") -> "WakeResult":
        return cls(ok=False, detail=detail, tail=tail[-2000:])


class Driver(abc.ABC):
    """One adapter per CLI transport style.

    Implementations must be safe to call concurrently for *different* seats.
    The supervisor serialises wakes per seat; it does not serialise across seats.
    """

    #: stdio_json | acp | spawn | none
    kind: str = "none"

    #: Windows caps a command line at 32,767 characters, and a prompt delivered
    #: as argv counts against it. Past that, CreateProcess raises
    #: FileNotFoundError [WinError 206] -- which reads as "the CLI is not
    #: installed" and sends you to debug entirely the wrong thing. Adapters that
    #: can take the prompt on stdin are exempt.
    max_argv_prompt: int = 24_000

    #: Wall-clock ceiling for one turn. A CLI that hangs must not hold a topic
    #: hostage -- on timeout the supervisor records the wake and moves on.
    timeout_s: float = 300.0
    #: How long to keep reading after the CLI exits. Output it wrote before
    #: exiting is already in the pipe; anything later belongs to a process it
    #: left behind, which is not the turn's to wait for.
    pipe_grace_s: float = 3.0

    #: True where the CLI is handed its server per run (`mooting_mcp_config`)
    #: rather than reading it from a global registration.
    injects_server: bool = False

    @classmethod
    def mcp_server(cls, agent: str) -> str:
        """The name this seat sees its Mooting server under, which the prompt repeats.

        A CLI handed its server per run sees exactly one, named `mooting`. A CLI
        that reads a global registration sees one per seat, `mooting-<seat>`, and
        must be told which is its own. The prompt used to name `mooting-<seat>`
        for everyone, and claude, finding no server by that name, wrote its
        answer as text rather than risk posting under another seat's name.
        """
        return INJECTED_SERVER if cls.injects_server else f"mooting-{agent}"

    def working_dir(self, seat: Seat) -> str:
        """Where this CLI actually runs. Overridden where cwd IS the containment."""
        return seat.cwd

    def usage_from(self, stdout: str, stderr: str) -> dict:
        """What the CLI said this turn cost, if it said anything.

        Read from the CLI's own JSON rather than counted here, because what
        matters is what came off the subscription and only the vendor knows that.
        Written to tolerate not being told: a CLI that reports nothing, or
        changes its shape, yields an empty dict rather than a wrong number.
        """
        return usage_in(stdout) or usage_in(stderr)

    def tool_profile(self, seat: Seat) -> list[str]:
        """Argv restricting what this seat may touch.

        One method per adapter rather than conditionals sprinkled through argv(),
        for the same reason effort_argv is one method: the blast-radius decision
        has to be reviewable in a single place. Deliberation seats get the
        narrowest surface the CLI offers; execute seats get enough to do the work
        and nothing more.
        """
        return []

    @abc.abstractmethod
    async def wake(self, seat: Seat, prompt: str) -> WakeResult:
        """Deliver `prompt` to `seat`'s session and return when the turn ends."""

    def failure_detail(self, code: int, out: str, err: str) -> str:
        """Why a non-zero exit happened, in words worth reading.

        stderr is not always where the reason is: agy reports errors as a JSON
        object on *stdout* and exits 1 with stderr empty, which surfaced as
        "agy exited 1: " -- an error message containing no error. Falling back to
        stdout costs nothing and is the difference between a diagnosis and a
        shrug.
        """
        reason = err.strip() or out.strip()
        return f"{self.binary} exited {code}: {reason[:300]}" if reason else \
            f"{self.binary} exited {code} with no output"

    async def close(self, seat: Seat) -> None:
        """Release any long-lived process held for this seat. Default: nothing."""
        return None

    # ---------------------------------------------------------------- utilities

    async def _run(
        self,
        argv: list[str],
        *,
        cwd: str,
        stdin: str | None = None,
        timeout: float | None = None,
    ) -> tuple[int, str, str]:
        """Run a CLI to completion with UTF-8 pinned in both directions.

        Decoding a UTF-8 subprocess pipe as the platform default produces
        garbled text that reads like protocol corruption, so the encoding is
        forced here rather than left to chance -- and `errors="replace"` keeps a
        stray byte from raising instead of degrading.
        """
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        # Without this, every wake flashes a console window on Windows -- and a
        # council of four seats running concurrently flashes four. The child's
        # stdio is piped either way, so it has no use for a console of its own;
        # the window was only ever an artefact of how Windows starts a console
        # program, most visibly through the .CMD shims.
        extra = {}
        if sys.platform == "win32":
            extra["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            **extra,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        payload = stdin.encode("utf-8") if stdin is not None else None
        out, err = bytearray(), bytearray()

        async def drain(stream, into: bytearray) -> None:
            while chunk := await stream.read(65536):
                into.extend(chunk)

        async def feed() -> None:
            if payload is None:
                return
            try:
                proc.stdin.write(payload)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass                       # it stopped reading; what it says still counts
            proc.stdin.close()

        async def exited() -> None:
            # Polled, not `proc.wait()`: on 3.11 that also waits for every pipe
            # to close, which is the very thing that can take forever.
            while proc.returncode is None:
                await asyncio.sleep(0.1)

        readers = [asyncio.ensure_future(drain(proc.stdout, out)),
                   asyncio.ensure_future(drain(proc.stderr, err))]
        turn = asyncio.gather(feed(), exited())
        # A cancelled turn leaves this gather holding a CancelledError that
        # nobody reads, and asyncio prints it as "exception was never retrieved"
        # with a traceback. In an unattended log that reads as a crash. Reading
        # it here, whenever it finishes, is all it takes.
        turn.add_done_callback(lambda f: f.cancelled() or f.exception())
        try:
            await asyncio.wait_for(turn, timeout=timeout or self.timeout_s)
            # The CLI has exited, but a process it started can still hold its
            # pipes. cursor-agent's self-updater did, and `communicate()` then
            # waited for the updater: a task done and reported at 52 s was
            # recorded as a 600 s timeout. What was written before the exit is
            # all there is to read, so drain briefly and stop.
            await asyncio.wait(readers, timeout=self.pipe_grace_s)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            for r in readers:
                r.cancel()
            # CancelledError matters as much as TimeoutError here. The supervisor
            # wraps this call in its own wait_for with the *same* deadline, and
            # its timer is armed first, so in practice the outer one always wins
            # and arrives as a cancellation. Catching only TimeoutError meant the
            # child was never killed: the council moved on and left a CLI running,
            # burning quota, for every timed-out turn.
            _kill_tree(proc.pid)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            # Bounded: on 3.11 `wait()` also waits for the pipes, and a process
            # the kill missed would hold this handler open in place of the turn.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=5)
            raise
        for r in readers:
            r.cancel()
        # Let go of pipes a leftover process still holds. There is no public
        # way to; left open, they outlive the event loop and warn when it closes.
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            transport.close()
        return (
            proc.returncode or 0,
            bytes(out).decode("utf-8", errors="replace"),
            bytes(err).decode("utf-8", errors="replace"),
        )
