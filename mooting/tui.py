"""`mooting tui` -- one screen where the council talks and the team works.

The REPL (`mooting console`) is a scrolling log: fine for a conversation, poor for
work, where "what is every seat doing and where has each task got to" is a
*state* question and a log answers it badly. This is the same board with the
state made visible -- transcript, seats, tasks and proposals side by side, and
one input that both talks and rules.

## It is a view, not a second application

Every command still goes through `Console.handle()`. The TUI only swaps where
output lands (`Console.emit`) and how long-running work is started. Two dispatch
paths would drift within a week, and then `/approve` would mean something subtly
different depending on which surface you were sitting in.

## Threading, which the REPL got away with and this cannot

The REPL runs a poller thread and a supervisor thread that both `print()`.
Textual owns the event loop and its widgets are not thread-safe, so:

* polling is a `set_interval` on the app, not a thread;
* the supervisor runs as a Textual **worker** on Textual's own loop -- the
  drivers are already async, so `asyncio.run()` is not just unnecessary here, it
  would refuse to start inside a running loop;
* the UI and the supervisor hold **separate** `Store` connections, so a slow
  query behind the table redraw cannot sit on the write lock while a seat is
  trying to post.

That last point is only safe because no `tx()` block ever awaits. See `Store.tx`.
"""

from __future__ import annotations

import json
import os
import uuid
import logging
from pathlib import Path

from rich.markdown import Markdown
from rich.markup import escape
from rich.padding import Padding
from rich.style import Style
from rich.text import Text
from textual.color import Color
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.containers import Vertical as _V  # noqa: F401  (re-exported below)
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label,
                             OptionList, RichLog, Static)
from textual.widgets.option_list import Option

from .console import Console
from .store import StoreError, connect

log = logging.getLogger("mooting.tui")


#: One colour per seat, picked from its name so it is the same in every session
#: and on every screen. In a four-way argument the author matters more than the
#: message kind, and scanning for "what did codex say" should be a colour, not a
#: read. Chosen to stay apart on both dark and light terminals.
#: Hex, not ANSI names: `Color.parse("bright_cyan")` fails, so every blended tint
#: came out empty and the backgrounds silently did nothing. Hex is understood by
#: both Rich (for the text) and Textual's colour maths (for the tint behind it).
SEAT_COLOURS = ("#7dcfff",   # sky
                "#9ece6a",   # green
                "#e0af68",   # amber
                "#bb9af7",   # violet
                "#f7768e",   # rose
                "#2ac3de",   # teal
                "#ff9e64",   # orange
                "#73daca")   # mint


#: How much of a seat's colour to mix into the background behind its messages.
#: Very low on purpose: the point is to group a reply, not to highlight it. 0.13
#: read as a coloured block rather than a tint; this is a hint you notice only
#: when scanning for who said what.
TINT = 0.055


def tint_for(colour: str, base: Color) -> str:
    """The seat's colour blended into the real background, as hex.

    Blended rather than set outright, so the band sits *under* the text at every
    theme instead of fighting it -- and so a light terminal gets a light tint
    rather than the same dark one.
    """
    try:
        return base.blend(Color.parse(colour), TINT).hex
    except Exception:          # an unparseable colour must not cost you the message
        return ""


def mk(markup: str) -> Text:
    """A styled line of the TUI's own. Explicit, because a plain string handed to
    `write_line` is now treated as literal text -- see the note there."""
    return Text.from_markup(markup)


def field(row, key, default=None):
    """Read a column that may not be there.

    Rows arrive from a few places -- a full sqlite3.Row, a dict built by a
    caller -- and sqlite3.Row has no .get(). A renderer should not fall over
    because one optional column is absent.
    """
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def seat_colours(names) -> dict:
    """Assign by position among the seats present, not by hashing the name.

    Hashing looked fine until two of this council's own seats -- codex and agy --
    landed on the same cyan. Distinctness is the entire point, so it is allocated
    rather than hoped for: sorted so it stays the same every time you open the
    topic, and only wrapping past eight seats, which no council here has.
    """
    return {name: SEAT_COLOURS[i % len(SEAT_COLOURS)]
            for i, name in enumerate(sorted(names))}


class Board(Console):
    """Console's command set, wired to a widget instead of stdout."""

    def __init__(self, db, topic, me, app: "MootApp") -> None:
        super().__init__(db, topic, me)
        self.app_ref = app
        self.emit = app.write_line

    # Long-running work becomes a Textual worker rather than a thread.
    def _run(self, _: str = "") -> None:
        # The base class guards; overriding it dropped the guard, so /run with no
        # topic started a worker that failed instead of simply saying so.
        if not self._require_topic():
            return
        if self.driving.is_set():
            self.emit("[dim]already driving[/dim]")
            return
        self.driving.set()
        self.app_ref.drive()
        self.emit(f"[dim]· council thinking at effort {self.effort()}[/dim]")

    def _nudge(self, agent: str) -> None:
        if not self._require_topic():
            return
        if not agent:
            self.emit("[red]usage: /nudge <agent>[/red]")
            return
        agent = self._seat_named(agent)
        if agent is None:
            return
        self.app_ref.nudge(agent)
        self.emit(f"[dim]waking {agent}…[/dim]")

    def on_topic_change(self) -> None:
        """/new and /topic move the whole view, not just a variable."""
        self.app_ref.rebind_topic()



