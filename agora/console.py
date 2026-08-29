"""`agora console` -- one terminal where the whole council is visible, and where
you answer when it asks you something.

## Why this is not just a log tail

A council that can @ you is useless if answering is awkward. Two things make it
work, and both are load-bearing rather than polish:

**Printing must not mangle what you are typing.** A background poller writing to
stdout while you compose an answer destroys the line you are on. `prompt_toolkit`'s
`patch_stdout` puts arriving messages *above* a stable prompt, so you can be
half-way through a sentence when three agents reply and lose nothing. Without it,
"reply interactively" is a lie. (There is a plain `input()` fallback if
prompt_toolkit is missing; it works, but it interleaves badly.)

**A question addressed to you must be impossible to miss.** Agents @ you when they
have hit something only you know -- where a file lives, which of two readings you
meant. That arrives in the same stream as everything else, so it is rendered
differently, counted in the toolbar, and repeated when the council stops.

## What answering does

Typing plain text posts as you and **discharges every question outstanding against
you** -- because answering in prose is how people actually reply, and requiring a
special "answer" gesture would leave stale asks forever. Your interjections never
spend an agent's metered turn.

## Two gestures that are deliberately not chat

`/approve` and `/reject` close a proposal. That is the one power agents do not
have, and it should not look like just another message. `/effort` retunes how hard
the whole council thinks -- the brainstorming dial: go wide and cheap, then think
deep on the branch worth it.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from .store import MENTION_RE, Store, StoreError, connect, slugify

#: CLIs a seat can be created against from inside the session.
AGENT_KINDS = frozenset({"claude", "codex", "copilot", "gemini", "agy"})

try:  # optional, but the difference between usable and not
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.patch_stdout import patch_stdout
    HAVE_PTK = True
except ImportError:  # pragma: no cover - fallback path
    HAVE_PTK = False

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
CYAN, YELLOW, GREEN, RED, MAGENTA = "\033[36m", "\033[33m", "\033[32m", "\033[31m", "\033[35m"

COMMANDS = {
    "/run": "start the council; replies stream in below",
    "/stop": "stop driving after the current turn",
    "/effort": "low | medium | high -- how hard the whole council thinks",
    "/asks": "questions waiting on you",
    "/auto": "on | off -- whether posting wakes the council (default on)",
    "/nudge": "wake one seat by hand",
    "/approve": "<id> <why> -- rule on a proposal (only you can)",
    "/reject": "<id> <why>",
    "/proposals": "what is waiting on you; /proposals <id> for the whole thing",
    "/show": "<id> -- a message in full, however far back it scrolled",
    "/tasks": "the work plan and where each task has got to",
    "/quote": "reply to the last message; or /quote <seat> | <id>",
    "/me": "<name> -- what the council calls you",
    "/minutes": "write the meeting out; /minutes decisions for the rulings only",
    "/conclude": "<your closing words> -- close the meeting and write the minutes",
    "/reopen": "resume a meeting you concluded",
    "/rounds": "<n> -- grant the council more rounds on this topic",
    "/seats": "who is here; /seats add <agent> | /seats rm <agent>",
    "/topic": "<slug> -- switch to another topic",
    "/new": "<what you want to discuss> -- opens a topic, same seats",
    "/mode": "debate | discuss | work <agent> -- what kind of topic this is",
    "/manager": "<agent> -- reassign the manager (work topics only)",
    "/rm": "<slug> -- delete a topic; /rm yes for this one",
    "/reset": "clear every topic; add `yes` to confirm",
    "/help": "this list",
    "/quit": "leave (the board keeps everything)",
}

BANNER = f"""{BOLD}agora console{RESET}  --  everything the council says, in one place

  {CYAN}<text>{RESET}              post as yourself, and answer anything asked of you
  {CYAN}@agent <question>{RESET}   ask one councillor directly (jumps the queue)
  {CYAN}/run{RESET}  {CYAN}/effort{RESET}  {CYAN}/asks{RESET}  {CYAN}/approve{RESET}  {CYAN}/seats{RESET}  {CYAN}/help{RESET}  {CYAN}/quit{RESET}
