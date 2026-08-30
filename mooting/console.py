"""`mooting console` -- one terminal where the whole council is visible, and where
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

import json
import os
import shlex
import sys
import threading
import time
from pathlib import Path

from .store import (MENTION_RE, Store, StoreError, agenda_text, connect,
                    slugify, split_points)

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
    "/approve": "<id> <why> -- sign off on a proposal (only you can)",
    "/reject": "<id> <why>",
    "/proposals": "what is waiting on you; /proposals <id> for the whole thing",
    "/show": "<id> -- a message in full, however far back it scrolled",
    "/tasks": "the work plan and where each task has got to",
    "/quote": "reply to the last message; or /quote <seat> | <id>",
    "/me": "<name> -- what the council calls you",
    "/minutes": "write the meeting out; /minutes decisions for the decisions only",
    "/conclude": "<your closing words> -- close the meeting and write the minutes",
    "/reopen": "resume a meeting you concluded",
    "/rounds": "<n> -- grant the council more rounds on this topic",
    "/seats": "who is here; /seats add <agent> | /seats rm <agent>",
    "/attach": "<file> -- feed a document to this council; /attach alone lists",
    "/topic": "new | switch | rename | agenda | mode | manager | rm | list",
    "/topic new": "<title> -- open a topic, same seats",
    "/topic agenda": "<point>; <point> -- what this meeting is to settle",
    "/topic rename": "<slug> <new title> -- rename any topic",
    "/capability": "<agent> execute <dir> -- let a seat edit files there",
    "/reset": "clear every topic; add `yes` to confirm",
    "/help": "this list",
    "/quit": "leave (the board keeps everything)",
}

#: Commands that used to be top level and now live under `/topic`. Kept so the
#: session answers "where did it go" instead of "unknown", which is the one
#: question a rename actually creates. Muscle memory outlives a changelog.
MOVED = {
    "new": "topic new", "agenda": "topic agenda", "mode": "topic mode",
    "manager": "topic manager", "rm": "topic rm", "rename": "topic rename",
}

BANNER = f"""{BOLD}mooting console{RESET}  --  everything the council says, in one place

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
            return "low"
        return self.store.topic(self.topic_id)["effort"] or "low"

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
            bits.append(f"{props} proposal(s) waiting on you")
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
            # The per-seat budget lives on the seat row, and `/rounds` writes it
            # there. Leaving Caps at its default put a second, invisible ceiling
            # of 6 on top: a seat granted 10 turns stopped at 6 and reported
            # having none left while the panel still showed 7/10.
            budget = max((s["max_turns"] for s in store.seats(self.topic_id)),
                         default=Caps.max_turns_per_seat)
            sup = Supervisor(store, build_drivers(store),
                             Caps(effort=self.effort(), max_turns_per_seat=budget))
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
            "proposals": self._proposals, "seats": self._seats, "topic": self._topic,
            "tasks": self._tasks,
            "reset": self._reset,
            "capability": self._capability,
            "quote": self._quote, "rounds": self._rounds, "me": self._me,
            "minutes": self._minutes, "show": self._show,
            "conclude": self._conclude, "reopen": self._reopen,
        }.get(cmd)
        if cmd in {"approve", "reject"}:
            self._decide(cmd, rest)
            return True
        if fn is None:
            if cmd in MOVED:
                # Show the line they meant, arguments and all, so it can be
                # retyped without translating anything.
                now = f"/{MOVED[cmd]}" + (f" {rest}" if rest else "")
                self.emit(f"{YELLOW}/{cmd} is now {BOLD}/{MOVED[cmd]}{RESET}"
                          f"{YELLOW} — topic commands are grouped{RESET}")
                self.emit(f"  {DIM}{now}{RESET}")
            else:
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
            ("/proposals", "what is waiting on your sign-off"),
            ("/proposals <id>", "the whole proposal: body, votes, objections"),
            ("/show <id>", "one message in full, however far back it scrolled"),
            ("/asks", "questions waiting on your answer"),
        ]),
        ("Topics", [
            ("/topic new <what to discuss>", "opens one here; the handle is derived"),
            ("/topic agenda <a>; <b>", "the points this meeting is to settle"),
            ("/topic agenda clear", "start the agenda over"),
            ("/attach <file>", "feed a document to the council; text is inlined"),
            ("/attach", "what is attached; /attach rm <id> removes one"),
            ("/topic", "where you are, the agenda, and what else is open"),
            ("/topic switch <slug>", "move to another topic"),
            ("/topic rename <slug> <title>", "rename any topic"),
            ("/topic rm <slug>", "delete one; /topic rm yes for this one"),
            ("/topic mode debate|discuss", "argue to find the flaw / build together"),
            ("/topic mode work <agent>", "team mode; that seat plans and reviews"),
            ("/capability <agent> execute <dir>", "let a seat edit files in that repo"),
            ("/seats", "who is here, budget left, who owes an answer"),
            ("/seats add <agent>", "seat one already registered"),
            ("/seats add <name> <cli>", "register a new seat and seat it"),
            ("/seats rm <agent>", "remove one; what it already said stays"),
            ("/rounds <n>", "grant more rounds when a topic runs out"),
            ("/tasks", "the work plan and where each task has got to"),
            ("/conclude <closing words>", "close the meeting and write its minutes"),
            ("/reopen", "resume a meeting you concluded"),
            ("/minutes", "write the meeting out as markdown"),
            ("/minutes decisions", "the decisions and work log, without the transcript"),
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
        # The verb stays required. Guessing that `/seats Santa claude` means add
        # would make the command mean different things depending on whether a
        # word happens to be a CLI name, and a command that quietly reinterprets
        # you is worse than one that says what it expected. The mistake is cheap
        # to make, so the message is written to correct it in one read.
        if words:
            self.emit(f"{RED}/seats {rest} — expected add or rm first{RESET}")
            if len(words) >= 2 and words[1] in AGENT_KINDS:
                self.emit(f"{DIM}you probably want:  /seats add "
                          f"{words[0]} {words[1]}{RESET}")
            self.emit(f"{DIM}/seats                     who is here{RESET}")
            self.emit(f"{DIM}/seats add <name> <cli>    a new seat "
                      f"({', '.join(sorted(AGENT_KINDS))}){RESET}")
            self.emit(f"{DIM}/seats add <agent>         one already registered{RESET}")
            self.emit(f"{DIM}/seats rm <agent>          remove one{RESET}")
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
        from .minutes import commits_on
        for t in rows:
            self.emit(f"  #{t['id']} [{t['status']}] {t['title']} — {t['assignee']}")
            if t["branch"]:
                # What the branch holds, next to what the worker said about it.
                n = commits_on(t)
                landed = f"  {n} commit(s)" if n is not None else ""
                self.emit(f"       {DIM}{t['branch']}{landed}{RESET}")
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

        # Only a proposal stops you, because a proposal is *your* outstanding
        # duty -- nobody else can close it. A question one agent left hanging for
        # another is a loose end worth recording, not a reason you cannot end the
        # meeting you are chairing.
        if undecided and not force:
            self.emit(f"{YELLOW}there is a decision still waiting on you:{RESET}")
            for p in undecided:
                self.emit(f"  proposal #{p['id']} {p['title']}")
                self.emit(f"    {DIM}/proposals {p['id']} to read it · "
                          f"/approve {p['id']} <why> · /reject {p['id']} <why>{RESET}")
            self.emit(f"{DIM}sign off on it, or /conclude force <closing words> to close "
                      f"the meeting with it unresolved{RESET}")
            return
        for m in unanswered:
            self.emit(f"{DIM}left unanswered: {m['asker']} asked {m['target']} — "
                      f"{' '.join(m['question'].split())[:70]}…{RESET}")
        if unanswered:
            self.emit(f"{DIM}(recorded in the minutes){RESET}")

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
        """`/rounds 7` means seven rounds. `/rounds +7` means seven more.

        It used to add in both cases, so asking for 7 on a 3-round topic gave 10
        rounds and 13 turns -- two numbers nobody asked for and no way to tell
        where they came from.
        """
        if not self._require_topic():
            return
        arg = rest.strip()
        add = arg.startswith("+")
        digits = arg.lstrip("+")
        if not digits.isdigit():
            t = self.store.topic(self.topic_id)
            self.emit(f"  round {BOLD}{t['round'] + 1}{RESET} of {BOLD}{t['max_rounds']}{RESET}"
                      f"   {DIM}/rounds <n> sets the total · /rounds +<n> adds more{RESET}")
            return

        n = int(digits)
        try:
            if add:
                self.store.grant_rounds(self.topic_id, n)
            else:
                self.store.set_rounds(self.topic_id, n)
        except StoreError as exc:
            self.emit(f"{RED}{exc}{RESET}")
            return

        t = self.store.topic(self.topic_id)
        seats = self.store.seats(self.topic_id)
        turns = min((s["max_turns"] for s in seats), default=t["max_rounds"])
        self.emit(f"{DIM}round {t['round'] + 1} of {t['max_rounds']} — "
                  f"each seat may speak up to {turns} time(s){RESET}")

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
                # codex, gemini and agy cannot be handed their MCP server per
                # run, so it is registered under the seat's own name. Without
                # this the CLI falls back to whatever server that *kind* has
                # registered globally, and posts arrive under that name instead.
                from .install import NEEDS_REGISTRATION, install_seat
                if kind in NEEDS_REGISTRATION:
                    ok, detail = install_seat(self.store, agent)
                    if ok:
                        self.emit(f"{DIM}{detail} — {agent} will post as itself"
                                  f"{RESET}")
                    else:
                        self.emit(f"{RED}could not register {agent}'s MCP server: "
                                  f"{detail}{RESET}")
                        self.emit(f"{RED}it would post under {kind}'s name; run "
                                  f"`mooting install {agent}` before using it{RESET}")
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
                          f"else> first, or /topic mode discuss{RESET}")
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
            self.emit(f"{DIM}{p['status']} by {p['decided_by']} "
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
            self.emit(f"{DIM}confirm with:  /topic rm {ref} yes{RESET}")
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
        self.emit(f"{DIM}board is empty — /topic new <what you want to discuss>{RESET}")
        self.on_topic_change()
        return True

    #: Everything you can do *to* a topic, reachable as `/topic <verb>`. Kept in
    #: one place so the session has one obvious noun to ask about rather than six
    #: unrelated verbs at the top level.
    TOPIC_VERBS = ("new", "switch", "rename", "agenda", "mode", "manager", "rm", "list")

    def _topic(self, rest: str) -> None:
        """`/topic` is the noun; the verbs live under it.

        `/topic <slug>` still switches, because that is the common case and it
        predates the grouping. A slug that collides with a verb loses -- name a
        topic `mode` and you will have to reach it another way, which is a fair
        trade for `/topic mode work` meaning what it says.
        """
        rest = rest.strip()
        verb, _, tail = rest.partition(" ")
        if not rest or verb == "list":
            return self._topic_overview()
        if verb in self.TOPIC_VERBS:
            return {"new": self._new, "switch": self._switch, "rename": self._rename,
                    "agenda": self._agenda, "attach": self._attach, "mode": self._mode,
                    "manager": self._manager, "rm": self._rm}[verb](tail.strip())
        # Not guessing that an unknown word is a slug. `/topic mode` would have
        # been ambiguous forever, and a wrong guess here silently switches you
        # somewhere instead of saying it did not understand.
        self.emit(f"{RED}no such topic command: {verb}{RESET}")
        self.emit(f"  {DIM}/topic switch {verb}   to move to that topic{RESET}")
        self.emit(f"  {DIM}/topic {' | '.join(self.TOPIC_VERBS)}{RESET}")

    def _topic_overview(self) -> None:
        """Where you are, what it is to settle, and what else is open."""
        if self.topic_id is not None:
            self._show_agenda()
        rows = [t for t in self.store.topics() if not t["slug"].startswith("doctor-")]
        if rows:
            self.emit(f"  {DIM}topics{RESET}")
            for t in rows:
                here = f"{BOLD}›{RESET}" if t["id"] == self.topic_id else " "
                self.emit(f"  {here} {t['slug']:<28} {DIM}{t['mode']}  {t['status']}{RESET}")
        self.emit(f"  {DIM}/topic {' | '.join(self.TOPIC_VERBS)}{RESET}")

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
            self.emit(f"{RED}usage: /topic new <what you want to discuss>{RESET}")
            self.emit(f"{DIM}e.g.  /topic new workflow optimization in agentic AI development"
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
        self.emit(f"{DIM}seats: {', '.join(seats)} — /topic agenda to say what to "
                  f"settle, then /run{RESET}")

    def _rename(self, rest: str) -> None:
        """`/topic rename <slug> <new title>` -- any topic, not just this one.

        Naming the target explicitly is the whole point: renaming something you
        are not looking at is exactly when a silent guess would be costly.
        """
        try:
            parts = shlex.split(rest)
        except ValueError:                       # an unbalanced quote
            parts = rest.split()
        if len(parts) < 2:
            self.emit(f"{RED}usage: /topic rename <slug> <new title>{RESET}")
            if self.topic_id is not None:
                cur = self.store.topic(self.topic_id)
                self.emit(f'  {DIM}e.g.  /topic rename {cur["slug"]} '
                          f'"a clearer title"{RESET}')
            return

        slug, title = parts[0], " ".join(parts[1:])
        try:
            target = self.store.topic(slug)
        except StoreError as exc:
            self.emit(f"{RED}{exc}{RESET}")
            return
        try:
            now = self.store.rename_topic(int(target["id"]), title, self.me)
        except StoreError as exc:
            self.emit(f"{RED}{exc}{RESET}")
            return

        self.emit(f"{DIM}`{slug}` renamed to {BOLD}{title}{RESET}"
                  + (f"{DIM} — now `{now}`{RESET}" if now != slug else ""))
        if self.topic_id == int(target["id"]):
            self.topic = self.store.topic(self.topic_id)
            self._show_agenda()
            self.on_topic_change()

    def _attach(self, rest: str) -> None:
        """Feed a document to the council.

        Text is copied next to the board and inlined into every seat's prompt,
        because a deliberating seat cannot open a file -- so a path alone would
        reach the one execute-capable seat and nobody else.
        """
        if not self._require_topic():
            return
        rest = rest.strip().strip('"')
        rows = self.store.attachments(self.topic_id)

        if not rest:
            if not rows:
                self.emit(f"  {DIM}nothing attached — /attach <file>{RESET}")
                return
            for a in rows:
                mark = "text" if a["is_text"] else "binary"
                self.emit(f"  {DIM}#{a['id']}{RESET} {a['name']}  "
                          f"{DIM}{a['bytes']:,}B, {mark}"
                          + (f" — {a['note']}" if a["note"] else "") + RESET)
            self.emit(f"  {DIM}/attach rm <id> removes one{RESET}")
            return

        verb, _, tail = rest.partition(" ")
        if verb in {"rm", "remove"} and tail.strip().isdigit():
            try:
                name = self.store.detach(int(tail.strip()), self.me)
            except StoreError as exc:
                self.emit(f"{RED}{exc}{RESET}")
                return
            self.emit(f"{DIM}removed {name}{RESET}")
            return

        try:
            aid = self.store.attach(self.topic_id, rest, self.me)
        except StoreError as exc:
            self.emit(f"{RED}{exc}{RESET}")
            return
        a = self.store.q1("SELECT * FROM attachments WHERE id = ?", (aid,))
        how = ("inlined into every seat's next prompt" if a["is_text"]
               else "binary — the seats get its path, not its contents")
        self.emit(f"{DIM}attached {a['name']} ({a['bytes']:,}B) — {how}{RESET}")

    def _agenda_points(self) -> list[str]:
        """The agenda as a list. Empty when the brief is only an echo of the
        title, because `/new` seeds it that way and a bare title is not one."""
        from .store import agenda_points
        return agenda_points(self.store.topic(self.topic_id))

    def _show_agenda(self, note: str = "") -> None:
        """The invitation: what this meeting is called, and what it is to settle.

        Printing "agenda set" and nothing else was how two calls could silently
        overwrite each other -- the confirmation looked identical whether a point
        had been added or the previous one thrown away. Showing the whole thing
        after every change makes that impossible to miss.
        """
        topic = self.store.topic(self.topic_id)
        points = self._agenda_points()
        self.emit("")
        self.emit(f"  {BOLD}{topic['title'].strip()}{RESET}  {DIM}({topic['slug']}){RESET}")
        if points:
            self.emit(f"  {DIM}agenda{RESET}")
            for i, pt in enumerate(points, 1):
                self.emit(f"    {BOLD}{i}.{RESET} {pt}")
        else:
            self.emit(f"  {DIM}no agenda yet — the seats get the title alone{RESET}")
        if note:
            self.emit(f"  {DIM}{note}{RESET}")
        self.emit("")

    def _agenda(self, rest: str) -> None:
        """What the meeting is for, as distinct from what it is called.

        A one-line topic gets a one-line answer from every seat. Given the
        points to settle, they work through them -- and the agenda ends up in
        the minutes as the thing the council was actually asked.

        Typing `/topic agenda X` twice adds two points rather than replacing the
        first, because that is what people do: an agenda is built up a line at a
        time, the way you would write a meeting invitation. Replacing wholesale
        is the rarer act, so it is the one that has to be asked for by name.
        """
        if not self._require_topic():
            return
        title = (self.store.topic(self.topic_id)["title"] or "").strip()
        points = self._agenda_points()
        rest = rest.strip()

        if not rest:
            self._show_agenda("/topic agenda <point> adds one · ; for several · "
                              "/topic agenda clear starts over")
            return

        if rest in {"clear", "none"}:
            self.store.set_brief(self.topic_id, title, self.me)
            self._show_agenda("cleared")
            return

        first, _, tail = rest.partition(" ")
        if first in {"drop", "rm"} and tail.strip().isdigit():
            n = int(tail.strip())
            if not 1 <= n <= len(points):
                self.emit(f"{RED}no point {n} — there are {len(points)}{RESET}")
                return
            dropped = points.pop(n - 1)
            self.store.set_brief(self.topic_id,
                                 "\n".join("- " + p for p in points) or title, self.me)
            self._show_agenda(f"dropped: {dropped}")
            return

        if first == "set":
            rest = tail.strip()
            points = []
            if not rest:
                self.emit(f"{RED}usage: /topic agenda set <the whole agenda>{RESET}")
                return
        elif rest.startswith("+"):
            rest = rest[1:].strip()      # kept: `+` used to be how you added
            if not rest:
                self.emit(f"{RED}usage: /topic agenda +<the point to add>{RESET}")
                return

        # The session gives you one line to type on, so `;` is how several
        # points get entered at once.
        added = [seg.strip() for seg in rest.split(";") if seg.strip()]
        points.extend(added)
        self.store.set_brief(self.topic_id, "\n".join("- " + p for p in points), self.me)
        word = "point" if len(added) == 1 else "points"
        self._show_agenda(f"added {len(added)} {word} — the seats "
                          f"see this from their next turn")

    def _mode(self, rest: str) -> None:
        if not self._require_topic():
            return
        words = rest.split()
        mode = words[0] if words else ""
        if mode not in {"debate", "discuss", "work"}:
            self.emit(f"  mode is {BOLD}{self.store.topic(self.topic_id)['mode']}{RESET}   "
                      f"{DIM}/topic mode debate | discuss | work <manager>{RESET}")
            return

        if mode == "work":
            # A role only exists where it means something. In discussion there is
            # no manager to be -- everyone argues on equal footing -- so the role
            # is granted when the topic becomes work, and taken back when it stops
            # being work, rather than lingering as a title nobody uses.
            manager = words[1] if len(words) > 1 else None
            if not manager:
                self.emit(f"{RED}work needs a manager: /topic mode work <agent>{RESET}")
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

    def _capability(self, rest: str) -> None:
        """Grant or revoke a seat's ability to change files.

        Kept as its own gesture rather than folded into /seats or /mode, because
        it is the one escalation here that matters: a seat woken by a daemon with
        write access is a different risk class from one you are watching. It still
        takes two keys -- this, and an approved task on a work topic -- so
        granting it does not by itself let anything run.
        """
        if not self._require_topic():
            return
        words = rest.split()
        if len(words) < 2 or words[1] not in {"execute", "deliberate"}:
            self.emit(f"{DIM}/capability <agent> execute <dir>   let it edit files "
                      f"there{RESET}")
            self.emit(f"{DIM}/capability <agent> deliberate      take that back"
                      f"{RESET}")
            for s_ in self.store.seats(self.topic_id):
                if s_["kind"] in {"human", "external"}:
                    continue
                cfg_ = json.loads(dict(self.store.agent(s_["agent"]))["driver_cfg"])
                where = cfg_.get("cwd", "")
                self.emit(f"  {s_['agent']:<12} {cfg_.get('capability', 'deliberate')}"
                          f"  {DIM}{where}{RESET}")
            return

        agent, level = words[0], words[1]
        try:
            meta = self.store.agent(agent)
        except StoreError as exc:
            self.emit(f"{RED}{exc}{RESET}")
            return
        cfg = json.loads(meta["driver_cfg"])

        if level == "deliberate":
            cfg.pop("capability", None)
            self.store.add_agent(agent, meta["kind"], display=meta["display"],
                                 driver=meta["driver"], driver_cfg=cfg)
            self.emit(f"{DIM}{agent} can no longer change files{RESET}")
            return

        where = " ".join(words[2:]).strip() or cfg.get("cwd") or os.getcwd()
        path = Path(where).expanduser()
        if not path.is_dir():
            self.emit(f"{RED}{path} is not a directory{RESET}")
            self.emit(f"{DIM}name the repo it should work in: /capability {agent} "
                      f"execute C:/path/to/repo{RESET}")
            return
        cfg["capability"] = "execute"
        cfg["cwd"] = str(path.resolve())
        self.store.add_agent(agent, meta["kind"], display=meta["display"],
                             driver=meta["driver"], driver_cfg=cfg)
        self.emit(f"{YELLOW}{agent} may now edit files in {path.resolve()}{RESET}")
        self.emit(f"{DIM}only for a task you have approved on a work topic, and only "
                  f"in its own git worktree{RESET}")

    def _manager(self, rest: str) -> None:
        if not self._require_topic():
            return
        if self.store.topic(self.topic_id)["mode"] != "work":
            self.emit(f"{DIM}no manager in a discussion — roles exist on work topics."
                      f" /topic mode work <agent> to switch.{RESET}")
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

    def _seat_named(self, name: str) -> str | None:
        """A seat on this topic, however the name was typed.

        `@Santa` is what a chat gives you -- Telegram completes a mention with
        its `@` attached -- while the seat is `Santa`. Everywhere else that takes
        an agent name already strips it; `/nudge` did not, and the mismatch
        surfaced as a wake that raised deep inside a worker thread.
        """
        want = name.strip().lstrip("@").strip()
        here = [s["agent"] for s in self.store.seats(self.topic_id)]
        for seat in here:
            if seat.lower() == want.lower():
                return seat
        self.emit(f"{RED}no seat called `{want}` on this topic — "
                  f"{', '.join(here)}{RESET}")
        return None

    def _nudge(self, agent: str) -> None:
        if not self._require_topic():
            return
        import asyncio

        from .drivers.registry import build_drivers
        from .supervisor import Supervisor

        agent = self._seat_named(agent)
        if agent is None:
            return

        def work() -> None:
            store = connect(self.db)
            try:
                r = asyncio.run(Supervisor(store, build_drivers(store, [agent]))
                                .wake_seat(self.topic_id, agent))
                if not r.ok:
                    self.emit(f"\n{RED}{agent}: {r.detail}{RESET}")
            except Exception as exc:
                # This thread dying used to take the only notice with it: the
                # room said "waking Santa..." and then nothing, for ever.
                self.emit(f"\n{RED}{agent}: {exc}{RESET}")
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
              f"mooting show {self.topic['slug']}{RESET}")
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
