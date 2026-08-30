"""Render a screenshot of the session from a real board.

Not a mockup. It builds a board, drives the actual TUI through Textual's test
pilot, and exports what the terminal would show — so the picture cannot drift
from the program the way a hand-drawn one does. Re-run it after a UI change:

    python -X utf8 tools/screenshot.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import shutil
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mooting.store import connect          # noqa: E402
from mooting.tui import MootApp            # noqa: E402

#: One copy, in docs/. The marketing page reaches it with ../docs/assets/ rather
#: than keeping its own -- two copies of a generated file drift the moment one is
#: regenerated and the other is not.
OUT = pathlib.Path(__file__).resolve().parent.parent / "docs" / "assets"


def build_board(path):
    b = connect(path, init=True)
    b.add_agent("you", "human")
    for name, kind in (("Santa", "claude"), ("Algae", "codex"), ("Gravity", "agy")):
        b.add_agent(name, kind, driver="spawn")
    tid = b.open_topic(
        "retries", "Should webhook retries use exponential backoff?",
        "The gateway retries on a fixed 30s schedule. Ops says that stampedes on "
        "recovery. Decide.",
        "you", seats=("Santa", "Algae", "Gravity", "you"), max_rounds=4)

    b.post(tid, "Santa",
           "Fixed interval is the whole problem. When a downstream comes back, "
           "every queued delivery fires in the same 30s window and knocks it over "
           "again.\n\n**Exponential with jitter**, capped at 6 attempts.")
    b.post(tid, "Algae",
           "Agreed on backoff, but the cap is the part that matters. Six attempts "
           "at 2^n is ~30 minutes of tail; our consumers treat anything over "
           "10 minutes as lost and re-send, so we would be duplicating.")
    b.post(tid, "Gravity",
           "@Santa jitter alone does not fix the stampede if every client uses the "
           "same base. It has to be **full** jitter, not equal jitter — otherwise "
           "you have just widened the spike, not flattened it.")
    b.post(tid, "you", "what does the gateway actually do today?", count_turn=False)
    b.propose(tid, "Santa", "Exponential backoff, full jitter, capped at 6",
              "Retry at `random(0, min(cap, base * 2^n))` with base 1s and cap 5m. "
              "Stop after 6 attempts and dead-letter.\n\nWithdraw if consumers "
              "cannot tolerate a 5-minute tail.")
    b.vote(1, "Algae", "object", "6 attempts exceeds the 10-minute consumer window")
    b.vote(1, "Gravity", "support", "full jitter is the right call")
    b.ask(tid, "Algae", "you", "What is the consumer's actual redelivery timeout?")
    b.close()
    return tid


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    # mkdtemp rather than the context manager: the app holds the board open, and
    # Windows refuses to unlink a file another handle has.
    tmp = tempfile.mkdtemp()
    db = pathlib.Path(tmp) / "board.db"
    build_board(db)
    app = MootApp(db, "retries", "you")
    app.board.auto = False              # never spawn a real CLI for a picture
    async with app.run_test(size=(118, 34)) as pilot:
        await pilot.pause()
        app.query_one("#say").value = "the gateway uses a fixed 30s, no cap"
        await pilot.pause()
        svg = app.export_screenshot(title="mooting")
    app.board.store.close()
    app.drive_store.close()
    shutil.rmtree(tmp, ignore_errors=True)
    target = OUT / "session.svg"
    target.write_text(svg, encoding="utf-8")
    print(f"wrote {target}  ({len(svg):,} bytes)")


if __name__ == "__main__":
    asyncio.run(main())