class ModelPicker(ModalScreen):
    """Pick the model a seat runs on.

    Only agy can enumerate its own models; the rest take `--model <name>` and
    offer no way to ask. So the list is a convenience and the text box is the
    contract -- a picker that only offered a guessed list would be wrong the week
    a new model shipped.
    """

    BINDINGS = [("escape", "dismiss_picker", "Cancel")]
    CSS = """
    ModelPicker { align: center middle; }
    #picker { width: 60; height: auto; max-height: 24; padding: 1 2;
              background: $panel; border: round $accent; }
    #picker Label { padding: 0 0 1 0; }
    #models { height: auto; max-height: 12; }
    """

    def __init__(self, seat: str, kind: str, current: str | None) -> None:
        super().__init__()
        self.seat, self.kind, self.current = seat, kind, current

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Label(Text.assemble(
                (self.seat, "bold"), (f"  runs {self.kind}", "dim"),
                (f"\nmodel: {self.current or 'whatever the CLI defaults to'}", "dim")))
            yield OptionList(Option("(loading models…)", id="__wait__"), id="models")
            yield Input(placeholder="…or type a model name and press Enter",
                        id="model_name")

    def on_mount(self) -> None:
        self.load_models()

    @work
    async def load_models(self) -> None:
        from .models import available
        found = await available(self.kind)
        lst = self.query_one("#models", OptionList)
        lst.clear_options()
        lst.add_options([Option("default — let the CLI choose", id="")]
                        + [Option(m, id=m) for m in found])
        lst.highlighted = 0

    def on_option_list_option_selected(self, event) -> None:
        self.dismiss(event.option.id or "")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_dismiss_picker(self) -> None:
        self.dismiss(None)          # None means "changed nothing"


