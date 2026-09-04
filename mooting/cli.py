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

from . import __version__
from .store import (NotAuthorised, Store, StoreError, agenda_points,
                    agenda_text, connect, split_points)

DIM_, RESET_ = "\033[2m", "\033[0m"

# A console whose default codepage is not UTF-8 would garble non-ASCII output --
# and stdin was left out of this for a long time, which was worse. `-` input was
# decoded with the locale codec: bytes that map to cp1252 became mojibake stored
# without complaint, and bytes that do not became lone surrogates that failed at
# the SQLite write, three layers below the mistake. Reported as issue #2.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")


def read_stdin(what: str) -> str:
    """Text piped in for a `-` argument.

    Named rather than inlined so the four call sites cannot drift apart, and so
    the failure says which argument was being read.
    """
    try:
        return sys.stdin.read()
    except UnicodeDecodeError as exc:
        raise SystemExit(f"mooting: {what} is not valid UTF-8 ({exc})") from exc


def _board(args: argparse.Namespace) -> Store:
    return connect(Path(args.db) if args.db else None, init=getattr(args, "_init", False))


#: Teardown noise from CPython's Windows Proactor loop, and nothing else.
_BENIGN_TEARDOWN = ("I/O operation on closed pipe", "Event loop is closed")


def quiet_asyncio_teardown() -> None:
    """Stop a harmless GC traceback from landing on a full-screen session.

    When the loop closes with a subprocess transport still open -- leaving a
    session that was mid-turn does exactly that -- the transport's `__del__`
    runs with no loop, tries to build a ResourceWarning, and asks a closed pipe
    for its file descriptor. Python prints the traceback as "Exception ignored"
    and carries on. Nothing is lost: the CLI has already exited and its result
    is on the board. It just reads like a crash.

    Closing those transports earlier does not help. Measured: draining them at
    shutdown turned zero warnings into six, because closing a pipe under a
    pending `communicate()` causes the very thing it was meant to prevent. So
    this suppresses the *display*, and narrowly -- the raising object must be an
    asyncio transport and the message one of two known ones. Anything else,
    including a real error during teardown, still reaches the default hook.
    """
    previous = sys.unraisablehook

    def hook(unraisable):
        # `unraisable.object` is the `__del__` *function*, not the transport --
        # so the class name has to come from its qualname, e.g.
        # `_ProactorBasePipeTransport.__del__`.
        where = getattr(getattr(unraisable, "object", None), "__qualname__", "")
        if (where.endswith("__del__") and "Transport" in where
                and isinstance(unraisable.exc_value, (ValueError, RuntimeError))
                and any(b in str(unraisable.exc_value) for b in _BENIGN_TEARDOWN)):
            return
        previous(unraisable)

    sys.unraisablehook = hook


def _session_board(args: argparse.Namespace) -> Store:
    """Open the board for a session, creating it if this is the first time.

    Refusing to start because nobody has run `init` is a fine answer for a typo
    in `--db`, and a poor one for someone who has just installed this and typed
    the only command they know. An explicit path that does not exist still
    fails; the directory's own board is simply made.
    """
    explicit = bool(args.db or os.environ.get("MOOTING_DB"))
    board = connect(Path(args.db) if args.db else None, init=not explicit)
    if not [a for a in board.agents() if a["kind"] == "human"]:
        who = (getattr(args, "as_", None) or os.environ.get("MOOTING_HUMAN")
               or os.environ.get("USERNAME") or os.environ.get("USER") or "me")
        board.add_agent(who, "human", display=who)
        print(f"new board at {board.path}")
        print(f"you are `{who}` — /me <name> changes that")
    return board


def _human(board: Store, name: str | None) -> str:
    """Resolve who 'you' are, and refuse to let a human command run as an agent."""
    who = name or os.environ.get("MOOTING_HUMAN")
    if not who:
        humans = [a["name"] for a in board.agents() if a["kind"] == "human"]
        if len(humans) == 1:
            return humans[0]
        if not humans:
            raise SystemExit(
                "no person holds a seat on this board yet — `mooting setup` seats you.")
        # `--as` is a global option, so it belongs before the command. The old
        # message said to pass it and not where, and argparse answers
        # `mooting telegram --as Jeremy` with "unrecognized arguments", which
        # reads like the flag does not exist.
        #
        # Pairing a second person is what usually gets somebody here: a board
        # with one person answers this on its own, so the command that worked
        # yesterday stops the day a colleague joins.
        raise SystemExit(
            "who are you? more than one person holds a seat on this board:\n"
            f"  {', '.join(humans)}\n"
            "Say which, before the command:\n"
            f"  mooting --as {humans[0]} <command>\n"
            "or set MOOTING_HUMAN once for the whole session.")
    if not board.is_human(who):
        raise SystemExit(f"{who!r} is not a human seat; agent seats cannot use this command")
    return who


