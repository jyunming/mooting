"""`mooting serve --web` — the whole session, in a browser.

Milestone A of docs/REMOTE.md. `textual-serve` runs a Textual app on this
machine and renders it over a websocket, so the browser gets the real session:
the transcript, the seat panel, the input that rules on proposals. Nothing is
reimplemented, which is the entire appeal.

Two things it does not do, and both matter more than the serving:

**It has no authentication.** Whoever reaches the port gets a live session and
can approve plans, grant execute capability and conclude meetings, because
`Store.decide` is an identity check and there is no identity here at all. So
this binds loopback and refuses anything else unless told twice.

**It launches one app per connection.** Two viewers are two `mooting tui`
processes on one board. The board tolerates that -- WAL, one writer at a time --
but both can press Run, and the drive lock below is what stops two supervisors
waking every seat twice on one budget.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .store import Store, StoreError, connect

#: Loopback only, unless somebody says otherwise in as many words.
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})

def serve_web(db, *, host: str, port: int, human: str, topic: str | None,
              allow_remote: bool = False) -> int:  # pragma: no cover - a server
    try:
        from textual_serve.server import Server
    except ImportError:
        print("mooting: the browser session needs textual-serve — "
              "pip install 'mooting[web]'", file=sys.stderr)
        return 1

    if host not in LOOPBACK and not allow_remote:
        raise StoreError(
            f"refusing to bind {host}: this serves a live session, and whoever "
            f"reaches it can rule as `{human}`.\n"
            f"  Reach it over an SSH tunnel, or pass --allow-remote once it is "
            f"behind something that authenticates.")

    store = connect(db)
    board_path = store.path
    store.close()

    # The served app is a real `mooting tui`, told which board and who it is.
    argv = [sys.executable, "-X", "utf8", "-m", "mooting", "--db",
            str(board_path), "--as", human, "tui"]
    if topic:
        argv.append(topic)
    command = " ".join(_quote(a) for a in argv)

    print(f"  board   {board_path}")
    print(f"  you     {human}")
    print(f"  url     http://{host}:{port}")
    if host not in LOOPBACK:
        print("  WARNING: not loopback, and this has no login. Anyone who "
              "reaches this port is you.")
    else:
        print("  loopback only — tunnel with: ssh -L "
              f"{port}:127.0.0.1:{port} <this machine>")
    Server(command, host=host, port=port, title="mooting").serve()
    return 0


def _quote(arg: str) -> str:
    return f'"{arg}"' if " " in arg else arg