class MootApp(App):
    CSS = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    /* Both panes need an explicit width. Without one the transcript sizes to its
       content, overruns the sidebar, and the two paint over each other. */
    #transcript { width: 1fr; border: round $primary; padding: 0 1; }
    #side { width: 42; }
    #seats { height: 45%; }
    #work { height: 1fr; }
    #seats, #work { border: round $secondary; }
    /* Thin, muted scrollbars: the defaults are wide and bright enough to read as
       a UI element in their own right. */
    #transcript, #seats, #work {
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 0;
        scrollbar-background: $surface;
        scrollbar-color: $panel;
        scrollbar-color-hover: $secondary;
    }
    #status { height: 1; background: $boost; color: $text; padding: 0 1; }
    /* Grows only while you are typing a command, so it never costs space when
       you are reading. */
    #hint { height: auto; max-height: 10; background: $panel; border: none;
            padding: 0 1; display: none; }
    #hint.showing { display: block; }
    Input { border: round $accent; }
    /* When the council is blocked on you, the box you would type into is the
       thing that should be shouting -- not a line elsewhere on the screen. */
    Input.waiting { border: round $warning; background: $warning 8%; }
    """
    BINDINGS = [
        ("ctrl+r", "run", "Run council"),
        ("ctrl+s", "stop", "Stop"),
        ("ctrl+t", "tasks", "Tasks"),
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, db: Path | str | None, topic: str, me: str) -> None:
        super().__init__()
        self.db = db
        self.board = Board(db, topic, me, self)
        #: Separate connection for the supervisor; see the module docstring.
        self.drive_store = connect(db)
        self.cursor = 0
        #: Set when the council is blocked on you; cleared when you answer.
        self._waiting: str | None = None
        #: Identifies this session to the drive lock. Per process, because that
        #: is what a browser tab is when the session is served.
        self._session_id = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._palette: dict[str, str] = {}
        self._tints: dict[str, str] = {}
        #: What you have typed, newest last, walked with ↑/↓ when no hint is open.
        #: Loaded from disk, because a history that starts empty every time you
        #: open the app is not a history -- pressing up in a fresh session did
        #: nothing, which is indistinguishable from the keys not working.
        self._history: list[str] = []
        self._history_at: int | None = None
        self._history_file = Path(self.board.store.path).parent / "input-history"

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            yield RichLog(id="transcript", wrap=True, markup=True, highlight=False)
            with Vertical(id="side"):
                # Rows are clickable: a seat opens its model picker, a proposal
                # or task opens itself in the transcript.
                yield DataTable(id="seats", cursor_type="row", zebra_stripes=True)
                yield DataTable(id="work", cursor_type="row", zebra_stripes=True)
        yield OptionList(id="hint")
        yield Static("", id="status")
        yield Input(placeholder="type to speak · @agent to ask one seat · /help",
                    id="say")
        yield Footer()

    def on_mount(self) -> None:
        seats = self.query_one("#seats", DataTable)
        seats.add_columns("seat", "state", "turns")
        work = self.query_one("#work", DataTable)
        work.add_columns("#", "what", "state")

        self.rebind_topic()

        # Paint once now, not only on the first tick -- otherwise every pane sits
        # empty for a second on open, which reads as "nothing here" exactly when
        # someone is looking to see what state the council is in.
        self._load_history()
        # A previous session may have been killed mid-round; do not inherit its
        # ghosts and show three seats thinking forever.
        freed = self.board.store.sweep_stale_wakes()
        if freed:
            self.write_line(mk(f"[dim]cleared {freed} wake(s) left running by "
                               f"an earlier session[/dim]"))
        self.refresh_board()
        self.set_interval(1.0, self.refresh_board)
        self.set_interval(300.0, lambda: self.board.store.sweep_stale_wakes())
        self.query_one("#say", Input).focus()

    # ------------------------------------------------------------- rendering

    def write_line(self, item) -> None:
        """Console.emit target: a markup string, a Rich renderable, or a list.

        Console styles its output with ANSI escapes, because that is what a plain
        terminal understands. A Rich widget does not: the escapes went in as
        literal control characters, which is what put black boxes behind the help
        text and corrupted the lines around it. Anything carrying an escape is
        decoded; anything else is treated as Rich markup, which is what the TUI's
        own strings use.
        """
        try:
            log = self.query_one("#transcript", RichLog)
        except Exception:
            # Something wrote while a modal was up. Losing a line is better than
            # taking the app down for it.
            return
        for part in (item if isinstance(item, list) else [item]):
            if isinstance(part, str):
                # A plain string is never Rich markup here. RichLog parses one as
                # markup, and board content is agent-written: a message containing
                # `[/INST]` raised MarkupError and killed the app, while `[draft]`
                # in the task list was silently deleted from the screen. Anything
                # the TUI itself wants styled is built with mk() or as a Rich
                # object, so this only ever converts genuine text.
                part = Text.from_ansi(part) if "\x1b" in part else Text(part)
            log.write(part)

    def notify_turn(self, why: str) -> None:
        """Ring the terminal and flag the status bar when the council needs you.

        A council that waits silently is a council you have to sit and watch. The
        bell is what most terminals turn into a taskbar flash, which is the point
        -- you should be able to look away.
        """
        self._waiting = why
        try:
            self.bell()
        except Exception:
            pass

    def colour_for(self, name: str) -> str:
        return self._palette.get(name, "cyan")

    def _render_proposal(self, pr) -> list:
        """A proposal is the one thing only you can close, so it is announced as
        something to act on -- and it carries its own body.

        The banner used to be the title alone, which reads as a notification that
        a decision exists somewhere else. You cannot rule on a title.
        """
        head = Text.assemble(("◆ proposal ", "bold yellow"),
                             (f"#{pr['id']} ", "bold yellow"),
                             (pr["title"], "yellow"),
                             (f"   by {pr['author']}", "dim"))
        act = Text(f"   /approve {pr['id']} <why>  ·  /reject {pr['id']} <why>"
                   f"  ·  /proposals {pr['id']} to re-read",
                   "bold yellow")
        body = pr["body"].strip()
        out = ["", head]
        if body:
            out.append(Markdown(body))
        votes = self.board.store.votes(pr["id"])
        if votes:
            for v in votes:
                mark = {"support": "+", "object": "!", "abstain": "~"}.get(v["stance"], "?")
                why = " ".join(v["rationale"].split())[:140]
                out.append(Text.assemble(
                    (f"   {mark} {v['agent']} {v['stance']}",
                     f"bold {self.colour_for(v['agent'])}"),
                    (f" — {why}" if why else "", "dim")))
        out.append(act)
        return out

    def _quoted_line(self, m):
        """A one-line echo of what this message is replying to.

        Enough to know what is being answered without scrolling back, and no more
        -- a full quote of a long argument would bury the reply to it.
        """
        ref = field(m, "reply_to")
        if not ref:
            return None
        row = self.board.store.quoted(int(ref))
        if row is None:
            return None
        preview = " ".join(row["body"].split())[:80]
        return Text.assemble(("│ ", "dim"),
                             (row["author"], f"dim {self.colour_for(row['author'])}"),
                             (f": {preview}…", "dim italic"))

    def base_colour(self) -> Color:
        """The background actually behind the transcript, so tints follow the theme."""
        try:
            bg = self.query_one("#transcript").background_colors[1]
            if bg is not None:
                return bg
        except Exception:
            pass
        return Color(24, 24, 32)

    def tint_style(self, name: str) -> Style:
        hexcol = self._tints.get(name, "")
        return Style(bgcolor=hexcol) if hexcol else Style()

    def _render_message(self, m) -> list:
        """Header line plus the body as markdown.

        Agents write markdown -- headings, bold, lists, fenced code -- and showing
        the source characters wastes the structure they went to the trouble of
        producing. Rendering it is also why the body must not go through Rich
        *markup*: an agent writing `[balance.json]` means the filename, not a
        style tag.
        """
        # The author's colour, not the message kind's: in a four-way argument you
        # are scanning for who spoke. Kind survives as a dim tag, except for the
        # two that are about the board rather than a person.
        system = m["kind"] in {"system", "ruling"}
        if system:
            colour = "dim" if m["kind"] == "system" else "bold green"
        else:
            colour = f"bold {self.colour_for(m['author'])}"
        tag = "" if m["kind"] == "say" else f"  [{m['kind']}]"
        body = m["body"].strip()

        # System notes and rulings are about the board rather than a person, so
        # they stay untinted -- that is what makes them read as not-a-seat.
        bg = Style() if system else self.tint_style(m["author"])
        # The id is what /quote takes, so it has to be visible without being loud.
        ident = field(m, "id")
        header = Text.assemble((m["author"], colour), (tag, "dim"),
                               (f"   #{ident}" if ident else "", "dim"), style=bg)
        pieces = []
        quoted = self._quoted_line(m)
        if quoted is not None:
            pieces.append(Padding(quoted, (0, 1), style=bg))
        rendered = Markdown(body) if body else Text("")
        pid = field(m, "proposal_id")
        if m["kind"] == "propose" and pid:
            pieces.append(Padding(
                Text(f"◆ proposal #{pid} — /approve {pid} <why> to rule on it",
                     "bold yellow"), (0, 1), style=bg))
        # Padding, not a bare style: it extends the band across the full width, so
        # a reply is one block rather than a ragged right edge following the text.
        return ["", Padding(header, (0, 1), style=bg), *pieces,
                Padding(rendered, (0, 1), style=bg)]

    def _render_ask(self, asker: str, question: str) -> list:
        return ["",
                Text.assemble(("❓ ", "magenta"),
                              (asker, f"bold {self.colour_for(asker)}"),
                              (" is asking you", "bold magenta")),
                Markdown(question.strip()),
                Text("   ↓ Type your answer in the box at the bottom and press "
                     "Enter. That clears the question and the council carries on.",
                     "bold yellow")]

    def refresh_board(self) -> None:
        """One tick: drain new events into the log, then repaint state."""
        if len(self.screen_stack) > 1:
            # A modal is on top, and query_one searches the *active* screen --
            # which has none of these widgets. The timer kept firing behind the
            # model picker and raised NoMatches a second after it opened.
            return
        store = self.board.store
        if self.board.topic_id is None:
            self.query_one("#seats", DataTable).clear()
            self.query_one("#work", DataTable).clear()
            self.query_one("#status", Static).update(
                "no topic  |  /topic new <what you want to discuss>  |  /help")
            return
        try:
            for ev in store.events_since(self.cursor, self.board.topic_id):
                self.cursor = ev.id
                line = self._render_event(store, ev)
                if line:
                    self.write_line(line)
            self._paint_seats()
            self._paint_work()
            self._paint_status()
        except Exception as exc:                    # a redraw must never kill the app
            self.write_line(mk(f"[red]refresh: {escape(str(exc))}[/red]"))

    def _render_event(self, store, ev) -> str | None:
        if ev.kind == "message":
            row = store.q1("SELECT * FROM messages WHERE id = ?",
                           (ev.payload.get("message_id"),))
            if row is None:
                return None
            if row["author"] == self.board.me:
                # Your own words used to be dropped here, on the grounds that you
                # had just typed them. But only `/` lines are echoed, so a plain
                # reply vanished -- the one message in the room with no author,
                # no colour and no place in the order. A transcript of a
                # discussion you took part in has to include you.
                return self._render_message(row)
            if self.board.me in (ev.payload.get("mentions") or []):
                self.notify_turn(f"{row['author']} asked you a question")
                return self._render_ask(row["author"], row["body"])
            return self._render_message(row)
        if ev.kind == "proposal" and ev.payload.get("action") == "opened":
            pid = ev.payload["proposal_id"]
            self.notify_turn(f"proposal #{pid} needs your ruling")
            try:
                return self._render_proposal(store.proposal(pid))
            except Exception as exc:
                log.warning("could not render proposal %s: %s", pid, exc)
                return mk(f"[yellow]◆ proposal #{pid} — /proposals {pid}[/yellow]")
        if ev.kind == "decision":
            return mk(f"\n[green]✓ proposal #{ev.payload['proposal_id']} "
                      f"{ev.payload['status']} by {escape(ev.actor)}[/green]")
        if ev.kind == "task":
            return mk(f"[dim]· task #{ev.payload.get('task_id')} "
                      f"{ev.payload.get('action')} ({escape(ev.actor)})[/dim]")
        if ev.kind == "topic" and ev.payload.get("action") == "paused":
            return mk(f"\n[dim]— paused: "
                      f"{escape(str(ev.payload.get('note', '')))}[/dim]")
        return None

    def _paint_seats(self) -> None:
        table = self.query_one("#seats", DataTable)
        thinking = {w["agent"]: w["secs"] for w in
                    self.board.store.active_wakes(self.board.topic_id)}
        table.clear()
        for s in self.board.store.seats(self.board.topic_id):
            owed = len(self.board.store.open_mentions(self.board.topic_id, s["agent"]))
            if s["agent"] in thinking:
                state = f"thinking {thinking[s['agent']]}s"
            elif owed:
                state = f"asked ×{owed}"
            else:
                state = s["state"]
            # Same colour as in the transcript, so the sidebar is a legend.
            who = Text(s["agent"], style="bold" if s["agent"] == self.board.me
                       else f"bold {self.colour_for(s['agent'])}")
            if s["kind"] in {"human", "external"}:
                # Neither column means anything for a person. A turn budget is a
                # cost control on metered CLIs -- yours is not metered and your
                # posts never spend one, so 0/4 implied a limit that does not
                # exist. And idle/thinking/capped describe a subprocess, not you.
                # What is true of a person here is what they said, and whether
                # the room is waiting on them.
                said = self.board.store.q1(
                    "SELECT COUNT(*) c FROM messages WHERE topic_id = ? AND author = ? "
                    "AND kind != 'system'", (self.board.topic_id, s["agent"]))["c"]
                budget = f"{said} said" if said else "—"
                state = f"asked ×{owed}" if owed else "—"
            else:
                # The cap that actually binds. A seat speaks at most once a
                # round, so a turn allowance above the round count can never be
                # spent -- and showing 0/13 on a topic that stops at 10 rounds
                # is a number the seat will never reach, with nothing to say why.
                rounds = self.board.store.topic(self.board.topic_id)["max_rounds"]
                budget = f"{s['turns_used']}/{min(s['max_turns'], rounds)}"
            table.add_row(who, state, budget)

    def _paint_work(self) -> None:
        """Tasks on a work topic, otherwise open proposals. Blocked reasons are
        shown rather than hidden -- a blocked task is the thing most likely to be
        waiting on a person."""
        table = self.query_one("#work", DataTable)
        table.clear()
        store, tid = self.board.store, self.board.topic_id
        if store.topic(tid)["mode"] == "work":
            for t in store.tasks(tid):
                table.add_row(str(t["id"]), t["title"][:30], t["status"])
                if t["status"] == "blocked" and t["result"]:
                    # Truncated here on purpose: the full reason is already a system
                    # message in the transcript. This row exists so a blocked task
                    # is not silently just a status word.
                    table.add_row("", f"↳ {t['result'][:30]}", "")
        else:
            for p in store.proposals(tid):
                table.add_row(str(p["id"]), p["title"][:30], p["status"])

    def _paint_status(self) -> None:
        b = self.board
        waiting = b.pending_asks()
        asks = len(waiting)
        props = len(b.store.proposals(b.topic_id, status="open"))
        if not asks and not props:
            self._waiting = None
        elif not self._waiting:
            # `notify_turn` only fires on a live event, so reopening a session
            # that is already waiting on you showed no banner at all -- just the
            # word "idle", which reads as "nothing is happening" when in fact the
            # council has stopped and cannot continue until you answer. Recover
            # it from the board instead of from an event that already happened.
            self._waiting = (f"{waiting[0]['asker']} is waiting on your answer"
                             if waiting else "a proposal is waiting on your ruling")

        # The clearest place to say "answer here" is the box you would type in.
        box = self.query_one("#say", Input)
        if waiting:
            box.placeholder = (f"▶ type your answer to {waiting[0]['asker']} here, "
                               f"then Enter")
            box.add_class("waiting")
        elif props:
            box.placeholder = "▶ /approve <id> <why>  or  /reject <id> <why>"
            box.add_class("waiting")
        else:
            box.placeholder = "type to speak · @agent to ask one seat · /help"
            box.remove_class("waiting")

        topic = b.store.topic(b.topic_id)
        bits = [f"round {topic['round'] + 1}/{topic['max_rounds']}",
                f"effort {b.effort()}",
                # "idle" is true of the subprocesses and misleading about the
                # room: a council stopped for an answer is not idling, it is
                # blocked on you.
                ("driving" if b.driving.is_set()
                 else "waiting on you" if (asks or props) else "idle"),
                f"auto {'on' if b.auto else 'off'}"]
        if asks:
            bits.append(f"[magenta]{asks} question(s) for you[/magenta]")
        if props:
            bits.append(f"[yellow]{props} awaiting your sign-off[/yellow]")
        line = "  |  ".join(bits)
        status = self.query_one("#status", Static)
        if self._waiting:
            # Loud on purpose: the council has stopped and is waiting on you.
            status.update(f"[black on bright_magenta] ▶ YOUR TURN — {self._waiting} "
                          f"[/black on bright_magenta]  {line}")
        else:
            status.update(line)

    def rebind_topic(self) -> None:
        """Repoint every pane at whatever topic the board is now on -- including
        no topic at all, which is a legitimate place to be sitting."""
        log = self.query_one("#transcript", RichLog)
        log.clear()
        if self.board.topic_id is None:
            self.title = "Mooting"
            self.sub_title = "no topic"
            log.write(mk("[dim]Nothing on the board yet.[/dim]"))
            log.write("")
            log.write(mk("[bold]Start one[/bold] — just say what you want to discuss:"))
            log.write(mk("  [cyan]/topic new the workflow optimization in agentic AI development"
                      "[/cyan]"))
            log.write("")
            log.write(mk("[dim]Then type any detail the council needs, and [/dim]"
                      "[cyan]/run[/cyan][dim].[/dim]"))
            log.write(mk("[dim]Answering is just typing. [/dim][cyan]@agent <question>"
                      "[/cyan][dim] asks one seat. [/dim][cyan]/help[/cyan]"
                      "[dim] for the rest.[/dim]"))
            self.cursor = self.board.store.head()
            self.refresh_board()
            return
        t = self.board.store.topic(self.board.topic_id)
        self._palette = seat_colours(
            s["agent"] for s in self.board.store.seats(self.board.topic_id))
        base = self.base_colour()
        self._tints = {name: tint_for(col, base) for name, col in self._palette.items()}
        self.title = t["title"]
        self.sub_title = f"{t['slug']} · {t['mode']}"
        for m in self.board.store.transcript(self.board.topic_id)[-40:]:
            self.write_line(self._render_message(m))
        for a in self.board.pending_asks():
            self.write_line(self._render_ask(a["asker"], a["question"]))
        # A proposal raised before you opened the topic was waiting on you
        # invisibly: only live arrivals were ever announced.
        for pr in self.board.store.proposals(self.board.topic_id, status="open"):
            self.write_line(self._render_proposal(pr))
        # Fresh cursor, or the first tick would replay the new topic's history.
        self.cursor = self.board.store.head()
        self.refresh_board()

    # -------------------------------------------------------------- behaviour

    def _hint_rows(self, text: str) -> list[tuple[str, str]]:
        """(what would be typed, how it reads) for the text so far."""
        rows: list[tuple[str, str]] = []
        # Once there is a space you have chosen the command and are writing its
        # arguments. Keeping the list open past that point meant Enter could
        # "accept" a completion over `/rm t yes` and throw the arguments away.
        # Raw, not stripped: a *trailing* space is the start of arguments too,
        # and stripping it reopened the list the moment a completion was taken.
        if " " in text:
            return rows
        if text.startswith("/"):
            typed = text.split(" ")[0]
            for _heading, entries in self.board.HELP:
                for cmd, why in entries:
                    name = cmd.split(" ")[0]
                    if name.startswith("/") and name.startswith(typed):
                        rows.append((cmd, why))
        elif text.startswith("@") and " " not in text:
            for name in self.board.seat_names():
                if name.startswith(text[1:]):
                    rows.append((f"@{name}", "ask this seat directly"))
        # Alphabetical. Grouping by purpose is right for /help, where you are
        # reading; here you are looking for one known name, and a list you have to
        # scan for it is slower than one you can jump down.
        rows.sort(key=lambda r: r[0])
        return rows

    def on_data_table_row_selected(self, event) -> None:
        """A panel row is a thing, so clicking it should open that thing."""
        table_id = event.data_table.id
        try:
            cell = str(event.data_table.get_cell_at((event.cursor_row, 0)))
        except Exception as exc:
            log.warning("row click ignored: %s", exc)
            return
        if table_id == "seats":
            self._pick_model(cell)
        elif table_id == "work":
            self._open_work_row(cell)

    def _pick_model(self, seat: str) -> None:
        if not seat or seat == self.board.me:
            return
        try:
            meta = self.board.store.agent(seat)
        except StoreError:
            return
        cfg = json.loads(meta["driver_cfg"])

        def chosen(value) -> None:
            if value is None:          # escaped
                return
            cfg2 = json.loads(self.board.store.agent(seat)["driver_cfg"])
            if value:
                cfg2["model"] = value
            else:
                cfg2.pop("model", None)
            self.board.store.add_agent(seat, meta["kind"], display=meta["display"],
                                       driver=meta["driver"], driver_cfg=cfg2)
            self.write_line(mk(f"[dim]{seat} → model "
                               f"{value or 'default'}; from its next turn[/dim]"))

        self.push_screen(ModelPicker(seat, meta["kind"], cfg.get("model")), chosen)

    def _open_work_row(self, first_cell: str) -> None:
        """Bring the thing itself into the transcript, rather than making someone
        retype an id they can already see."""
        ref = first_cell.strip().lstrip("#")
        if not ref.isdigit():
            return
        if self.board.store.topic(self.board.topic_id)["mode"] == "work":
            self.board.handle("/tasks")
            return
        # `/proposals <id>` is the console's renderer: plain strings, because the
        # console has no way to draw anything else. Opening a proposal here went
        # through it, so a body full of tables and bold arrived as raw markdown
        # characters -- in the one place a person is deciding something.
        try:
            pr = self.board.store.proposal(int(ref))
        except (StoreError, ValueError):
            self.board.handle(f"/proposals {ref}")
            return
        self.write_line(self._render_proposal(pr))

    def on_input_changed(self, event: Input.Changed) -> None:
        """Offer what the half-typed thing could become, arrow-navigable.

        A hint you cannot browse still requires knowing what exists. `/` alone
        lists everything, ↑/↓ walk it, and Tab takes the highlighted one -- while
        the cursor stays in the box, because being thrown into a menu to pick a
        command and back again is worse than typing it.
        """
        rows = self._hint_rows(event.value)
        hint = self.query_one("#hint", OptionList)
        hint.clear_options()
        if not rows:
            hint.remove_class("showing")
            return
        hint.add_options([
            Option(Text.assemble((f"{cmd:<26}", "bold cyan"), (why, "dim")), id=cmd)
            for cmd, why in rows
        ])
        hint.highlighted = 0
        hint.add_class("showing")

    #: Enough to reach for something said a while ago; small enough to stay a file
    #: you could read.
    HISTORY_MAX = 300

    def _load_history(self) -> None:
        try:
            lines = self._history_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        if not lines and self.board.topic_id is not None:
            # Nothing saved yet, but the board remembers what you said here, and
            # that is the more useful thing to reach for on a first run.
            lines = [m["body"] for m in self.board.store.transcript(self.board.topic_id)
                     if m["author"] == self.board.me and m["kind"] != "system"]
        self._history = [ln for ln in lines if ln.strip()][-self.HISTORY_MAX:]

    def _save_history(self) -> None:
        # One entry per line, so anything containing a newline is skipped rather
        # than silently reappearing later as several separate entries.
        self._history = [h for h in self._history if chr(10) not in h]
        try:
            self._history_file.parent.mkdir(parents=True, exist_ok=True)
            self._history_file.write_text(
                chr(10).join(self._history[-self.HISTORY_MAX:]), encoding="utf-8")
        except OSError:
            pass          # a history that cannot be written is not worth a crash

    def _walk_history(self, step: int) -> None:
        box = self.query_one("#say", Input)
        if self._history_at is None:
            # Stepping back from a fresh line starts at the newest entry.
            self._history_at = len(self._history) if step < 0 else len(self._history)
        pos = max(0, min(len(self._history), self._history_at + step))
        self._history_at = pos
        box.value = "" if pos >= len(self._history) else self._history[pos]
        box.cursor_position = len(box.value)

    def _highlighted_command(self) -> str:
        hint = self.query_one("#hint", OptionList)
        if not hint.has_class("showing") or hint.highlighted is None:
            return ""
        return (hint.get_option_at_index(hint.highlighted).id or "").split(" ")[0]

    def _accept_hint(self) -> bool:
        """Put the highlighted command in the box, ready for its arguments."""
        hint = self.query_one("#hint", OptionList)
        if not hint.has_class("showing") or hint.highlighted is None:
            return False
        chosen = hint.get_option_at_index(hint.highlighted).id or ""
        # Only the command itself; the <angle brackets> are documentation, and
        # leaving them in the box would mean deleting them before typing.
        box = self.query_one("#say", Input)
        box.value = chosen.split(" ")[0] + " "
        box.cursor_position = len(box.value)
        hint.remove_class("showing")
        return True

    def on_key(self, event) -> None:
        """↑/↓ walk the hints and Tab takes one, without moving focus.

        Handled here rather than by focusing the list: the Input owns the cursor,
        and handing focus away mid-sentence loses your place.
        """
        if len(self.screen_stack) > 1:
            return          # keys belong to the modal while one is open
        hint = self.query_one("#hint", OptionList)
        if not hint.has_class("showing"):
            # No list open, so the arrows mean what they mean in every other
            # prompt: walk what you typed before.
            if event.key in ("up", "down") and self._history:
                self._walk_history(-1 if event.key == "up" else 1)
                event.prevent_default()
                event.stop()
            return
        if event.key in ("down", "up"):
            count = hint.option_count
            if count:
                cur = hint.highlighted or 0
                hint.highlighted = (cur + (1 if event.key == "down" else -1)) % count
            event.prevent_default()
            event.stop()
        elif event.key in ("tab", "enter"):
            # Enter takes the highlighted one, which is what a list you can walk
            # implies. Except when what you typed already *is* that command --
            # then you meant to run it, and making you press Enter twice to run
            # /help would be its own small insult.
            chosen = self._highlighted_command()
            typed = self.query_one("#say", Input).value.strip()
            if event.key == "enter" and typed == chosen:
                # You typed it in full, so you meant to run it. Making /help take
                # two Enters would be its own small insult.
                self.query_one("#hint", OptionList).remove_class("showing")
                return
            if self._accept_hint():
                event.prevent_default()
                event.stop()
        elif event.key == "escape":
            hint.remove_class("showing")
            event.prevent_default()
            event.stop()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        event.input.value = ""
        self.query_one("#hint", OptionList).remove_class("showing")
        if line and (not self._history or self._history[-1] != line):
            self._history.append(line)
            self._save_history()
        self._history_at = None
        if not line:
            return
        if line.startswith("/"):
            # Commands are not board messages, so the echo is the only record of
            # them. Anything else -- speech, an @question -- now renders as the
            # message it becomes, and echoing it too would show it twice.
            self.write_line(mk(f"[dim]> {escape(line)}[/dim]"))
        try:
            if not self.board.handle(line):
                self.exit()
        except StoreError as exc:
            self.write_line(mk(f"[red]{escape(str(exc))}[/red]"))
        self.refresh_board()

    @work(exclusive=True, group="supervisor")
    async def drive(self) -> None:
        """The council, on Textual's own loop.

        Not `asyncio.run` -- that refuses to start inside a running loop, which is
        exactly what the REPL's thread was hiding.
        """
        from .drivers.registry import build_drivers
        from .supervisor import Caps, Supervisor

        store = self.drive_store
        # Served over the web, each browser tab is its own `mooting tui`
        # process, so two viewers pressing Run would start two supervisors on
        # one board and wake every seat twice against one budget.
        holder = store.take_drive(self.board.topic_id, self._session_id)
        if holder is not None:
            self.write_line(mk(f"[yellow]— already being driven by another "
                               f"session[/yellow]"))
            self.board.driving.clear()
            return
        try:
            if store.topic(self.board.topic_id)["status"] == "paused":
                store.set_topic_status(self.board.topic_id, "open", self.board.me,
                                       "resumed from the tui")
            # The per-seat budget lives on the seat row, and `/rounds` writes it
            # there. Leaving Caps at its default put a second, invisible ceiling
            # of 6 on top: a seat granted 10 turns stopped at 6 and reported
            # having none left while the panel still showed 7/10.
            budget = max((s["max_turns"] for s in store.seats(self.board.topic_id)),
                         default=Caps.max_turns_per_seat)
            sup = Supervisor(store, build_drivers(store),
                             Caps(effort=self.board.effort(), max_turns_per_seat=budget))
            reason = await sup.run_topic(self.board.topic_id)
            self.write_line(mk(f"\n[dim]— council stopped: {escape(reason)}[/dim]"))
        except Exception as exc:
            self.write_line(mk(f"\n[red]— council failed: {escape(str(exc))}[/red]"))
        finally:
            store.release_drive(self.board.topic_id, self._session_id)
            self.board.driving.clear()

    @work(group="nudge")
    async def nudge(self, agent: str) -> None:
        from .drivers.registry import build_drivers
        from .supervisor import Supervisor

        store = self.drive_store
        try:
            sup = Supervisor(store, build_drivers(store, [agent]))
            r = await sup.wake_seat(self.board.topic_id, agent)
            if not r.ok:
                self.write_line(mk(f"[red]{escape(agent)}: {escape(r.detail)}[/red]"))
        except Exception as exc:
            # A Textual worker exits the app on an unhandled exception, and a bad
            # seat name should not end the session.
            self.write_line(mk(f"[red]could not wake {escape(agent)}: "
                               f"{escape(str(exc))}[/red]"))

    # ---------------------------------------------------------------- actions

    def action_run(self) -> None:
        self.board.handle("/run")

    def action_stop(self) -> None:
        self.board.handle("/stop")

    def action_tasks(self) -> None:
        # A key binding must guard exactly like a typed command. Textual exits the
        # app on an exception from an action handler, so an unguarded topic lookup
        # here took the whole session down on an empty board.
        if self.board.topic_id is None:
            self.board.handle("/tasks")          # answers "no topic yet"
            return
        self.board.handle("/tasks" if
                          self.board.store.topic(self.board.topic_id)["mode"] == "work"
                          else "/proposals")

    def on_unmount(self) -> None:
        self.board.stop.set()


def run_tui(db: Path | str | None, topic: str, me: str) -> int:
    MootApp(db, topic, me).run()
    return 0
