"""A driver that costs nothing.

The point of building this first: live debate auto-triggers billed turns on four
subscription CLIs, and the parts most likely to be wrong -- turn-taking, cap
enforcement, the human-approval block -- have nothing to do with any real CLI.
Proving them here means the first real wake happens against a loop that already
works, instead of debugging both at once on someone's quota.

A FakeDriver seat behaves like a real one in the only way that matters: it posts
to the board through the store, exactly as a real agent posts through the MCP
tools. The supervisor cannot tell the difference, which is the test.
"""

from __future__ import annotations

import asyncio
from typing import Callable

from ..store import Store
from .base import Driver, Seat, WakeResult

#: (store, seat, prompt) -> reply text, or None to stay silent this turn.
Script = Callable[[Store, Seat, str], str | None]


def echo_script(marker: str = "noted") -> Script:
    def _script(store: Store, seat: Seat, prompt: str) -> str:
        return f"[{seat.agent}] {marker} (round prompt was {len(prompt)} chars)"
    return _script


class FakeDriver(Driver):
    kind = "fake"
    timeout_s = 5.0

    def __init__(
        self,
        store: Store,
        script: Script | None = None,
        *,
        latency_s: float = 0.0,
        fail_agents: set[str] | None = None,
    ) -> None:
        self.store = store
        self.script = script or echo_script()
        self.latency_s = latency_s
        #: Seats whose wake always fails, to exercise the degrade-to-catch-up path.
        self.fail_agents = fail_agents or set()
        self.calls: list[tuple[str, str]] = []

    async def wake(self, seat: Seat, prompt: str) -> WakeResult:
        self.calls.append((seat.agent, prompt))
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        if seat.agent in self.fail_agents:
            return WakeResult.failure("simulated wake failure")

        reply = self.script(self.store, seat, prompt)
        if reply is None:
            # A real agent may read the board and decide it has nothing to add.
            # That still ends a turn, and it still costs a request.
            return WakeResult(ok=True, cli_session=seat.cli_session or "fake-session", detail="silent")

        self.store.post(seat.topic_id, seat.agent, reply)
        return WakeResult(ok=True, cli_session=seat.cli_session or "fake-session")
