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
import subprocess
import re
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



#: PyPI will not render an SVG -- it serves as text and shows a broken image --
#: so the README needs a PNG. Keeping the two in one command is the point: the
#: PNG went stale once already, still showing the project's old name and the old
#: default effort long after both had changed, and nothing said so.
CHROME = [
    r"C:/Program Files/Google/Chrome/Application/chrome.exe",
    r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def write_png(svg: str, name: str = "session") -> pathlib.Path | None:
    """Rasterise the same SVG through headless Chrome, at 2x for sharp text."""
    browser = next((c for c in CHROME if pathlib.Path(c).exists()), None) or shutil.which("chromium")
    if not browser:
        return None

    m = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    if not m:
        return None
    w, h = (int(float(g)) + 1 for g in m.groups())

    tmp = pathlib.Path(tempfile.mkdtemp())
    page = tmp / "shot.html"
    page.write_text(
        "<html><head><meta charset='utf-8'><style>"
        "html,body{margin:0;padding:0;background:transparent}"
        f"svg{{display:block;width:{w}px;height:{h}px}}"
        "</style></head><body>" + svg + "</body></html>",
        encoding="utf-8")

    out = OUT / f"{name}.png"
    r = subprocess.run(
        [browser, "--headless", "--disable-gpu", "--hide-scrollbars",
         "--default-background-color=00000000", "--force-device-scale-factor=2",
         f"--window-size={w},{h}", f"--screenshot={out}", page.resolve().as_uri()],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    shutil.rmtree(tmp, ignore_errors=True)
    return out if out.exists() and r.returncode == 0 else None


def build_unanimous(path):
    """A board where every seat is in favour and it still has not passed.

    The one-screen proof. Agreement among agents is loud and a status field that
    does not change is quiet, which is the whole idea in one line: all of them
    said yes, and it waited for you anyway. Built from the real board rather than
    drawn, so it cannot claim something the code does not do.
    """
    b = connect(path, init=True)
    b.add_agent("you", "human")
    for name, kind in (("Santa", "claude"), ("Algae", "codex"), ("Gravity", "agy")):
        b.add_agent(name, kind, driver="spawn")
    tid = b.open_topic(
        "retries", "Should webhook retries use exponential backoff?",
        "The gateway retries on a fixed 30s schedule. Ops says that stampedes on "
        "recovery. Decide.",
        "you", seats=("Santa", "Algae", "Gravity", "you"), max_rounds=4)

    pid = b.propose(tid, "Santa", "Cap retries at 6 with partial jitter",
                    "Fixed 30s intervals stampede the gateway on recovery. Six "
                    "attempts with partial jitter bounds the worst case without "
                    "dropping deliveries that would have succeeded.")
    for seat, why in (
        ("Algae", "Agreed. Partial over full jitter is right when ordering matters."),
        ("Gravity", "Support. Six is the number the runbook already assumes."),
        ("Santa", "Mine, and I still think it is the smallest change that works."),
    ):
        b.vote(pid, seat, "support", why)

    # Every seat in favour, and the proposal is still a draft. Nothing here
    # decides it: there is no tool that could, and `Store.decide` would refuse.
    assert b.proposal(pid)["status"] == "open", "the point of the picture"
    b.close()
    return pid


async def shot_of(db, slug, typed, size=(118, 34)):
    """One screenshot of the real TUI against a real board."""
    app = MootApp(db, slug, "you")
    app.board.auto = False              # never spawn a real CLI for a picture
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.query_one("#say").value = typed
        # One pause is not enough: the key bar along the bottom fills in a frame
        # later, and a picture that caught it half-drawn shipped once already.
        for _ in range(4):
            await pilot.pause()
        svg = app.export_screenshot(title="mooting")
    app.board.store.close()
    app.drive_store.close()
    return svg


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
        for _ in range(4):
            await pilot.pause()
        svg = app.export_screenshot(title="mooting")
    app.board.store.close()
    app.drive_store.close()
    shutil.rmtree(tmp, ignore_errors=True)
    target = OUT / "session.svg"
    target.write_text(svg, encoding="utf-8")
    print(f"wrote {target}  ({len(svg):,} bytes)")
    png = write_png(svg)
    if png:
        print(f"wrote {png}  ({png.stat().st_size:,} bytes)")
    else:
        print("no Chrome found -- session.png not refreshed; it is now stale")

    # The second picture: unanimous, and still waiting on a person.
    tmp2 = tempfile.mkdtemp()
    db2 = pathlib.Path(tmp2) / "board.db"
    pid = build_unanimous(db2)
    svg2 = await shot_of(db2, "retries", f"/approve {pid} agreed")
    shutil.rmtree(tmp2, ignore_errors=True)
    signoff = OUT / "signoff.svg"
    signoff.write_text(svg2, encoding="utf-8")
    print(f"wrote {signoff}  ({len(svg2):,} bytes)")
    png2 = write_png(svg2, name="signoff")
    if png2:
        print(f"wrote {png2}  ({png2.stat().st_size:,} bytes)")


if __name__ == "__main__":
    asyncio.run(main())