# ------------------------------------------------------------------- commands

def cmd_init(args) -> int:
    args._init = True
    board = _board(args)
    board.add_agent(args.human, "human", display=args.human)
    print(f"board at {board.path}")
    print(f"human seat: {args.human}")
    print("next: mooting agents add claude claude --cwd .")
    return 0


def cmd_setup(args) -> int:
    """One command that gets a council standing."""
    from .setup import run
    return run(args.db, assume_yes=args.yes)


def cmd_agents_add(args) -> int:
    board = _board(args)
    cfg = {"cwd": os.path.abspath(args.cwd)} if args.cwd else {}
    if args.repo:
        cfg["repo"] = os.path.abspath(args.repo)
    if args.model:
        cfg["model"] = args.model
    if args.effort:
        cfg["effort"] = args.effort
    if args.capability:
        cfg["capability"] = args.capability
    if args.arg:
        # Escape hatch for machine-local quirks -- a broken plugin to switch off,
        # a flag a newer CLI needs. Keeping these per-seat means the drivers stay
        # general instead of accumulating one user's environment.
        cfg["extra_argv"] = args.arg
    board.add_agent(args.name, args.kind, driver=args.driver, driver_cfg=cfg)
    print(f"seat {args.name} ({args.kind}, driver={board.agent(args.name)['driver']})")
    return 0


def cmd_agents_ls(args) -> int:
    board = _board(args)
    for a in board.agents():
        print(f"{a['name']:<12} {a['kind']:<10} driver={a['driver']:<11} "
              f"{'enabled' if a['enabled'] else 'disabled'}")
    return 0


def cmd_agents_rm(args) -> int:
    board = _board(args)
    a = board.agent(args.name)
    counts = {"seats": len(board.q("SELECT 1 FROM seats WHERE agent = ?", (args.name,))),
              "messages": len(board.q("SELECT 1 FROM messages WHERE author = ?", (args.name,)))}
    if not args.yes:
        print(f"would remove seat `{a['name']}` ({a['kind']})")
        print(f"  sits on {counts['seats']} topic(s); "
              f"{counts['messages']} message(s) it wrote stay on the board")
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
        brief = read_stdin("--brief")
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
            text = read_stdin("the agenda")
        added = split_points(text)
        points = added if replacing else [*points, *added]
        board.set_brief(tid, agenda_text(points), _human(board, args.as_))

    print(f"{topic['title'].strip()}  ({topic['slug']})")
    if points:
        for i, pt in enumerate(points, 1):
            print(f"  {i}. {pt}")
    else:
        print("  no agenda — the seats get the title alone")
    board.close()
    return 0


def cmd_topic_rm(args) -> int:
    board = _board(args)
    t = board.topic(int(args.slug) if args.slug.isdigit() else args.slug)
    trees = board.orphan_worktrees(int(t["id"]))
    if not args.yes:
        print(f"would delete `{t['slug']}` — {t['title']}")
        print(f"  {board.message_count(int(t['id']))} message(s), "
              f"{len(board.tasks(int(t['id'])))} task(s), "
              f"{len(board.proposals(int(t['id'])))} proposal(s)")
        for w in trees:
            print(f"  leaves a worktree on disk: {w}")
        print("re-run with --yes to actually delete it")
        return 1
    counts = board.delete_topic(int(t["id"]))
    print(f"deleted `{t['slug']}` ({counts['messages']} messages, "
          f"{counts['tasks']} tasks, {counts['proposals']} proposals)")
    for w in trees:
        print(f"  worktree left on disk: {w}")
        print(f"    git worktree remove {w}")
    return 0


def cmd_reset(args) -> int:
    """Clear the board. Seats survive unless you ask for everything."""
    board = _board(args)
    topics = board.topics()
    trees = [w for t in topics for w in board.orphan_worktrees(int(t["id"]))]
    if not args.yes:
        print(f"would delete {len(topics)} topic(s) and all their messages, "
              f"tasks and proposals:")
        for t in topics[:12]:
            print(f"  {t['slug']:<22} {t['title'][:44]}")
        if len(topics) > 12:
            print(f"  … and {len(topics) - 12} more")
        if args.all:
            print(f"and {len(board.agents())} registered seat(s)")
        for w in trees:
            print(f"  leaves a worktree on disk: {w}")
        print("re-run with --yes to actually do it")
        return 1
    n = board.clear_topics()
    print(f"cleared {n} topic(s)")
    if args.all:
        with board.tx() as c:
            c.execute("DELETE FROM agents WHERE kind != 'human'")
        print("removed every agent seat; human seats kept")
    for w in trees:
        print(f"  worktree left on disk: {w}  ->  git worktree remove {w}")
    return 0


