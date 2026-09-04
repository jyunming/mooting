"""`mooting setup` — one command that gets a council standing.

Setting this up by hand is six commands, and the order matters in ways nobody
should have to learn: a seat of certain kinds needs its MCP server registered
under its own name *before* it is woken, or it posts as somebody else.

Everything here has a plain command behind it (`mooting init`, `mooting agents add`,
`mooting install`, `mooting doctor`). The wizard runs them in the right order and says
what it is doing, so it stays possible to do by hand — and possible to see what
it did.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .drivers.spawn import DRIVER_CLASSES
from .install import NEEDS_REGISTRATION, install_seat
from .store import Store, StoreError, connect, default_db_path

#: Offered in the order most people would want them seated. Gemini's driver
#: works and `mooting agents add <name> gemini` still seats it -- it is left out
#: here because it is not being recommended yet, not because it is broken.
CANDIDATES = ("claude", "codex", "copilot", "agy")


def _found() -> list[tuple[str, str]]:
    """(kind, resolved path) for every candidate CLI actually on PATH."""
    out = []
    for kind in CANDIDATES:
        binary = DRIVER_CLASSES[kind].binary
        where = shutil.which(binary)
        if where:
            out.append((kind, where))
    return out


def _ask(prompt: str, default: str = "") -> str:
    """A question, or the default when nothing is typed. Never raises on EOF —
    a piped or non-interactive run should take the defaults, not crash."""
    try:
        answer = input(f"{prompt} ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return answer or default


def _yes(prompt: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    got = _ask(f"{prompt} {hint}", "y" if default else "n").lower()
    return got.startswith("y")


def run(db: Path | str | None, *, assume_yes: bool = False) -> int:
    print()
    print("  mooting setup")
    print("  ─────────────────────────────────────────────")
    print("  Everything below has a plain command behind it; this just runs them")
    print("  in an order that works.")
    print()

    # ---------------------------------------------------------------- 1. board
    # `default_db_path`, not a local path of its own: `mooting init`
    # centralises boards under the home directory, and setup answering
    # differently means the two commands disagree about where a board is.
    target = Path(db) if db else default_db_path()
    fresh = not target.exists()
    store: Store = connect(db, init=True)
    print(f"  board      {target}{'  (new)' if fresh else '  (existing)'}")

    humans = [a["name"] for a in store.agents() if a["kind"] == "human"]
    if humans:
        me = humans[0]
        print(f"  you        {me}")
    else:
        me = _ask("  What should the council call you?",
                  os.environ.get("USERNAME") or os.environ.get("USER") or "me")
        store.add_agent(me, "human", display=me)
        print(f"  you        {me}")
    print()

    # ---------------------------------------------------------------- 2. seats
    found = _found()
    if not found:
        print("  No agent CLIs found on PATH.")
        print("  Install at least one — claude, codex, copilot or agy — and run this again.")
        return 1

    print("  Found on your PATH:")
    for kind, where in found:
        print(f"    {kind:<9} {where}")
    print()

    already = {a["name"]: a for a in store.agents()}
    seated: list[str] = []
    cwd = str(Path.cwd())

    for kind, _where in found:
        if kind in already:
            print(f"  seat       {kind}  (already registered)")
            seated.append(kind)
            continue
        if not assume_yes and not _yes(f"  Seat {kind}?"):
            continue
        store.add_agent(kind, kind, driver_cfg={"cwd": cwd})
        seated.append(kind)
        print(f"  seat       {kind}  registered, working directory {cwd}")

    if not seated:
        print("\n  No seats registered; nothing to convene. Stopping here.")
        return 1
    print()

    # ------------------------------------------------------------ 3. MCP wiring
    # Order matters: a seat of these kinds posts under the wrong name until its
    # own server exists, so this has to happen before anything is woken.
    needing = [s for s in seated if store.agent(s)["kind"] in NEEDS_REGISTRATION]
    if needing:
        print("  Registering MCP servers (these CLIs cannot be handed one per run):")
        for name in needing:
            ok, detail = install_seat(store, name)
            mark = "ok  " if ok else "FAIL"
            print(f"    [{mark}] {name:<9} {detail}")
        print()

    # ---------------------------------------------------------------- 4. proof
    print("  Each seat now gets one real turn, to prove it can reach the board.")
    print("  This spends a little of each CLI's quota, and it is the only way to")
    print("  know: a CLI can load the server, decline to call it, and exit 0.")
    print()
    if assume_yes or _yes("  Run the check now?"):
        import asyncio

        from .doctor import run_doctor
        asyncio.run(run_doctor(store, only=",".join(seated)))
    else:
        print("  Skipped. Run `mooting doctor` when you want it.")
    print()

    # ----------------------------------------------------------- 5. first topic
    print("  ─────────────────────────────────────────────")
    if not store.topics():
        question = "" if assume_yes else _ask("  What would you like the council to discuss? (blank to skip)")
        if question:
            from .store import slugify
            slug = slugify(question, [t["slug"] for t in store.topics()])
            store.open_topic(slug, question, question, me, seats=[*seated, me])
            print(f"\n  Opened `{slug}`.")
            print(f"  Next:  mooting tui {slug}")
            store.close()
            return 0

    print("\n  Ready. Next:")
    print("     mooting tui                 open the session")
    print("     /topic new <question>       start a topic from inside it")
    print("     /help                       everything else")
    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin wrapper
    try:
        return run(None)
    except StoreError as exc:
        print(f"mooting: {exc}", file=sys.stderr)
        return 1
