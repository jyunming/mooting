# ===== mooting/cli.py =====
"""The human's surface.

Deliberately a separate surface from the MCP tools. Agents get `mooting_say`,
`mooting_propose`, `mooting_vote`; the human gets those *plus* `approve`/`reject`,
which exist nowhere in the agent-facing tool list. Same board, different powers,
and the difference is structural rather than a matter of prompt discipline.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from .store import (NotAuthorised, Store, StoreError, agenda_points,
                    agenda_text, connect, split_points)

DIM_, RESET_ = "\033[2m", "\033[0m"

# A console whose default codepage is not UTF-8 would garble non-ASCII output.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _board(args: argparse.Namespace) -> Store:
    return connect(Path(args.db) if args.db else None, init=getattr(args, "_init", False))


#: Teardown noise from CPython's Windows Proactor loop, and nothing else.
_BENIGN_TEARDOWN = ("I/O operation on closed pipe", "Event loop is closed")


def quiet_asyncio_teardown() -> None:
    """Stop a harmless GC traceback from landing on a full-screen session.

    When the loop closes with a subprocess transport still open -- leaving a
    session that was mid-turn does exactly that -- the transport's `__del__`
# ...
        print("re-run with --yes")
        return 1
    got = board.delete_agent(args.name)
    print(f"removed `{args.name}` (was on {got['seats']} topic(s); "
          f"{got['messages']} message(s) kept)")
    return 0


def cmd_topic_new(args) -> int:
    board = _board(args)
    brief = args.brief
    if brief == "-":
        brief = sys.stdin.read()
    seats = [s.strip() for s in args.seats.split(",") if s.strip()]
    who = _human(board, args.as_)
    if who not in seats:
        seats.append(who)  # the human always holds a seat on their own topic
    tid = board.open_topic(args.slug, args.title, brief, who, seats=seats,
                           max_rounds=args.rounds, max_turns=args.turns, mode=args.mode,
                           effort=args.effort, manager=args.manager)
    print(f"topic #{tid} `{args.slug}` opened with seats: {', '.join(seats)}")
    print(f"next: mooting run {args.slug}")
    return 0


def cmd_attach(args) -> int:
    """Feed a file to a council."""
    board = _board(args)
    who = _human(board, args.as_)
    topic = board.topic(int(args.topic) if args.topic.isdigit() else args.topic)
    tid = int(topic["id"])
    if args.rm:
        print(f"removed {board.detach(int(args.rm), who)}")
    for f in args.files:
        aid = board.attach(tid, f, who, note=args.note or "")
        a = board.q1("SELECT * FROM attachments WHERE id = ?", (aid,))
        kind = "text, inlined into every prompt" if a["is_text"] else "binary, path only"
        print(f"#{a['id']} {a['name']}  ({a['bytes']:,} bytes — {kind})")
    rows = board.attachments(tid)
    if not args.files and not args.rm:
        if not rows:
            print(f"nothing attached to `{topic['slug']}`")
        for a in rows:
            mark = "text  " if a["is_text"] else "binary"
            print(f"  #{a['id']} {mark} {a['name']:<24} {a['bytes']:>9,}B"
                  + (f"  — {a['note']}" if a["note"] else ""))
    board.close()
    return 0


def cmd_topic_agenda(args) -> int:
    """Set a topic's agenda from the shell.

    The reason this exists is remote use: over SSH you could already open a
    topic and start the council, but the agenda -- the middle step, and the one
    that makes the difference between a question and a meeting -- was reachable
    only from inside a session.
    """
    board = _board(args)
    topic = board.topic(int(args.slug) if args.slug.isdigit() else args.slug)
    tid = int(topic["id"])
    points = agenda_points(topic)

    if args.clear:
        board.set_brief(tid, topic["title"], _human(board, args.as_))
        points = []
    elif args.set is not None or args.points:
        # `--set` takes its text directly. As a bare flag it was ambiguous with
        # the trailing points, and argparse resolved that by rejecting the line.
        replacing = args.set is not None
        text = args.set if replacing else " ".join(args.points)
        if text.strip() == "-":
            text = sys.stdin.read()
        added = split_points(text)
        points = added if replacing else [*points, *added]
        board.set_brief(tid, agenda_text(points), _human(board, args.as_))

    print(f"{topic['title'].strip()}  ({topic['slug']})")
    if points:
        for i, pt in enumerate(points, 1):
            print(f"  {i}. {pt}")
# ...
    return 0


def cmd_say(args) -> int:
    """A human joining the discussion -- not consuming an agent's metered turn."""
    board = _board(args)
    who = _human(board, args.as_)
    t = board.topic(int(args.topic) if args.topic.isdigit() else args.topic)
    body = sys.stdin.read() if args.body == "-" else args.body
    mid = board.post(int(t["id"]), who, body, count_turn=False)
    print(f"posted #{mid} as {who}")
    return 0


def cmd_ask(args) -> int:
    """@ one agent directly. The human's version of an @mention."""
    board = _board(args)
    who = _human(board, args.as_)
    t = board.topic(int(args.topic) if args.topic.isdigit() else args.topic)
    question = sys.stdin.read() if args.question == "-" else args.question
    mid = board.ask(int(t["id"]), who, args.agent, question)
    print(f"asked {args.agent} (#{mid}). They are next in line.")
    print(f"next: mooting nudge {t['slug']} {args.agent}    (or `mooting run {t['slug']}`)")
    return 0


# ...
