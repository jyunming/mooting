"""Test-wide safety rails.

The suite is careful about not spawning real agent CLIs, but registration was a
hole in that: `/seats add reviewer codex` makes the console register an MCP
server for the new seat, and registration really does shell out to
`codex mcp add`. A TUI test therefore wrote a server into the user's *global*
codex config on every run -- found in the wild, pointing at a pytest temp
directory that had long since been deleted.

The fix belongs here rather than in the one test that tripped it, because any
future test that seats a codex-like agent would do the same thing.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def never_touch_global_cli_config(monkeypatch):
    """No test registers a real MCP server.

    A test that genuinely wants to exercise registration can monkeypatch
    `install_seat` itself; this only stops the accidental case, where the call
    is a side effect of seating an agent.
    """
    import mooting.install as install

    def refuse(store, agent, *a, **k):
        return True, f"[test] registration stubbed for {agent}"

    monkeypatch.setattr(install, "install_seat", refuse)
    yield