def cmd_ls(args) -> int:
    board = _board(args)
    for t in board.topics(args.status):
        openp = len(board.proposals(t["id"], status="open"))
        flag = f"  ⟨{openp} awaiting you⟩" if openp else ""
        print(f"#{t['id']:<3} {t['slug']:<24} {t['status']:<9} "
              f"r{t['round'] + 1}/{t['max_rounds']}  {t['title']}{flag}")
    return 0


def cmd_show(args) -> int:
    board = _board(args)
    t = board.topic(int(args.topic) if args.topic.isdigit() else args.topic)
    print(f"# {t['title']}   (`{t['slug']}`, {t['status']}, round {t['round'] + 1}/{t['max_rounds']})\n")
    print(t["brief"] + "\n")
    for s in board.seats(t["id"]):
        print(f"  {s['agent']:<10} {s['kind']:<9} {s['state']:<8} {s['turns_used']}/{s['max_turns']}")
    print("\n" + "-" * 60 + "\n")
    for m in board.transcript(t["id"], limit=None):
        tag = "" if m["kind"] == "say" else f" [{m['kind']}]"
        print(f"#{m['id']} {m['author']}{tag}  {m['created_at']}")
        print(m["body"].strip() + "\n")
    return 0


def cmd_say(args) -> int:
    """A human joining the discussion -- not consuming an agent's metered turn."""
    board = _board(args)
    who = _human(board, args.as_)
    t = board.topic(int(args.topic) if args.topic.isdigit() else args.topic)
    body = read_stdin("the message") if args.body == "-" else args.body
    mid = board.post(int(t["id"]), who, body, count_turn=False)
    print(f"posted #{mid} as {who}")
    return 0


def cmd_ask(args) -> int:
    """@ one agent directly. The human's version of an @mention."""
    board = _board(args)
    who = _human(board, args.as_)
    t = board.topic(int(args.topic) if args.topic.isdigit() else args.topic)
    question = read_stdin("the question") if args.question == "-" else args.question
    mid = board.ask(int(t["id"]), who, args.agent, question)
    print(f"asked {args.agent} (#{mid}). They are next in line.")
    print(f"next: mooting nudge {t['slug']} {args.agent}    (or `mooting run {t['slug']}`)")
    return 0


def cmd_install(args) -> int:
    from .install import install_seat
    board = _board(args)
    names = [n.strip() for n in args.agents.split(",")] if args.agents else         [a["name"] for a in board.agents()]
    rc = 0
    for name in names:
        try:
            ok, detail = install_seat(board, name, dry_run=args.dry_run)
        except StoreError as exc:
            ok, detail = False, str(exc)
        print(f"[{'ok' if ok else 'FAIL'}] {name:<12} {detail}")
        rc |= 0 if ok else 1
    return rc


def cmd_minutes(args) -> int:
    """Write the meeting out as markdown."""
    from .minutes import default_path, render
    board = _board(args)
    t = board.topic(int(args.topic) if args.topic.isdigit() else args.topic)
    text = render(board, int(t["id"]), transcript=not args.decisions_only)
    if args.out == "-":
        print(text)
        return 0
    path = Path(args.out) if args.out else Path(default_path(board, int(t["id"])))
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path}  ({len(text.splitlines())} lines)")
    return 0


def cmd_conclude(args) -> int:
    """Close the meeting and write its minutes in one step."""
    from .minutes import default_path, render
    board = _board(args)
    who = _human(board, args.as_)
    t = board.topic(int(args.topic) if args.topic.isdigit() else args.topic)
    tid = int(t["id"])

    # Only an undecided proposal blocks: it is the chair's own outstanding duty.
    # A question one agent left hanging for another is recorded, not a veto.
    undecided = board.proposals(tid, status="open")
    if undecided and not args.force:
        print(f"`{t['slug']}` has {len(undecided)} decision(s) still waiting on you:")
        for p in undecided:
            print(f"  proposal #{p['id']} {p['title']}")
        print("sign off on them first, or --force to close with them unresolved")
        return 1
    for m in board.open_mentions(tid):
        print(f"left unanswered: {m['asker']} asked {m['target']} — "
              f"{' '.join(m['question'].split())[:70]}")

    board.conclude(tid, who, args.note or "")
    text = render(board, tid, transcript=not args.decisions_only)
    path = Path(args.out) if args.out else Path(default_path(board, tid))
    path.write_text(text, encoding="utf-8")
    print(f"concluded `{t['slug']}` and wrote {path}")
    return 0