"""


def _ask_banner(asker: str, question: str) -> str:
    return (f"\n{MAGENTA}{BOLD}❓ {asker} is asking you{RESET}\n"
            f"{MAGENTA}{question.strip()}{RESET}\n"
            f"{DIM}   just type your answer — it posts as you and clears the question{RESET}\n")


def _fmt_event(store: Store, ev, me: str) -> str | None:
    """One board event as a line worth reading. None = not worth showing."""
    if ev.kind == "message":
        row = store.q1("SELECT * FROM messages WHERE id = ?", (ev.payload.get("message_id"),))
        if row is None:
            return None
        author, kind, body = row["author"], row["kind"], row["body"].strip()
        if author == me and kind != "ruling":
            return None                      # you just typed it; do not echo it back

        mentions = ev.payload.get("mentions") or []
        if me in mentions:
            # A question addressed to you is not just another message in the feed.
            return _ask_banner(author, body)

        colour = {"system": DIM, "ruling": GREEN, "object": YELLOW}.get(kind, CYAN)
        tag = "" if kind == "say" else f" {DIM}[{kind}]{RESET}"
        at = f"  {YELLOW}→ @{', @'.join(mentions)}{RESET}" if mentions else ""
        return f"\n{colour}{BOLD}{author}{RESET}{tag}{at}\n{body}\n"

    if ev.kind == "proposal" and ev.payload.get("action") == "opened":
        pid = ev.payload["proposal_id"]
        return (f"\n{YELLOW}{BOLD}◆ proposal #{pid}{RESET} {YELLOW}{ev.payload['title']}{RESET}"
                f"\n{DIM}  by {ev.actor} — waiting on you: /approve {pid}  |  /reject {pid}{RESET}\n")

    if ev.kind == "decision":
        return (f"\n{GREEN}✓ proposal #{ev.payload['proposal_id']} "
                f"{ev.payload['status']} by {ev.actor}{RESET}\n")

    if ev.kind == "topic" and ev.payload.get("action") == "paused":
        return f"\n{DIM}— paused: {ev.payload.get('note', '')}{RESET}\n"
    return None


class _ConsoleCompleter:
    """Completes `/commands` and `@seatnames`. Seats are re-read each time so a
    council that gains a member mid-session completes it."""

    def __init__(self, console: "Console") -> None:
        self.console = console

    def get_completions(self, document, complete_event):  # pragma: no cover - UI
        text = document.text_before_cursor
        word = text.split()[-1] if text.split() and not text.endswith(" ") else ""
        if text.startswith("/") and " " not in text.rstrip():
            for cmd, why in COMMANDS.items():
                if cmd.startswith(text):
                    yield Completion(cmd, start_position=-len(text), display_meta=why)
        elif word.startswith("@"):
            for s in self.console.seat_names():
                if s.startswith(word[1:]):
                    yield Completion("@" + s, start_position=-len(word))


class Console:
    def __init__(self, db: Path | str | None, topic_ref: str | None, me: str):
        self.db = db
        self.store = connect(db)
        #: A session with no topic is a real state, not an error. You have to be
        #: able to open the thing on an empty board and start from inside it --
        #: being told to go back to the shell first is exactly the break this
        #: session exists to remove.
        self.topic = None
        self.topic_id = None
        if topic_ref is not None:
            self.topic = self.store.topic(
                int(topic_ref) if str(topic_ref).isdigit() else topic_ref)
            self.topic_id = int(self.topic["id"])
        self.me = me
        self.stop = threading.Event()
        self.driving = threading.Event()
        #: Posting wakes the council without you typing /run. This is the whole
        #: difference between a meeting and a batch job: you say something, they
        #: pick it up, you see the replies, you answer again. /auto off restores
        #: the explicit-only behaviour.
        self.auto = True
        #: Message id this reply is attached to, set by /quote and cleared on post.
        self._quoting: int | None = None
        #: Where command output goes. The REPL prints; the TUI writes to a widget.
        #: Both drive the same `handle()`, so a command cannot behave differently
        #: in one surface than the other -- two dispatchers would drift.
        self.emit = print

    # ------------------------------------------------------------------- state

    def seat_names(self) -> list[str]:
        if self.topic_id is None:      # nothing seated yet; offer everyone registered
            return [a["name"] for a in self.store.agents()
                    if a["kind"] not in {"human", "external"}]
        return [s["agent"] for s in self.store.seats(self.topic_id) if s["agent"] != self.me]

    def pending_asks(self):
        return [] if self.topic_id is None else self.store.open_mentions(self.topic_id, self.me)

    def effort(self) -> str:
        if self.topic_id is None:
            return "medium"
        return self.store.topic(self.topic_id)["effort"] or "medium"

    def _require_topic(self) -> bool:
        if self.topic_id is None:
            self.emit(f"{DIM}no topic yet — /new <what you want to discuss>"
                      f"{', or /topic <slug>' if self.store.topics() else ''}{RESET}")
            return False
        return True

    def toolbar(self) -> str:  # pragma: no cover - UI
        asks = len(self.pending_asks())
        props = len(self.store.proposals(self.topic_id, status="open"))
        bits = [f" {self.topic['slug']}", f"effort {self.effort()}"]
        # Live, because the toolbar re-renders on a timer: you can watch a seat
        # think instead of wondering whether anything is happening.
        thinking = [f"{w['agent']} {w['secs']}s" for w in self.store.active_wakes(self.topic_id)]
        if thinking:
            bits.append("thinking: " + ", ".join(thinking))
        else:
            bits.append("driving" if self.driving.is_set() else "idle")
        if asks:
            bits.append(f"{asks} question(s) for you")
        if props:
            bits.append(f"{props} proposal(s) to rule on")
        return "  |  ".join(bits)

    # ----------------------------------------------------------------- threads

    def _poll(self) -> None:
        """Tail the board. Own Store: sqlite3 connections are per-thread."""
        store = connect(self.db)
        cursor = store.head()
        if self.topic_id is None:
            while not self.stop.is_set():
                time.sleep(1.0)
            store.close()
            return
        for m in store.transcript(self.topic_id)[-6:]:
            self.emit(f"\n{DIM}{m['author']}{RESET}  {m['body'].strip()[:400]}")
        for a in store.open_mentions(self.topic_id, self.me):
            self.emit(_ask_banner(a["asker"], a["question"]))
        while not self.stop.is_set():
            try:
                for ev in store.events_since(cursor, self.topic_id):
                    cursor = ev.id
                    line = _fmt_event(store, ev, self.me)
                    if line:
                        self.emit(line)
            except Exception as exc:                    # never kill the console
                self.emit(f"{RED}poller: {exc}{RESET}")
            time.sleep(1.0)
        store.close()

    def _drive(self) -> None:
        import asyncio

        from .drivers.registry import build_drivers
        from .supervisor import Caps, Supervisor

        store = connect(self.db)
        try:
            if store.topic(self.topic_id)["status"] == "paused":
                store.set_topic_status(self.topic_id, "open", self.me, "resumed from console")
            sup = Supervisor(store, build_drivers(store), Caps(effort=self.effort()))
            reason = asyncio.run(sup.run_topic(self.topic_id))
            self.emit(f"\n{DIM}— council stopped: {reason}{RESET}")
            for a in store.open_mentions(self.topic_id, self.me):
                self.emit(_ask_banner(a["asker"], a["question"]))
        except Exception as exc:
            self.emit(f"\n{RED}— council failed: {exc}{RESET}")
        finally:
            store.close()
            self.driving.clear()

    # ---------------------------------------------------------------- commands

    def handle(self, line: str) -> bool:
        """Returns False to quit."""
        line = line.strip()
        if not line:
            return True

        if not line.startswith("/"):
            return self._speak(line)

        cmd, _, rest = line[1:].partition(" ")
        rest = rest.strip()
        fn = {
            "quit": lambda _: False, "q": lambda _: False, "exit": lambda _: False,
            "help": self._help, "run": self._run, "stop": self._stop,
            "effort": self._effort, "asks": self._asks, "nudge": self._nudge,
            "auto": self._auto,
            "proposals": self._proposals, "seats": self._seats, "topic": self._switch,
            "tasks": self._tasks, "new": self._new, "mode": self._mode,
            "manager": self._manager, "rm": self._rm, "reset": self._reset,
            "quote": self._quote, "rounds": self._rounds, "me": self._me,
            "minutes": self._minutes, "show": self._show,
            "conclude": self._conclude, "reopen": self._reopen,
        }.get(cmd)
        if cmd in {"approve", "reject"}:
            self._decide(cmd, rest)
            return True
        if fn is None:
            self.emit(f"{RED}unknown /{cmd}{RESET} — /help")
            return True
        return fn(rest) is not False

    def _speak(self, line: str) -> bool:
        if not self._require_topic():
            return True
        if line.startswith("@"):
            # "@agy, what do you think?" -- punctuation after a name is normal
            # writing, and taking it as part of the name produced the baffling
            # "'agy,' holds no seat on this topic".
            m = MENTION_RE.match(line)
            target = m.group(1) if m else ""
            question = line[m.end():].lstrip(" ,:;-–—") if m else ""
            if not target or not question.strip():
                self.emit(f"{RED}usage: @agent your question{RESET}")
                return True
            try:
                self.store.ask(self.topic_id, self.me, target, question.strip())
            except StoreError as exc:
                self.emit(f"{RED}{exc}{RESET}")
                return True
            self.emit(f"{DIM}asked {target}{RESET}")
            # Falls through to the wake below. Returning here is why asking a
            # question started nothing and the council just sat there.
            self._after_post(asked=target)
            return True

        answered = self.pending_asks()
        # count_turn=False: a human joining never spends an agent's metered turn.
        self.store.post(self.topic_id, self.me, line, count_turn=False,
                        reply_to=self._quoting)
        self._quoting = None
        if answered:
            who = ", ".join(sorted({a["asker"] for a in answered}))
            self.emit(f"{DIM}answers {who}{RESET}")
        self._after_post()
        return True

    def _after_post(self, asked: str | None = None) -> None:
        """Anything you say is new board state, so the council has something to
        react to. Making you type /run here is what made this a batch job."""
        if self.driving.is_set():
            return
        topic = self.store.topic(self.topic_id)
        if topic["round"] + 1 >= topic["max_rounds"]:
            # You typing is the authorisation. The cap exists to stop a loop
            # running away unattended, not to make you ask permission to continue
            # a conversation you are sitting in -- so one round is granted, and
            # only one, so it still cannot run off on its own.
            self.store.grant_rounds(self.topic_id, 1)
            topic = self.store.topic(self.topic_id)
            self.emit(f"{DIM}was out of rounds — granted one more "
                      f"(round {topic['round'] + 1}/{topic['max_rounds']}); "
                      f"/rounds <n> for more{RESET}")
        if self.auto:
            self._run("")
        else:
            self.emit(f"{DIM}/run when you want them to pick it up{RESET}")

    #: Grouped, because a flat alphabetical list of eighteen commands answers
    #: "what exists" and not "what do I do now" -- and the answer to the second is
    #: usually "just type", which a command list never says.
    HELP = [
        ("Talking", [
            ("<anything>", "post it — and it clears any question waiting on you"),
            ("@agent <question>", "ask one seat; the others wait for their answer"),
            ("/quote", "reply to the last thing anyone said"),
            ("/quote <seat>", "reply to that seat's latest, or /quote <id>"),
            ("/me <name>", "what the council calls you"),
        ]),
        ("Running the council", [
            ("/run", "start it (posting starts it too, unless /auto off)"),
            ("/stop", "pause after the turn in flight"),
            ("/effort low|medium|high", "how hard everyone thinks — low is ~9x faster"),
            ("/nudge <agent>", "wake one seat by hand"),
            ("/auto on|off", "whether posting wakes the council"),
        ]),
        ("Deciding — only you can", [
            ("/approve <id> <why>", "accept a proposal"),
            ("/reject <id> <why>", "refuse it"),
            ("/proposals", "what is waiting on your ruling"),
            ("/proposals <id>", "the whole proposal: body, votes, objections"),
            ("/show <id>", "one message in full, however far back it scrolled"),
            ("/asks", "questions waiting on your answer"),
        ]),
        ("Topics", [
            ("/new <what to discuss>", "opens one here; the handle is derived"),
            ("/topic <slug>", "switch to another"),
            ("/mode debate|discuss", "argue to find the flaw / build together"),
            ("/mode work <agent>", "team mode; that seat plans and reviews"),
            ("/seats", "who is here, budget left, who owes an answer"),
            ("/seats add <agent>", "seat one already registered"),
            ("/seats add <name> <cli>", "register a new seat and seat it"),
            ("/seats rm <agent>", "remove one; what it already said stays"),
            ("/rounds <n>", "grant more rounds when a topic runs out"),
            ("/tasks", "the work plan and where each task has got to"),
            ("/conclude <closing words>", "close the meeting and write its minutes"),
            ("/reopen", "resume a meeting you concluded"),
            ("/minutes", "write the meeting out as markdown"),
            ("/minutes decisions", "the rulings and work log, without the transcript"),
        ]),
        ("Clearing up", [
            ("/rm [slug] yes", "delete a topic (omit slug for this one)"),
            ("/reset yes", "clear every topic; seats are kept"),
            ("/quit", "leave — the board keeps everything"),
        ]),
    ]

    def _help(self, _: str) -> None:
        for heading, rows in self.HELP:
            self.emit("")
            self.emit(f"{BOLD}{heading}{RESET}")
            for cmd, why in rows:
                self.emit(f"  {CYAN}{cmd:<24}{RESET} {DIM}{why}{RESET}")
        self.emit("")
        self.emit(f"{DIM}Ctrl+R run · Ctrl+S stop · Ctrl+T tasks · Ctrl+Q quit{RESET}")

    def _run(self, _: str) -> None:
        if not self._require_topic():
            return
        if self.driving.is_set():
            self.emit(f"{DIM}already driving{RESET}")
            return
        self.driving.set()
        threading.Thread(target=self._drive, daemon=True).start()
        self.emit(f"{DIM}· council thinking at effort {self.effort()} — keep typing{RESET}")

    def _auto(self, rest: str) -> None:
        if rest in {"on", "off"}:
            self.auto = rest == "on"
        self.emit(f"{DIM}auto-wake {'on — posting resumes the council' if self.auto else 'off — /run to drive'}{RESET}")

    def _stop(self, _: str) -> None:
        if not self._require_topic():
            return
        self.store.set_topic_status(self.topic_id, "paused", self.me, "stopped from console")
        self.emit(f"{DIM}pausing after the current turn{RESET}")

    def _effort(self, rest: str) -> None:
        if not self._require_topic():
            return
        """The brainstorming dial: cheap and wide, then deep on what survived."""
        if rest not in {"low", "medium", "high"}:
            self.emit(f"  effort is {BOLD}{self.effort()}{RESET}   "
                  f"{DIM}/effort low|medium|high{RESET}")
            self.emit(f"  {DIM}low ≈ 9x faster and thinner; high for a call that turns on "
                  f"catching a flaw{RESET}")
            return
        with self.store.tx() as c:
            c.execute("UPDATE topics SET effort = ? WHERE id = ?", (rest, self.topic_id))
        # Takes effect on the next wake even mid-run: the supervisor captures Caps
        # once, but wake_seat re-reads the topic row every time and topic effort
        # outranks the council default. A concurrent round's wakes all start
        # together, so the change lands on the round after the current one.
        when = " — from the next round" if self.driving.is_set() else ""
        self.emit(f"{DIM}council effort → {rest}{when}{RESET}")

    def _asks(self, _: str) -> None:
        if not self._require_topic():
            return
        asks = self.pending_asks()
        if not asks:
            self.emit(f"{DIM}nothing is waiting on you{RESET}")
            return
        for a in asks:
            self.emit(_ask_banner(a["asker"], a["question"]))

    def _seats(self, rest: str = "") -> None:
        if not self._require_topic():
            return
        words = rest.split()
        if words and words[0] in {"add", "rm", "remove"}:
            return self._seat_change(words[0], words[1] if len(words) > 1 else "",
                                     words[2] if len(words) > 2 else "")
        # `/seats Santa claude` is what people actually type. Insisting on the
        # word "add" when the meaning is unambiguous is a rule for the parser's
        # benefit, not the reader's.
        if len(words) == 2 and words[1] in AGENT_KINDS:
            return self._seat_change("add", words[0], words[1])
        if len(words) == 1 and words[0] not in {"add", "rm", "remove"}:
            return self._seat_change("add", words[0], "")
        if words:
            self.emit(f"{RED}not sure what you meant by /seats {rest}{RESET}")
            self.emit(f"{DIM}/seats                       who is here{RESET}")
            self.emit(f"{DIM}/seats <name> <cli>          add a new seat "
                      f"({', '.join(sorted(AGENT_KINDS))}){RESET}")
            self.emit(f"{DIM}/seats add <agent>           seat one already "
                      f"registered{RESET}")
            self.emit(f"{DIM}/seats rm <agent>            remove one{RESET}")
            return
        for s in self.store.seats(self.topic_id):
            owed = len(self.store.open_mentions(self.topic_id, s["agent"]))
            flag = f"  {YELLOW}{owed} open ask(s){RESET}" if owed else ""
            self.emit(f"  {s['agent']:<12} {s['kind']:<9} {s['state']:<8} "
                  f"{s['turns_used']}/{s['max_turns']} turns{flag}")

    def _tasks(self, _: str) -> None:
        if not self._require_topic():
            return
        rows = self.store.tasks(self.topic_id)
        if not rows:
            self.emit(f"{DIM}no tasks — this is not a work topic, or nothing is planned{RESET}")
            return
        for t in rows:
            self.emit(f"  #{t['id']} [{t['status']}] {t['title']} — {t['assignee']}")
            if t["branch"]:
                self.emit(f"       {DIM}{t['branch']}{RESET}")
            if t["result"]:
                self.emit(f"       {t['result'].strip()[:200]}")

    def _me(self, rest: str) -> None:
        """Rename yourself. The council addresses you by this."""
        new = rest.strip().lstrip("@")
        if not new:
            self.emit(f"  you are {BOLD}{self.me}{RESET}   {DIM}/me <name>{RESET}")
            return
        try:
            self.store.rename_agent(self.me, new)
        except StoreError as exc:
            self.emit(f"{RED}{exc}{RESET}")
            return
        old, self.me = self.me, new
        self.emit(f"{DIM}{old} → {new}, everywhere on the board{RESET}")
        self.on_topic_change()

    def _conclude(self, rest: str) -> None:
        """End the meeting and write it up, in one step.

        Ruling on proposals and exporting were separate, and nothing marked a
        meeting as over -- so minutes of an abandoned discussion read exactly like
        minutes of a settled one.
        """
        if not self._require_topic():
            return
        words = rest.split()
        force = bool(words) and words[0] in {"force", "-f", "anyway"}
        note = " ".join(words[1:] if force else words).strip()

        undecided = self.store.proposals(self.topic_id, status="open")
        unanswered = self.store.open_mentions(self.topic_id)
        if (undecided or unanswered) and not force:
            self.emit(f"{YELLOW}this meeting still has loose ends:{RESET}")
            for p in undecided:
                self.emit(f"  proposal #{p['id']} {p['title']}  "
                          f"{DIM}/approve {p['id']} <why>{RESET}")
            for m in unanswered:
                self.emit(f"  {m['asker']} asked {m['target']}: "
                          f"{' '.join(m['question'].split())[:70]}…")
            self.emit(f"{DIM}rule on them first, or /conclude force <closing words> "
                      f"to close it as it stands{RESET}")
            return

        try:
            self.store.conclude(self.topic_id, self.me, note)
        except StoreError as exc:
            self.emit(f"{RED}{exc}{RESET}")
            return
        self.emit(f"{DIM}meeting concluded{RESET}")
        self._minutes("")
        self.on_topic_change()

    def _reopen(self, _: str) -> None:
        if not self._require_topic():
            return
        try:
            self.store.reopen(self.topic_id, self.me)
        except StoreError as exc:
            self.emit(f"{RED}{exc}{RESET}")
            return
        self.emit(f"{DIM}reopened — the council can speak again{RESET}")
        self.on_topic_change()

    def _minutes(self, rest: str) -> None:
        """Take the meeting out of the board: what was asked, what was decided,
        who objected, and what came of it."""
        if not self._require_topic():
            return
        from pathlib import Path as _Path

        from .minutes import default_path, render
        words = rest.split()
        # "decisions" first, because what you usually want to hand to someone is
        # what was ruled -- the transcript is the evidence behind it, not the
        # thing itself.
        brief = bool(words) and words[0] in {"decisions", "decision", "-d"}
        if brief:
            words = words[1:]
        stem = default_path(self.store, self.topic_id)
        if brief:
            stem = stem.replace("-minutes.md", "-decisions.md")
        text = render(self.store, self.topic_id, transcript=not brief)
        path = _Path(words[0] if words else stem)
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            self.emit(f"{RED}could not write {path}: {exc}{RESET}")
            return
        decided = [p for p in self.store.proposals(self.topic_id)
                   if p["status"] in {"approved", "rejected"}]
        self.emit(f"{DIM}wrote {path.resolve()} — {len(text.splitlines())} lines, "
                  f"{len(decided)} decision(s)"
                  f"{', transcript omitted' if brief else ''}{RESET}")

    def _quote(self, rest: str) -> None:
        """Attach your next message to one already said.

        Hunting for an id is the wrong ask: the thing you want to answer is
        almost always the last thing someone said, so a bare /quote takes that,
        /quote <seat> takes that seat's latest, and an id is there when you need
        to reach further back.
        """
        if not self._require_topic():
            return
        ref = rest.strip().lstrip("#")

        if ref in {"0", "off", "none"}:
            self._quoting = None
            self.emit(f"{DIM}not replying to anything{RESET}")
            return

        row = None
        if not ref:
            row = self.store.q1(
                "SELECT * FROM messages WHERE topic_id = ? AND author != ? "
                "AND kind IN ('say','propose','object','support') "
                "ORDER BY id DESC LIMIT 1", (self.topic_id, self.me))
            if row is None:
                self.emit(f"{DIM}nobody has said anything to reply to yet{RESET}")
                return
        elif ref.isdigit():
            row = self.store.q1("SELECT * FROM messages WHERE id = ? AND topic_id = ?",
                                (int(ref), self.topic_id))
            if row is None:
                self.emit(f"{RED}no message #{ref} on this topic{RESET}")
                return
        else:
            row = self.store.q1(
                "SELECT * FROM messages WHERE topic_id = ? AND author = ? "
                "ORDER BY id DESC LIMIT 1", (self.topic_id, ref.lstrip("@")))
            if row is None:
                self.emit(f"{RED}{ref!r} has not said anything here{RESET}")
                self.emit(f"{DIM}/quote            the last thing anyone said{RESET}")
                self.emit(f"{DIM}/quote <seat>     that seat's latest{RESET}")
                self.emit(f"{DIM}/quote <id>       the dim #n beside a message{RESET}")
                return

        self._quoting = int(row["id"])
        preview = " ".join(row["body"].split())[:90]
        self.emit(f"{DIM}replying to #{row['id']} {row['author']}: {preview}…{RESET}")

    def _rounds(self, rest: str) -> None:
        if not self._require_topic():
            return
        if not rest.strip().isdigit():
            t = self.store.topic(self.topic_id)
            self.emit(f"  round {t['round'] + 1} of {t['max_rounds']}   "
                      f"{DIM}/rounds <n> to grant more{RESET}")
            return
        self.store.grant_rounds(self.topic_id, int(rest))
        t = self.store.topic(self.topic_id)
        self.emit(f"{DIM}now round {t['round'] + 1} of {t['max_rounds']}, "
                  f"and every seat has {rest} more turn(s){RESET}")

    def _seat_change(self, verb: str, agent: str, kind: str = "") -> None:
        """Add or remove a seat on this topic.

        The council is per topic, not global: some questions want the historian,
        some want the engine, and paying four CLIs to sit through a question two
        of them cannot help with is the cost this whole thing exists to control.
        """
        if not agent:
            self.emit(f"{RED}usage: /seats {verb} <agent>{RESET}")
            self.emit(f"{DIM}registered: "
                      f"{', '.join(a['name'] for a in self.store.agents())}{RESET}")
            return
        try:
            self.store.agent(agent)
        except StoreError:
            # Naming a CLI registers the seat on the spot. Several seats can run
            # the same CLI under different names -- a historian and an engineer
            # both on claude, with different working directories -- and giving
            # them names you chose is most of what makes a council readable.
            if verb == "add" and kind in AGENT_KINDS:
                self.store.add_agent(agent, kind, driver_cfg={"cwd": os.getcwd()})
                self.emit(f"{DIM}registered {agent} (runs {kind}){RESET}")
            else:
                self.emit(f"{RED}{agent!r} is not a registered seat{RESET}")
                self.emit(f"{DIM}name the CLI to create it:  /seats add {agent} "
                          f"<{'|'.join(sorted(AGENT_KINDS))}>{RESET}")
                return

        seated = self.store.seat(self.topic_id, agent) is not None
        if verb == "add":
            if seated:
                self.emit(f"{DIM}{agent} is already here{RESET}")
                return
            with self.store.tx() as c:
                c.execute("INSERT INTO seats (topic_id, agent) VALUES (?,?)",
                          (self.topic_id, agent))
            # last_seen stays 0 on purpose: someone joining late should read what
            # was already said before answering, which the bounded catch-up gives
            # them for free.
            self.emit(f"{DIM}{agent} seated — it will catch up on the discussion "
                      f"so far{RESET}")
        else:
            if not seated:
                self.emit(f"{DIM}{agent} is not on this topic{RESET}")
                return
            if self.store.is_manager(self.topic_id, agent):
                self.emit(f"{RED}{agent} manages this topic — /mode work <someone "
                          f"else> first, or /mode discuss{RESET}")
                return
            owed = len(self.store.open_mentions(self.topic_id, agent))
            with self.store.tx() as c:
                c.execute("DELETE FROM seats WHERE topic_id = ? AND agent = ?",
                          (self.topic_id, agent))
                # An unanswerable question would block the room forever.
                c.execute("DELETE FROM mentions WHERE topic_id = ? AND target = ? "
                          "AND answered_by IS NULL", (self.topic_id, agent))
            note = f" ({owed} unanswered question(s) dropped)" if owed else ""
            self.emit(f"{DIM}{agent} left — what it already said stays{note}{RESET}")
        self.on_topic_change()

    def _proposals(self, rest: str = "") -> None:
        if not self._require_topic():
            return
        ref = rest.strip().lstrip("#")
        if ref.isdigit():
            return self._proposal_detail(int(ref))
        rows = self.store.proposals(self.topic_id)
        if not rows:
            self.emit(f"{DIM}nothing proposed yet{RESET}")
            return
        for p in rows:
            votes = ", ".join(f"{v['agent']}:{v['stance']}"
                              for v in self.store.votes(p["id"]))
            self.emit(f"  proposal #{p['id']} [{p['status']}] {p['title']}  "
                      f"{DIM}{votes}{RESET}")
        self.emit(f"{DIM}/proposals <n> for the whole thing — those are proposal "
                  f"numbers, not the #n beside a message{RESET}")

    def _proposal_detail(self, pid: int) -> None:
        """Everything behind a one-line summary.

        A ruling is the one thing here that cannot be undone, so the body, the
        objections and the reasons for them have to be readable at the moment you
        are deciding -- not a scroll back through the transcript.
        """
        try:
            p = self.store.proposal(pid)
        except StoreError as exc:
            self.emit(f"{RED}{exc}{RESET}")
            return
        self.emit("")
        self.emit(f"{BOLD}proposal #{p['id']} {p['title']}{RESET}  "
                  f"{DIM}[{p['status']}] by {p['author']}{RESET}")
        # Proposal numbers and message numbers are different counters, and typing
        # one where the other is expected quotes the wrong thing without complaint.
        self.emit(f"{DIM}posted as message #{p['message_id']} — "
                  f"/quote {p['message_id']} to reply to it{RESET}")
        if p["decided_by"]:
            self.emit(f"{DIM}ruled {p['status']} by {p['decided_by']} "
                      f"{p['decided_at'] or ''}{RESET}")
            if p["rationale"]:
                self.emit(f"{DIM}  “{p['rationale'].strip()}”{RESET}")
        self.emit("")
        self.emit(p["body"].strip())
        votes = self.store.votes(pid)
        if votes:
            self.emit("")
            for v in votes:
                mark = {"support": "+", "object": "!", "abstain": "~"}.get(v["stance"], "?")
                self.emit(f"  {mark} {BOLD}{v['agent']}{RESET} {v['stance']}")
                if v["rationale"]:
                    self.emit(f"    {DIM}{v['rationale'].strip()}{RESET}")
        if p["status"] == "open":
            self.emit("")
            self.emit(f"{DIM}/approve {pid} <why>   |   /reject {pid} <why>{RESET}")

    def _show(self, rest: str) -> None:
        """One message in full, however far back it scrolled."""
        if not self._require_topic():
            return
        ref = rest.strip().lstrip("#")
        if not ref.isdigit():
            self.emit(f"{RED}usage: /show <id>{RESET}   "
                      f"{DIM}ids are the dim #n beside each message{RESET}")
            return
        row = self.store.q1("SELECT * FROM messages WHERE id = ? AND topic_id = ?",
                            (int(ref), self.topic_id))
        if row is None:
            self.emit(f"{RED}no message #{ref} on this topic{RESET}")
            return
        self.emit("")
        self.emit(f"{BOLD}{row['author']}{RESET} {DIM}#{row['id']} · {row['kind']} · "
                  f"{row['created_at']}{RESET}")
        if row["reply_to"]:
            ref_row = self.store.quoted(int(row["reply_to"]))
            if ref_row is not None:
                preview = " ".join(ref_row["body"].split())[:90]
                self.emit(f"{DIM}  | replying to #{ref_row['id']} "
                          f"{ref_row['author']}: {preview}…{RESET}")
        self.emit("")
        self.emit(row["body"].strip())

    def _rm(self, rest: str) -> bool:
        """Delete a topic. Two steps, because one keystroke should not be able to
        destroy a conversation you cannot get back."""
        words = rest.split()
        confirmed = bool(words) and words[-1] == "yes"
        if confirmed:
            words = words[:-1]
        ref = words[0] if words else self.topic["slug"]
        try:
            t = self.store.topic(int(ref) if ref.isdigit() else ref)
        except StoreError as exc:
            self.emit(f"{RED}{exc}{RESET}")
            return True
        tid = int(t["id"])
        trees = self.store.orphan_worktrees(tid)

        if not confirmed:
            self.emit(f"{YELLOW}delete `{t['slug']}` — {t['title']}?{RESET}")
            self.emit(f"  {len(self.store.transcript(tid))} message(s), "
                      f"{len(self.store.tasks(tid))} task(s), "
                      f"{len(self.store.proposals(tid))} proposal(s)")
            for w in trees:
                self.emit(f"  {DIM}leaves a worktree on disk: {w}{RESET}")
            self.emit(f"{DIM}confirm with:  /rm {ref} yes{RESET}")
            return True

        was_current = tid == self.topic_id
        self.store.delete_topic(tid)
        self.emit(f"{DIM}deleted `{t['slug']}`{RESET}")
        for w in trees:
            self.emit(f"{DIM}  worktree left on disk — git worktree remove {w}{RESET}")
        return self._land_somewhere() if was_current else True

    def _reset(self, rest: str) -> bool:
        topics = self.store.topics()
        if rest.strip() != "yes":
            self.emit(f"{YELLOW}clear all {len(topics)} topic(s)?{RESET}")
            for t in topics[:10]:
                self.emit(f"  {t['slug']:<20} {t['title'][:44]}")
            if len(topics) > 10:
                self.emit(f"  {DIM}… and {len(topics) - 10} more{RESET}")
            self.emit(f"{DIM}seats are kept. confirm with:  /reset yes{RESET}")
            return True
        trees = [w for t in topics for w in self.store.orphan_worktrees(int(t["id"]))]
        n = self.store.clear_topics()
        self.emit(f"{DIM}cleared {n} topic(s){RESET}")
        for w in trees:
            self.emit(f"{DIM}  worktree left on disk — git worktree remove {w}{RESET}")
        return self._land_somewhere()

    def _land_somewhere(self) -> bool:
        """After deleting what we were looking at, find somewhere to stand.

        An empty board is a place you can stand. Ending the session here would
        mean clearing the board throws you out of the very thing you would use to
        start again.
        """
        rest = [t for t in self.store.topics() if t["status"] in {"open", "paused"}]
        if rest:
            self._switch(rest[0]["slug"])
            return True
        self.topic, self.topic_id = None, None
        self.emit(f"{DIM}board is empty — /new <what you want to discuss>{RESET}")
        self.on_topic_change()
        return True

    def _switch(self, rest: str) -> None:
        try:
            self.topic = self.store.topic(rest)
            self.topic_id = int(self.topic["id"])
        except StoreError as exc:
            self.emit(f"{RED}{exc}{RESET}")
            return
        self.emit(f"{DIM}now on `{self.topic['slug']}` ({self.topic['mode']}){RESET}")
        self.on_topic_change()

    def _new(self, rest: str) -> None:
        """Open a topic without leaving the session.

        Seats, mode and effort carry over from where you are standing, because the
        common case is "same room, next question" -- and re-listing the council
        every time is the friction that sends you back to the shell.
        """
        title = rest.strip()
        if not title:
            self.emit(f"{RED}usage: /new <what you want to discuss>{RESET}")
            self.emit(f"{DIM}e.g.  /new workflow optimization in agentic AI development"
                      f"{RESET}")
            return
        # The short handle is derived, not demanded. Asking someone to invent a
        # name for their own question before they can ask it is friction for
        # nothing -- the title is what they actually have.
        slug = slugify(title, [t["slug"] for t in self.store.topics()])
        if self.topic_id is None:
            # Nothing to carry over: seat everyone registered, which is what a
            # first topic on a fresh board almost always wants.
            seats = [a["name"] for a in self.store.agents() if a["enabled"]]
            mode, effort, manager = "debate", None, None
        else:
            here = self.store.topic(self.topic_id)
            seats = [s["agent"] for s in self.store.seats(self.topic_id)]
            mode, effort = here["mode"], here["effort"]
            manager = next((s["agent"] for s in self.store.seats(self.topic_id)
                            if s["role"] == "manager"), None)
        if self.me not in seats:
            seats.append(self.me)
        try:
            self.store.open_topic(slug, title, title, self.me, seats=seats,
                                  mode=mode, effort=effort, manager=manager)
        except StoreError as exc:
            self.emit(f"{RED}{exc}{RESET}")
            return
        except Exception as exc:   # UNIQUE on slug is the one people hit
            self.emit(f"{RED}could not open `{slug}`: {exc}{RESET}")
            return
        self._switch(slug)
        self.emit(f"{DIM}seats: {', '.join(seats)} — type any detail they need, "
                  f"then /run{RESET}")

    def _mode(self, rest: str) -> None:
        if not self._require_topic():
            return
        words = rest.split()
        mode = words[0] if words else ""
        if mode not in {"debate", "discuss", "work"}:
            self.emit(f"  mode is {BOLD}{self.store.topic(self.topic_id)['mode']}{RESET}   "
                      f"{DIM}/mode debate | discuss | work <manager>{RESET}")
            return

        if mode == "work":
            # A role only exists where it means something. In discussion there is
            # no manager to be -- everyone argues on equal footing -- so the role
            # is granted when the topic becomes work, and taken back when it stops
            # being work, rather than lingering as a title nobody uses.
            manager = words[1] if len(words) > 1 else None
            if not manager:
                self.emit(f"{RED}work needs a manager: /mode work <agent>{RESET}")
                self.emit(f"{DIM}candidates: {', '.join(self.seat_names())}{RESET}")
                return
            if not self.store.seat(self.topic_id, manager):
                self.emit(f"{RED}{manager!r} holds no seat here{RESET}")
                return
            with self.store.tx() as c:
                c.execute("UPDATE topics SET mode = 'work' WHERE id = ?", (self.topic_id,))
                c.execute("UPDATE seats SET role = 'participant' WHERE topic_id = ?",
                          (self.topic_id,))
                c.execute("UPDATE seats SET role = 'manager' WHERE topic_id = ? "
                          "AND agent = ?", (self.topic_id, manager))
            self.emit(f"{DIM}mode → work, {manager} manages{RESET}")
        else:
            with self.store.tx() as c:
                c.execute("UPDATE topics SET mode = ? WHERE id = ?", (mode, self.topic_id))
                c.execute("UPDATE seats SET role = 'participant' WHERE topic_id = ?",
                          (self.topic_id,))
            self.emit(f"{DIM}mode → {mode} — no roles; everyone argues on equal footing"
                      f"{RESET}")
        self.on_topic_change()

    def _manager(self, rest: str) -> None:
        if not self._require_topic():
            return
        if self.store.topic(self.topic_id)["mode"] != "work":
            self.emit(f"{DIM}no manager in a discussion — roles exist on work topics."
                      f" /mode work <agent> to switch.{RESET}")
            return
        if not self.store.seat(self.topic_id, rest):
            self.emit(f"{RED}{rest!r} holds no seat here{RESET}")
            return
        with self.store.tx() as c:
            c.execute("UPDATE seats SET role = 'participant' WHERE topic_id = ?",
                      (self.topic_id,))
            c.execute("UPDATE seats SET role = 'manager' WHERE topic_id = ? AND agent = ?",
                      (self.topic_id, rest))
        self.emit(f"{DIM}{rest} is now the manager{RESET}")
        self.on_topic_change()

    def on_topic_change(self) -> None:
        """Hook for a surface that has to repaint. The REPL has nothing to do."""

    def _nudge(self, agent: str) -> None:
        if not self._require_topic():
            return
        import asyncio

        from .drivers.registry import build_drivers
        from .supervisor import Supervisor

        def work() -> None:
            store = connect(self.db)
            try:
                r = asyncio.run(Supervisor(store, build_drivers(store, [agent]))
                                .wake_seat(self.topic_id, agent))
                if not r.ok:
                    self.emit(f"\n{RED}{agent}: {r.detail}{RESET}")
            finally:
                store.close()

        threading.Thread(target=work, daemon=True).start()
        self.emit(f"{DIM}waking {agent}...{RESET}")

    def _decide(self, cmd: str, rest: str) -> None:
        pid_s, _, why = rest.partition(" ")
        if not pid_s.isdigit():
            self.emit(f"{RED}usage: /{cmd} <proposal id> [reason]{RESET}")
            return
        try:
            self.store.decide(int(pid_s), self.me, cmd == "approve", why.strip())
        except StoreError as exc:
            self.emit(f"{RED}{exc}{RESET}")

    # -------------------------------------------------------------------- loop

    def run(self) -> int:
        self.emit(BANNER)
        if self.topic is None:
            self.emit(f"{DIM}no topic yet — /new <slug> <title> to start one{RESET}")
        else:
            self.emit(f"{BOLD}{self.topic['title']}{RESET}  {DIM}(`{self.topic['slug']}`, "
                      f"{self.topic['status']}, effort {self.effort()}){RESET}")
        self.emit(f"{DIM}you are {self.me}. /run to start, /help for commands.{RESET}")

        threading.Thread(target=self._poll, daemon=True).start()
        try:
            self._input_loop()
        except KeyboardInterrupt:
            self.emit()
        finally:
            self.stop.set()
            self.store.close()
        self.emit(f"{DIM}left the console. The board keeps everything: "
              f"agora show {self.topic['slug']}{RESET}")
        return 0

    def _input_loop(self) -> None:
        """Rich prompt where the terminal supports one, plain input everywhere else.

        prompt_toolkit needs a real console. Piped stdin raises EOF, and on Windows
        an MSYS/Cygwin terminal -- Git Bash, mintty -- raises
        NoConsoleScreenBufferError, which is a very plausible way to launch this.
        Crashing there would be absurd when `input()` works fine, so any failure
        setting up the rich prompt degrades instead of ending the session.
        """
        if not HAVE_PTK or not sys.stdin.isatty():
            if HAVE_PTK is False:
                self.emit(f"{DIM}(pip install prompt_toolkit for a prompt that survives "
                      f"incoming messages){RESET}")
            self._plain_loop()
            return
        try:
            self._ptk_loop()
        except Exception as exc:
            self.emit(f"{DIM}(plain prompt: {type(exc).__name__} — for the full console "
                  f"use Windows Terminal, PowerShell or cmd rather than mintty){RESET}")
            self._plain_loop()

    def _ptk_loop(self) -> None:  # pragma: no cover - interactive
        completer = _ConsoleCompleter(self)
        # refresh_interval keeps the toolbar live, so "claude thinking 14s" ticks
        # up while you wait rather than freezing until your next keystroke.
        session = PromptSession(completer=completer, complete_while_typing=False,
                                refresh_interval=1.0)
        # patch_stdout is the whole point: the poller thread's prints land *above*
        # the prompt instead of through the middle of what you are typing.
        with patch_stdout(raw=True):
            while True:
                try:
                    line = session.prompt(ANSI(f"{BOLD}>{RESET} "),
                                          bottom_toolbar=lambda: self.toolbar())
                except EOFError:
                    break
                except KeyboardInterrupt:
                    continue          # Ctrl-C clears the line; Ctrl-D leaves
                if not self.handle(line):
                    break

    def _plain_loop(self) -> None:
        while True:
            try:
                line = input(f"{BOLD}>{RESET} ")
            except EOFError:
                break
            if not self.handle(line):
                break


def run_console(db: Path | str | None, topic: str, me: str) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.platform == "win32":
        import os
        os.system("")   # enable ANSI on legacy conhost
    return Console(db, topic, me).run()
