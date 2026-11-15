# ===== mooting/drivers/base.py =====
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
import os
import subprocess
import sys
from dataclasses import dataclass, field
# ...
    #: Windows caps a command line at 32,767 characters, and a prompt delivered
    #: as argv counts against it. Past that, CreateProcess raises
    #: FileNotFoundError [WinError 206] -- which reads as "the CLI is not
    #: installed" and sends you to debug entirely the wrong thing. Adapters that
    #: can take the prompt on stdin are exempt.
    max_argv_prompt: int = 24_000

    #: Wall-clock ceiling for one turn. A CLI that hangs must not hold a topic
    #: hostage -- on timeout the supervisor records the wake and moves on.
    timeout_s: float = 300.0

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
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(payload), timeout=timeout or self.timeout_s
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # CancelledError matters as much as TimeoutError here. The supervisor
            # wraps this call in its own wait_for with the *same* deadline, and
            # its timer is armed first, so in practice the outer one always wins
            # and arrives as a cancellation. Catching only TimeoutError meant the
            # child was never killed: the council moved on and left a CLI running,
            # burning quota, for every timed-out turn.
            proc.kill()
            await proc.wait()
            raise
        return (
            proc.returncode or 0,
            out.decode("utf-8", errors="replace"),
            err.decode("utf-8", errors="replace"),
        )