def cmd_tasks(args) -> int:
    board = _board(args)
    t = board.topic(int(args.topic) if args.topic.isdigit() else args.topic)
    rows = board.tasks(int(t["id"]), status=args.status)
    if not rows:
        print("no tasks")
        return 0
    for r in rows:
        print(f"#{r['id']:<3} [{r['status']:<11}] {r['assignee']:<10} {r['title']}")
        if r["branch"]:
            print(f"      {DIM_}branch {r['branch']}  worktree {r['worktree']}{RESET_}")
        if r["result"]:
            print(f"      {r['result'].strip()[:300]}")
    return 0


def cmd_proposals(args) -> int:
    board = _board(args)
    tid = None
    if args.topic:
        tid = int(board.topic(int(args.topic) if args.topic.isdigit() else args.topic)["id"])
    for p in board.proposals(tid, status=args.status):
        votes = board.votes(p["id"])
        tally = ", ".join(f"{v['agent']}:{v['stance']}" for v in votes) or "no votes"
        print(f"#{p['id']} [{p['status']}] {p['title']}  — by {p['author']}  ({tally})")
        if args.full:
            print("   " + p["body"].strip().replace("\n", "\n   "))
            for v in votes:
                if v["rationale"]:
                    print(f"   · {v['agent']} {v['stance']}: {v['rationale']}")
    return 0


def _decide(args, approve: bool) -> int:
    board = _board(args)
    who = _human(board, args.as_)
    board.decide(args.proposal_id, who, approve, args.message or "")
    p = board.proposal(args.proposal_id)
    print(f"proposal #{p['id']} {p['status']} by {who}")
    if board.topic(int(p["topic_id"]))["status"] == "paused":
        print(f"topic is paused — `mooting run {board.topic(int(p['topic_id']))['slug']}` to continue")
    return 0


def cmd_approve(args) -> int:
    return _decide(args, True)


def cmd_reject(args) -> int:
    return _decide(args, False)


def _drivers(board: Store, names: list[str] | None = None):
    from .drivers.registry import build_drivers
    return build_drivers(board, names)


def cmd_run(args) -> int:
    from .supervisor import Caps, Supervisor
    board = _board(args)
    t = board.topic(int(args.topic) if args.topic.isdigit() else args.topic)
    if t["status"] == "paused" and not args.resume:
        print(f"topic is paused ({t['slug']}). Re-run with --resume once you have signed off.")
        return 1
    if args.resume:
        board.set_topic_status(int(t["id"]), "open", _human(board, args.as_), "resumed by human")
        if args.rounds:
            # `grant_rounds` exists for exactly this and its docstring names the
            # trap: raising rounds without the turns to use them leaves a council
            # that looks alive and says nothing. The raw UPDATE here raised one
            # of the two, and skipped the identity check as well, so
            # `--as <agent> --resume --rounds N` let a seat grant itself more.
            board.grant_rounds(int(t["id"]), args.rounds, _human(board, args.as_))

    # Whatever the seats actually hold, unless this run says lower.
    seated = [r["max_turns"] for r in board.seats(int(t["id"]))]
    ceiling = args.max_turns if args.max_turns is not None else (
        max(seated) if seated else Caps.max_turns_per_seat)
    caps = Caps(max_turns_per_seat=ceiling, max_wakes_per_agent_per_hour=args.max_wakes,
                effort=args.effort or "low")
    sup = Supervisor(board, _drivers(board), caps,
                     turn_taking="sequential" if args.sequential else "concurrent")
    reason = asyncio.run(sup.run_topic(int(t["id"])))
    print(f"\n== stopped: {reason}")
    for p in board.proposals(int(t["id"]), status="open"):
        print(f"   awaiting you: #{p['id']} {p['title']}")
        print(f"   mooting approve {p['id']} -m '...'   |   mooting reject {p['id']} -m '...'")
    return 0


def cmd_nudge(args) -> int:
    """Wake one seat by hand. The manual equivalent of everything the supervisor
    does -- which is the point: the board works without the daemon."""
    from .supervisor import Supervisor
    board = _board(args)
    t = board.topic(int(args.topic) if args.topic.isdigit() else args.topic)
    sup = Supervisor(board, _drivers(board, [args.agent]))
    result = asyncio.run(sup.wake_seat(int(t["id"]), args.agent))
    print(f"{args.agent}: {'ok' if result.ok else 'FAILED — ' + result.detail}")
    if result.tail and args.verbose:
        print(result.tail)
    return 0 if result.ok else 1


