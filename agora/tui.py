"""`agora tui` -- one screen where the council talks and the team works.

The REPL (`agora console`) is a scrolling log: fine for a conversation, poor for
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

from pathlib import Path

from rich.markdown import Markdown
from rich.markup import escape
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static

from .console import Console
from .store import StoreError, connect


class Board(Console):
    """Console's command set, wired to a widget instead of stdout."""

    def __init__(self, db, topic, me, app: "AgoraApp") -> None:
        super().__init__(db, topic, me)
        self.app_ref = app
        self.emit = app.write_line

    # Long-running work becomes a Textual worker rather than a thread.
    def _run(self, _: str = "") -> None:
        if self.driving.is_set():
            self.emit("[dim]already driving[/dim]")
            return
        self.driving.set()
        self.app_ref.drive()
        self.emit(f"[dim]· council thinking at effort {self.effort()}[/dim]")

    def _nudge(self, agent: str) -> None:
        if not agent:
            self.emit("[red]usage: /nudge <agent>[/red]")
            return
        self.app_ref.nudge(agent)
        self.emit(f"[dim]waking {agent}…[/dim]")

    def on_topic_change(self) -> None:
        """/new and /topic move the whole view, not just a variable."""
        self.app_ref.rebind_topic()


class AgoraApp(App):
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
    Input { border: round $accent; }
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

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            yield RichLog(id="transcript", wrap=True, markup=True, highlight=False)
            with Vertical(id="side"):
                yield DataTable(id="seats", cursor_type="none", zebra_stripes=True)
                yield DataTable(id="work", cursor_type="none", zebra_stripes=True)
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
        self.refresh_board()
        self.set_interval(1.0, self.refresh_board)
        self.query_one("#say", Input).focus()

    # ------------------------------------------------------------- rendering

    def write_line(self, item) -> None:
        """Console.emit target. Takes a markup string or any Rich renderable, and
        a list of either -- messages render as several pieces."""
        log = self.query_one("#transcript", RichLog)
        for part in (item if isinstance(item, list) else [item]):
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

    @staticmethod
    def _render_message(m) -> list:
        """Header line plus the body as markdown.

        Agents write markdown -- headings, bold, lists, fenced code -- and showing
        the source characters wastes the structure they went to the trouble of
        producing. Rendering it is also why the body must not go through Rich
        *markup*: an agent writing `[balance.json]` means the filename, not a
        style tag.
        """
        colour = {"system": "dim", "ruling": "green", "object": "yellow",
                  "propose": "yellow"}.get(m["kind"], "cyan")
        tag = "" if m["kind"] == "say" else f"  [{m['kind']}]"
        header = Text.assemble((m["author"], f"bold {colour}"), (tag, "dim"))
        body = m["body"].strip()
        return ["", header, Markdown(body) if body else Text("")]

    @staticmethod
    def _render_ask(asker: str, question: str) -> list:
        return ["",
                Text.assemble(("❓ ", "magenta"),
                              (f"{asker} is asking you", "bold magenta")),
                Markdown(question.strip()),
                Text("   Type your answer below — it clears the question and the "
                     "council resumes.", "dim")]

    def refresh_board(self) -> None:
        """One tick: drain new events into the log, then repaint state."""
        store = self.board.store
        if self.board.topic_id is None:
            self.query_one("#seats", DataTable).clear()
            self.query_one("#work", DataTable).clear()
            self.query_one("#status", Static).update(
                "no topic  |  /new <what you want to discuss>  |  /help")
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
            self.write_line(f"[red]refresh: {escape(str(exc))}[/red]")

    def _render_event(self, store, ev) -> str | None:
        if ev.kind == "message":
            row = store.q1("SELECT * FROM messages WHERE id = ?",
                           (ev.payload.get("message_id"),))
            if row is None or (row["author"] == self.board.me and row["kind"] != "ruling"):
                return None
            if self.board.me in (ev.payload.get("mentions") or []):
                self.notify_turn(f"{row['author']} asked you a question")
                return self._render_ask(row["author"], row["body"])
            return self._render_message(row)
        if ev.kind == "proposal" and ev.payload.get("action") == "opened":
            pid = ev.payload["proposal_id"]
            self.notify_turn(f"proposal #{pid} needs your ruling")
            return (f"\n[yellow bold]◆ proposal #{pid}[/yellow bold] "
                    f"[yellow]{escape(ev.payload['title'])}[/yellow]\n"
                    f"[dim]  /approve {pid} <why>   |   /reject {pid} <why>[/dim]")
        if ev.kind == "decision":
            return (f"\n[green]✓ proposal #{ev.payload['proposal_id']} "
                    f"{ev.payload['status']} by {escape(ev.actor)}[/green]")
        if ev.kind == "task":
            return (f"[dim]· task #{ev.payload.get('task_id')} "
                    f"{ev.payload.get('action')} ({escape(ev.actor)})[/dim]")
        if ev.kind == "topic" and ev.payload.get("action") == "paused":
            return f"\n[dim]— paused: {escape(str(ev.payload.get('note', '')))}[/dim]"
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
            table.add_row(s["agent"], state, f"{s['turns_used']}/{s['max_turns']}")

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
        asks = len(b.pending_asks())
        props = len(b.store.proposals(b.topic_id, status="open"))
        if not asks and not props:
            self._waiting = None
        bits = [f"effort {b.effort()}",
                "driving" if b.driving.is_set() else "idle",
                f"auto {'on' if b.auto else 'off'}"]
        if asks:
            bits.append(f"[magenta]{asks} question(s) for you[/magenta]")
        if props:
            bits.append(f"[yellow]{props} awaiting your ruling[/yellow]")
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
            self.title = "Agora"
            self.sub_title = "no topic"
            log.write("[dim]Nothing on the board yet.[/dim]")
            log.write("")
            log.write("[bold]Start one[/bold] — just say what you want to discuss:")
            log.write("  [cyan]/new the workflow optimization in agentic AI development"
                      "[/cyan]")
            log.write("")
            log.write("[dim]Then type any detail the council needs, and [/dim]"
                      "[cyan]/run[/cyan][dim].[/dim]")
            log.write("[dim]Answering is just typing. [/dim][cyan]@agent <question>"
                      "[/cyan][dim] asks one seat. [/dim][cyan]/help[/cyan]"
                      "[dim] for the rest.[/dim]")
            self.cursor = self.board.store.head()
            self.refresh_board()
            return
        t = self.board.store.topic(self.board.topic_id)
        self.title = t["title"]
        self.sub_title = f"{t['slug']} · {t['mode']}"
        for m in self.board.store.transcript(self.board.topic_id)[-40:]:
            self.write_line(self._render_message(m))
        for a in self.board.pending_asks():
            self.write_line(self._render_ask(a["asker"], a["question"]))
        # Fresh cursor, or the first tick would replay the new topic's history.
        self.cursor = self.board.store.head()
        self.refresh_board()

    # -------------------------------------------------------------- behaviour

    def on_input_submitted(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        event.input.value = ""
        if not line:
            return
        if line.startswith("/") or line.startswith("@"):
            self.write_line(f"[dim]> {escape(line)}[/dim]")
        try:
            if not self.board.handle(line):
                self.exit()
        except StoreError as exc:
            self.write_line(f"[red]{escape(str(exc))}[/red]")
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
        try:
            if store.topic(self.board.topic_id)["status"] == "paused":
                store.set_topic_status(self.board.topic_id, "open", self.board.me,
                                       "resumed from the tui")
            sup = Supervisor(store, build_drivers(store), Caps(effort=self.board.effort()))
            reason = await sup.run_topic(self.board.topic_id)
            self.write_line(f"\n[dim]— council stopped: {escape(reason)}[/dim]")
        except Exception as exc:
            self.write_line(f"\n[red]— council failed: {escape(str(exc))}[/red]")
        finally:
            self.board.driving.clear()

    @work(group="nudge")
    async def nudge(self, agent: str) -> None:
        from .drivers.registry import build_drivers
        from .supervisor import Supervisor

        store = self.drive_store
        sup = Supervisor(store, build_drivers(store, [agent]))
        r = await sup.wake_seat(self.board.topic_id, agent)
        if not r.ok:
            self.write_line(f"[red]{escape(agent)}: {escape(r.detail)}[/red]")

    # ---------------------------------------------------------------- actions

    def action_run(self) -> None:
        self.board.handle("/run")

    def action_stop(self) -> None:
        self.board.handle("/stop")

    def action_tasks(self) -> None:
        self.board.handle("/tasks" if
                          self.board.store.topic(self.board.topic_id)["mode"] == "work"
                          else "/proposals")

    def on_unmount(self) -> None:
        self.board.stop.set()


def run_tui(db: Path | str | None, topic: str, me: str) -> int:
    AgoraApp(db, topic, me).run()
    return 0
