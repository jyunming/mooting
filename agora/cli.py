"""The human's surface.

Deliberately a separate surface from the MCP tools. Agents get `agora_say`,
`agora_propose`, `agora_vote`; the human gets those *plus* `approve`/`reject`,
which exist nowhere in the agent-facing tool list. Same board, different powers,
and the difference is structural rather than a matter of prompt discipline.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from .store import Store, StoreError, connect

DIM_, RESET_ = "\033[2m", "\033[0m"

# Windows consoles default to cp950 here; council traffic is Chinese from day one.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _board(args: argparse.Namespace) -> Store:
    return connect(Path(args.db) if args.db else None, init=getattr(args, "_init", False))


def _human(board: Store, name: str | None) -> str:
    """Resolve who 'you' are, and refuse to let a human command run as an agent."""
    who = name or os.environ.get("AGORA_HUMAN")
    if not who:
        humans = [a["name"] for a in board.agents() if a["kind"] == "human"]
        if len(humans) == 1:
            return humans[0]
        raise SystemExit("who are you? pass --as <name> or set AGORA_HUMAN")
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
    print("next: agora agents add claude claude --cwd .")
    return 0


def cmd_agents_add(args) -> int:
    board = _board(args)
    cfg = {"cwd": os.path.abspath(args.cwd)} if args.cwd else {}
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
        brief = sys.stdin.read()
    seats = [s.strip() for s in args.seats.split(",") if s.strip()]
    who = _human(board, args.as_)
    if who not in seats:
        seats.append(who)  # the human always holds a seat on their own topic
    tid = board.open_topic(args.slug, args.title, brief, who, seats=seats,
                           max_rounds=args.rounds, max_turns=args.turns, mode=args.mode,
                           effort=args.effort, manager=args.manager)
    print(f"topic #{tid} `{args.slug}` opened with seats: {', '.join(seats)}")
    print(f"next: agora run {args.slug}")
    return 0


def cmd_topic_rm(args) -> int:
    board = _board(args)
    t = board.topic(int(args.slug) if args.slug.isdigit() else args.slug)
    trees = board.orphan_worktrees(int(t["id"]))
    if not args.yes:
        print(f"would delete `{t['slug']}` — {t['title']}")
        print(f"  {len(board.transcript(int(t['id'])))} message(s), "
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
    for m in board.transcript(t["id"]):
        tag = "" if m["kind"] == "say" else f" [{m['kind']}]"
        print(f"#{m['id']} {m['author']}{tag}  {m['created_at']}")
        print(m["body"].strip() + "\n")
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
    print(f"next: agora nudge {t['slug']} {args.agent}    (or `agora run {t['slug']}`)")
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

    loose = board.proposals(tid, status="open") + list(board.open_mentions(tid))
    if loose and not args.force:
        print(f"`{t['slug']}` still has {len(loose)} loose end(s):")
        for p in board.proposals(tid, status="open"):
            print(f"  proposal #{p['id']} {p['title']}")
        for m in board.open_mentions(tid):
            print(f"  {m['asker']} asked {m['target']}: "
                  f"{' '.join(m['question'].split())[:70]}")
        print("rule on them first, or --force to close it as it stands")
        return 1

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
        print(f"topic is paused — `agora run {board.topic(int(p['topic_id']))['slug']}` to continue")
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
        print(f"topic is paused ({t['slug']}). Re-run with --resume once you have ruled.")
        return 1
    if args.resume:
        board.set_topic_status(int(t["id"]), "open", _human(board, args.as_), "resumed by human")
        if args.rounds:
            with board.tx() as c:
                c.execute("UPDATE topics SET max_rounds = max_rounds + ? WHERE id = ?",
                          (args.rounds, int(t["id"])))

    caps = Caps(max_turns_per_seat=args.max_turns, max_wakes_per_agent_per_hour=args.max_wakes,
                effort=args.effort or "medium")
    sup = Supervisor(board, _drivers(board), caps,
                     turn_taking="sequential" if args.sequential else "concurrent")
    reason = asyncio.run(sup.run_topic(int(t["id"])))
    print(f"\n== stopped: {reason}")
    for p in board.proposals(int(t["id"]), status="open"):
        print(f"   awaiting you: #{p['id']} {p['title']}")
        print(f"   agora approve {p['id']} -m '...'   |   agora reject {p['id']} -m '...'")
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


def cmd_console(args) -> int:
    """One terminal where every agent's reply lands and you can talk back."""
    from .console import run_console
    board = _board(args)
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
    """One screen: transcript, seats, tasks, and an input that talks and rules."""
    from .tui import run_tui
    board = _board(args)
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
    ap = argparse.ArgumentParser(prog="agora", description=__doc__.splitlines()[0])
    ap.add_argument("--db", help="board path (default ./.agora/board.db, or $AGORA_DB)")
    ap.add_argument("--as", dest="as_", help="act as this human seat (or $AGORA_HUMAN)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create a board and your human seat")
    p.add_argument("--human", default=os.environ.get("AGORA_HUMAN", "human"))
    p.set_defaults(fn=cmd_init)

    ag = sub.add_parser("agents", help="manage seats").add_subparsers(dest="sub", required=True)
    p = ag.add_parser("add")
    p.add_argument("name")
    p.add_argument("kind", choices=["claude", "codex", "copilot", "gemini", "agy", "human", "external"])
    p.add_argument("--cwd", help="repo the agent works in")
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

    p = sub.add_parser("install", help="register the MCP server with codex/gemini (one-time)")
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
                   help="skip the transcript; keep the rulings and the work log")
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
    p.add_argument("--max-turns", type=int, default=6, dest="max_turns")
    p.add_argument("--max-wakes", type=int, default=30, dest="max_wakes",
                   help="per agent per hour; a failed wake still counts")
    p.add_argument("--effort", choices=["low", "medium", "high"],
                   help="council-wide effort for this run (default medium)")
    p.add_argument("--sequential", action="store_true",
                   help="one seat at a time so each sees the last; slower by ~N x")
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

    p = sub.add_parser("console", help="live council view: watch replies and talk back")
    p.add_argument("topic", nargs="?", help="default: the most recent open topic")
    p.set_defaults(fn=cmd_console)

    p = sub.add_parser("tui", help="full-screen session: talk, watch the work, rule")
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
        print(f"agora: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