def cmd_prompt(args) -> int:
    """Print exactly what a seat would be told. Costs nothing; use it before
    spending a real turn on a prompt that turns out to be wrong."""
    from .supervisor import Supervisor
    board = _board(args)
    t = board.topic(int(args.topic) if args.topic.isdigit() else args.topic)
    text, cursor = Supervisor(board, {}).build_prompt(int(t["id"]), args.agent)
    print(text)
    print(f"\n--- would advance {args.agent} to cursor {cursor} on success ---")
    return 0


def cmd_telegram(args) -> int:
    """Run a council in a Telegram chat.

    The token is asked for once. After a successful start it lives on the board,
    so `mooting telegram` on its own works from then on -- a bot you have to
    re-authorise every time is a bot you stop using.
    """
    board = _session_board(args)
    if args.forget_token:
        board.set_setting("telegram.token", None)
        print("forgotten. `mooting telegram --token ...` to set it again.")
        board.close()
        return 0

    stored = board.setting("telegram.token")
    token = (args.token or os.environ.get("MOOTING_TELEGRAM_TOKEN")
             or os.environ.get("TELEGRAM_BOT_TOKEN") or stored)
    if not token:
        board.close()
        print("mooting: need a bot token, once.\n"
              "  mooting telegram --token <token from @BotFather>\n"
              "  Get one in Telegram: message @BotFather and send /newbot.\n"
              "  It is remembered afterwards, so you only pass it this time.",
              file=sys.stderr)
        return 1

    # Remembered only once Telegram has accepted it -- see `run`. Saving here
    # would remember a token that never worked.
    remember = token != stored and not args.no_save
    if stored and not args.token:
        print("  token   remembered from a previous run")

    who = _human(board, args.as_)
    chats = args.chat or [c for c in (board.setting("telegram.chats") or "").split(",") if c]
    if args.chat:
        board.set_setting("telegram.chats", ",".join(args.chat))
    board.close()

    try:
        from .telegram import run as run_bot
    except ImportError:
        print("mooting: the Telegram bot needs aiogram — "
              "pip install 'mooting[telegram]'", file=sys.stderr)
        return 1
    return run_bot(args.db, bot_token=token, chats=chats, human=who,
                   topic=args.topic, remember=remember)


def cmd_claim(args) -> int:
    """Print a code that proves whoever redeems it reached this machine.

    Every other way of saying who owns a board is something a stranger can
    produce: a name passed on the command line, being first to pair, or creating
    the Telegram group. Reading this terminal is not.
    """
    board = _board(args)
    who = args.seat or _human(board, args.as_)
    try:
        code = board.new_claim(who)
    except (StoreError, NotAuthorised) as exc:
        print(f"mooting: {exc}", file=sys.stderr)
        board.close()
        return 1
    minutes = int(board.CLAIM_TTL_S // 60)
    print(f"send this to the bot, from the account that should be `{who}`:")
    print(f"\n    /pair {code}\n")
    print(f"good for {minutes} minutes, once. It binds that chat account to the")
    print(f"seat and makes them the host of the room they send it in.")
    board.close()
    return 0


def cmd_pair(args) -> int:
    """Approve, deny or list chat identities from the shell."""
    board = _board(args)
    who = _human(board, args.as_)
    if args.approve:
        want = board.q1("SELECT * FROM pairings WHERE id = ?", (int(args.approve),))
        if want is None:
            print(f"no pairing request {args.approve}", file=sys.stderr)
            return 1
        # Same rule as in chat: their own display name is the name they answer
        # to, so approving them does not also mean inventing one.
        seat = args.seat or board.seat_name_for(want["display"],
                                                fallback=f"guest{args.approve}")
        row = board.pair_approve(int(args.approve), seat, who)
        # Approving from a terminal is how a room is bootstrapped, and it left
        # the room with no host at all -- so the first person to approve
        # somebody in the chat afterwards became one by accident.
        if row["channel"] == "telegram":
            board.claim_room(board.ensure_room("telegram", row["chat_id"]),
                             row["seat"])
        print(f"{row['display'] or row['user_id']} speaks as {row['seat']}")
    elif args.deny:
        board.pair_deny(int(args.deny), who)
        print(f"denied {args.deny}")
    rows = board.pairings()
    if not rows:
        print("no pairing requests yet")
    for r in rows:
        print(f"  #{r['id']} {r['status']:<9} {r['display'] or r['user_id']:<24}"
              f" chat={r['chat_id']}" + (f"  -> {r['seat']}" if r["seat"] else ""))
    board.close()
    return 0


def cmd_serve(args) -> int:
    """The board over HTTP, for clients that are not a terminal."""
    try:
        from .server import serve
    except ImportError:
        print("mooting: the server needs aiohttp — pip install 'mooting[serve]'",
              file=sys.stderr)
        return 1
    board = _board(args)
    who = _human(board, args.as_)
    if args.grant:
        # A per-seat token is an identity: it is who the caller *is* over a
        # socket, which is why only a human seat can hold one.
        try:
            token = board.grant_token(args.grant)
        except (StoreError, NotAuthorised) as exc:
            print(f"mooting: {exc}", file=sys.stderr)
            board.close()
            return 1
        print(f"token for {args.grant}:\n  {token}")
        print("  it may speak and sign off as that seat — treat it as a password")
        board.close()
        return 0
    if args.revoke:
        board.revoke_token(args.revoke)
        print(f"revoked the token for {args.revoke}")
        board.close()
        return 0
    holders = board.token_holders()
    board.close()
    if args.web:
        from .web import serve_web
        return serve_web(args.db, host=args.host, port=args.port, human=who,
                         topic=None, allow_remote=args.allow_remote)
    if holders:
        print(f"  seats   with a token: {', '.join(holders)}")
    return serve(args.db, host=args.host, port=args.port, token=args.token,
                 human=who, allow_remote=args.allow_remote)


def cmd_console(args) -> int:
    """One terminal where every agent's reply lands and you can talk back."""
    quiet_asyncio_teardown()
    from .console import run_console
    board = _session_board(args)
    who = _human(board, args.as_)
    topic = args.topic
    if not topic:
        live = [t for t in board.topics() if t["status"] in {"open", "paused"}
                and not t["slug"].startswith("doctor-")]
        # An empty board opens fine: /new works from inside.
        topic = live[0]["slug"] if live else None
    board.close()
    return run_console(args.db, topic, who)


def cmd_tui(args) -> int:
    """One screen: transcript, seats, tasks, and an input that talks and decides."""
    quiet_asyncio_teardown()
    from .tui import run_tui
    board = _session_board(args)
    who = _human(board, args.as_)
    topic = args.topic
    if not topic:
        live = [t for t in board.topics() if t["status"] in {"open", "paused"}
                and not t["slug"].startswith("doctor-")]
        # An empty board opens fine: /new works from inside.
        topic = live[0]["slug"] if live else None
    board.close()
    return run_tui(args.db, topic, who)


def cmd_watch(args) -> int:
    """Read-only tail, for a second terminal beside the one driving."""
    from .console import Console
    board = _board(args)
    who = _human(board, args.as_)
    board.close()
    c = Console(args.db, args.topic, who)
    try:
        c._poll()
    except KeyboardInterrupt:
        c.stop.set()
    return 0


def cmd_doctor(args) -> int:
    from .doctor import run_doctor
    return asyncio.run(run_doctor(_board(args), only=args.only, timeout=args.timeout))


# --------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="mooting", description=__doc__.splitlines()[0])
    ap.add_argument("--version", action="version", version=f"mooting {__version__}",
                    help="which version this is, for a bug report")
    ap.add_argument("--db", help="board path (default ./.mooting/board.db, or $MOOTING_DB)")
    ap.add_argument("--as", dest="as_", help="act as this human seat (or $MOOTING_HUMAN)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("setup", help="find your CLIs, seat them, wire them up, prove it works")
    p.add_argument("-y", "--yes", action="store_true",
                   help="take every default; seat every CLI found")
    p.set_defaults(fn=cmd_setup)

    p = sub.add_parser("init", help="create a board and your human seat")
    p.add_argument("--human", default=os.environ.get("MOOTING_HUMAN", "human"))
    p.set_defaults(fn=cmd_init)

    ag = sub.add_parser("agents", help="manage seats").add_subparsers(dest="sub", required=True)
    p = ag.add_parser("add")
    p.add_argument("name")
    p.add_argument("kind", choices=["claude", "codex", "copilot", "gemini", "agy", "human", "external"])
    p.add_argument("--cwd", help="where the seat runs while deliberating; keep it "
                                 "empty, a coding CLI reads what it finds there")
    p.add_argument("--repo", help="the repository this seat works in, for work "
                                  "topics; separate from --cwd on purpose")
    p.add_argument("--model")
    p.add_argument("--driver", choices=["stdio_json", "acp", "spawn", "none"])
    p.add_argument("--effort", choices=["low", "medium", "high"],
                   help="reasoning effort for this seat; the dominant latency knob")
    p.add_argument("--capability", choices=["deliberate", "execute"],
                   help="execute lets this seat edit files, but only for an approved "
                        "task on a work topic (default: deliberate)")
    p.add_argument("--arg", action="append",
                   help="extra argv passed to this CLI every wake (repeatable)")
    p.set_defaults(fn=cmd_agents_add)
    ag.add_parser("ls").set_defaults(fn=cmd_agents_ls)
    p = ag.add_parser("rm", help="remove a seat from the registry")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true", help="actually do it")
    p.set_defaults(fn=cmd_agents_rm)

    tp = sub.add_parser("topic", help="manage topics").add_subparsers(dest="sub", required=True)
    p = tp.add_parser("new")
    p.add_argument("slug")
    p.add_argument("--title", required=True)
    p.add_argument("--brief", required=True, help="the question; '-' reads stdin")
    p.add_argument("--seats", required=True, help="comma-separated agent names")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--turns", type=int, default=6, help="per-seat turn ceiling")
    p.add_argument("--effort", choices=["low", "medium", "high"],
                   help="override seat effort for this topic (low is ~9x faster)")
    p.add_argument("--mode", choices=["debate", "discuss", "work"], default="debate",
                   help="debate: find the flaw (default). discuss: build on each "
                        "other. work: a manager assigns tasks and the team does them")
    p.add_argument("--manager", help="work mode: the seat that plans and reviews")
    p.set_defaults(fn=cmd_topic_new)

    p = tp.add_parser("agenda", help="what this meeting is to settle")
    p.add_argument("slug")
    p.add_argument("points", nargs="*",
                   help="a point, or several separated by ';'. '-' reads stdin. "
                        "Omit to just show the agenda.")
    p.add_argument("--set", metavar="TEXT",
                   help="replace the whole agenda with this")
    p.add_argument("--clear", action="store_true", help="remove the agenda")
    p.set_defaults(fn=cmd_topic_agenda)

    p = tp.add_parser("rm", help="delete one topic and everything in it")
    p.add_argument("slug")
    p.add_argument("--yes", action="store_true", help="actually do it")
    p.set_defaults(fn=cmd_topic_rm)

    p = sub.add_parser("reset", help="clear every topic (seats are kept)")
    p.add_argument("--all", action="store_true",
                   help="also remove every agent seat, leaving a bare board")
    p.add_argument("--yes", action="store_true", help="actually do it")
    p.set_defaults(fn=cmd_reset)

    p = sub.add_parser("ls", help="list topics")
    p.add_argument("--status")
    p.set_defaults(fn=cmd_ls)

    p = sub.add_parser("show", help="print a topic's transcript")
    p.add_argument("topic")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("say", help="join the discussion yourself")
    p.add_argument("topic")
    p.add_argument("body", help="'-' reads stdin")
    p.set_defaults(fn=cmd_say)

    p = sub.add_parser("ask", help="@ one agent directly for its opinion")
    p.add_argument("topic")
    p.add_argument("agent")
    p.add_argument("question", help="'-' reads stdin")
    p.set_defaults(fn=cmd_ask)

    p = sub.add_parser("install", help="register the MCP server with codex/gemini/agy (one-time)")
    p.add_argument("agents", nargs="?", help="comma-separated; default all seats")
    p.add_argument("--dry-run", action="store_true", help="print the command instead of running it")
    p.set_defaults(fn=cmd_install)

    p = sub.add_parser("conclude", help="close a meeting and write its minutes")
    p.add_argument("topic")
    p.add_argument("note", nargs="?", help="your closing words, recorded as the conclusion")
    p.add_argument("-o", "--out")
    p.add_argument("--decisions-only", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="close it with proposals or questions still open")
    p.set_defaults(fn=cmd_conclude)

    p = sub.add_parser("minutes", help="write the meeting out as markdown")
    p.add_argument("topic")
    p.add_argument("-o", "--out", help="file to write ('-' for stdout)")
    p.add_argument("--decisions-only", action="store_true",
                   help="skip the transcript; keep the decisions and the work log")
    p.set_defaults(fn=cmd_minutes)

    p = sub.add_parser("tasks", help="the work plan and where each task has got to")
    p.add_argument("topic")
    p.add_argument("--status")
    p.set_defaults(fn=cmd_tasks)

    p = sub.add_parser("proposals", help="what is waiting on you")
    p.add_argument("topic", nargs="?")
    p.add_argument("--status", default="open")
    p.add_argument("--full", action="store_true")
    p.set_defaults(fn=cmd_proposals)

    for name, fn, helptext in (("approve", cmd_approve, "approve a proposal"),
                               ("reject", cmd_reject, "reject a proposal")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("proposal_id", type=int)
        p.add_argument("-m", "--message", help="your rationale, recorded on the board")
        p.set_defaults(fn=fn)

    p = sub.add_parser("run", help="drive the debate until it needs you")
    p.add_argument("topic")
    p.add_argument("--resume", action="store_true", help="unpause first")
    p.add_argument("--rounds", type=int, default=0, help="with --resume: grant N more rounds")
    # No default. This is a `min()` against the seat's own budget, so a number
    # here can only ever lower it -- and a default of 6 silently overrode a
    # budget the chair had deliberately granted, on a flag they never passed.
    p.add_argument("--max-turns", type=int, default=None, dest="max_turns",
                   help="lower the per-seat ceiling for this run; the seat's own "
                        "budget applies when this is not given")
    p.add_argument("--max-wakes", type=int, default=30, dest="max_wakes",
                   help="per agent per hour; a failed wake still counts")
    p.add_argument("--effort", choices=["low", "medium", "high"],
                   help="council-wide effort for this run (default low)")
    p.add_argument("--sequential", action="store_true",
                   help="one seat at a time so each sees the last, and one task "
                        "at a time on a work topic; slower by ~N x")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("nudge", help="wake one seat by hand")
    p.add_argument("topic")
    p.add_argument("agent")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_nudge)

    p = sub.add_parser("prompt", help="show what a seat would be told, without waking it")
    p.add_argument("topic")
    p.add_argument("agent")
    p.set_defaults(fn=cmd_prompt)

    p = sub.add_parser("attach", help="feed a file to a council")
    p.add_argument("topic")
    p.add_argument("files", nargs="*", help="omit to list what is attached")
    p.add_argument("--note", help="why it is attached; shown to every seat")
    p.add_argument("--rm", help="remove attachment by id")
    p.set_defaults(fn=cmd_attach)

    p = sub.add_parser("telegram", help="run a council in a Telegram chat")
    p.add_argument("--token", help="bot token from @BotFather; needed "
                                   "once, then remembered")
    p.add_argument("--chat", action="append",
                   help="allowlisted chat id; repeatable. Without one the bot "
                        "answers anywhere it is added")
    p.add_argument("--topic", help="topic slug the chat drives")
    p.add_argument("--no-save", action="store_true",
                   help="use the token once without remembering it")
    p.add_argument("--forget-token", action="store_true",
                   help="remove the saved token and exit")
    p.set_defaults(fn=cmd_telegram)

    p = sub.add_parser("claim", help="a one-time code that hands somebody a seat "
                                     "and hosts them in the room they use it in")
    p.add_argument("--seat", help="the human seat the code is for (default: you)")
    p.set_defaults(fn=cmd_claim)

    p = sub.add_parser("pair", help="who may act as which seat in a chat")
    p.add_argument("--approve", help="pairing id to approve")
    p.add_argument("--seat", help="the human seat they speak as")
    p.add_argument("--deny", help="pairing id to refuse")
    p.set_defaults(fn=cmd_pair)

    p = sub.add_parser("serve", help="serve the board over HTTP (read-only, B1)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4173)
    p.add_argument("--token", help="bearer token; generated if omitted")
    p.add_argument("--allow-remote", action="store_true",
                   help="bind a non-loopback address; only behind something "
                        "that authenticates")
    p.add_argument("--grant", metavar="SEAT",
                   help="issue a token that may speak and sign off as that seat")
    p.add_argument("--revoke", metavar="SEAT", help="withdraw that seat's token")
    p.add_argument("--web", action="store_true",
                   help="serve the full session in a browser (needs "
                        "textual-serve)")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("console", help="live council view: watch replies and talk back")
    p.add_argument("topic", nargs="?", help="default: the most recent open topic")
    p.set_defaults(fn=cmd_console)

    p = sub.add_parser("tui", help="full-screen session: talk, watch the work, sign off")
    p.add_argument("topic", nargs="?", help="default: the most recent open topic")
    p.set_defaults(fn=cmd_tui)

    p = sub.add_parser("watch", help="read-only live tail of a topic")
    p.add_argument("topic")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("doctor", help="smoke-test each CLI driver end to end")
    p.add_argument("--only", help="comma-separated agent names")
    p.add_argument("--timeout", type=float, default=180.0)
    p.set_defaults(fn=cmd_doctor)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except StoreError as exc:
        print(f"mooting: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
