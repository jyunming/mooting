"""The full-screen session, driven headlessly.

Textual ships a pilot that runs the real app with a virtual terminal, so there is
no excuse for shipping this unexecuted -- which is exactly what happened to the
prompt_toolkit path, claimed for three turns before anything ran it.

These drive real keystrokes through the real widgets and assert on the board
afterwards: typing posts, @ directs, /approve rules, and a work topic shows its
tasks. What they cannot check is whether it *looks* right in a terminal.
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from textual.widgets import DataTable, Input, RichLog, Static  # noqa: E402

from agora.store import StoreError, connect                          # noqa: E402
from agora.tui import AgoraApp                           # noqa: E402


@pytest.fixture()
def board(tmp_path):
    s = connect(tmp_path / "board.db", init=True)
    s.add_agent("me", "human")
    s.add_agent("claude", "claude", driver="spawn")
    s.add_agent("codex", "codex", driver="spawn",
                driver_cfg={"capability": "execute", "cwd": str(tmp_path)})
    yield s
    s.close()


def app_for(tmp_path, board, slug="t", **kw):
    board.open_topic(slug, "A topic", "the brief", "me",
                     seats=("claude", "codex", "me"), **kw)
    app = AgoraApp(tmp_path / "board.db", slug, "me")
    # auto-wake off for every test, without exception: posting would otherwise
    # start a real supervisor and spawn real CLIs. A test suite that quietly
    # spends subscription quota is a trap, and it caught me once already.
    app.board.auto = False
    return app


async def type_line(pilot, app, text: str) -> None:
    app.query_one("#say", Input).value = text
    await pilot.press("enter")
    await pilot.pause()


@pytest.mark.asyncio
async def test_the_app_starts_with_every_pane(tmp_path, board):
    app = app_for(tmp_path, board)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#transcript", RichLog)
        assert app.query_one("#seats", DataTable).row_count == 3
        assert "A topic" in str(app.title)


@pytest.mark.asyncio
async def test_typing_posts_to_the_board(tmp_path, board):
    app = app_for(tmp_path, board)
    async with app.run_test() as pilot:
        app.board.auto = False          # do not start a real supervisor in a test
        await type_line(pilot, app, "the config lives in ops/")

    bodies = [m["body"] for m in board.transcript(app.board.topic_id)]
    assert "the config lives in ops/" in bodies


@pytest.mark.asyncio
async def test_at_mention_from_the_tui_directs_a_question(tmp_path, board):
    app = app_for(tmp_path, board)
    async with app.run_test() as pilot:
        app.board.auto = False
        await type_line(pilot, app, "@codex what does the gateway do today?")

    asks = board.open_mentions(app.board.topic_id)
    assert [(a["asker"], a["target"]) for a in asks] == [("me", "codex")]


@pytest.mark.asyncio
async def test_answering_an_ask_clears_it_and_it_was_shown_first(tmp_path, board):
    app = app_for(tmp_path, board)
    board.ask(app.board.topic_id, "claude", "me", "Where does the config live?")
    async with app.run_test() as pilot:
        app.board.auto = False
        await pilot.pause()
        assert app.board.pending_asks(), "the question should be outstanding at start"
        await type_line(pilot, app, "ops/gateway.yaml")

    assert app.board.pending_asks() == [], "answering did not clear the question"


@pytest.mark.asyncio
async def test_a_human_can_rule_on_a_proposal_from_the_input(tmp_path, board):
    """The one power agents do not have, exercised through the real UI."""
    app = app_for(tmp_path, board)
    pid = board.propose(app.board.topic_id, "claude", "Adopt backoff", "with jitter")
    async with app.run_test() as pilot:
        app.board.auto = False
        await type_line(pilot, app, f"/approve {pid} agreed")

    p = board.proposal(pid)
    assert (p["status"], p["decided_by"]) == ("approved", "me")


@pytest.mark.asyncio
async def test_a_work_topic_shows_its_tasks_and_blocked_reasons(tmp_path, board):
    app = app_for(tmp_path, board, slug="w", mode="work", manager="claude")
    tid = board.draft_task(app.board.topic_id, "claude", "codex", "Add backoff")
    board.decide(board.submit_plan(app.board.topic_id, "claude"), "me", approve=True)
    board.update_task(tid, "codex", "blocked", "needs the staging credentials")

    async with app.run_test() as pilot:
        await pilot.pause()
        work = app.query_one("#work", DataTable)
        cells = [str(work.get_cell_at((r, c)))
                 for r in range(work.row_count) for c in range(3)]

    assert any("Add backoff" in c for c in cells)
    assert any("blocked" in c for c in cells)
    # A blocked task is the thing most likely to be waiting on a person, so the
    # reason has to be visible in the pane, not only a status word. (The full text
    # is also a system message in the transcript.)
    assert any("staging" in c for c in cells)


@pytest.mark.asyncio
async def test_effort_is_retunable_from_the_tui(tmp_path, board):
    app = app_for(tmp_path, board)
    async with app.run_test() as pilot:
        await type_line(pilot, app, "/effort high")
    assert app.board.effort() == "high"


@pytest.mark.asyncio
async def test_agent_bodies_render_as_markdown_not_as_markup(tmp_path, board):
    """Agents write markdown, so it is rendered. Which is also why the body must
    not go through Rich *markup*: `[balance.json]` means the filename."""
    from rich.markdown import Markdown

    parts = AgoraApp(tmp_path / "board.db", None, "me")._render_message(
        {"author": "claude", "kind": "say",
         "body": "## Finding" + chr(10) * 2 + "see [balance.json] line 12"
                 + chr(10) * 2 + "- one" + chr(10) + "- two"})
    # The body is wrapped in a Padding that carries the seat's colour band.
    from rich.padding import Padding
    inner = [p.renderable if isinstance(p, Padding) else p for p in parts]
    body = [p for p in inner if isinstance(p, Markdown)]
    assert body, "the body should be a rendered Markdown, not a plain string"
    assert "[balance.json]" in body[0].markup
    assert "## Finding" in body[0].markup


@pytest.mark.asyncio
async def test_a_new_topic_can_be_opened_without_leaving(tmp_path, board):
    """The whole premise is one place; going back to the shell to start the next
    question breaks it."""
    app = app_for(tmp_path, board)
    async with app.run_test() as pilot:
        app.board.auto = False
        await type_line(pilot, app, "/new Should retries use exponential backoff?")
        await pilot.pause()

        assert app.board.topic["slug"] == "should-retries-use-exponential-backoff"
        assert "backoff" in str(app.sub_title)
        # Seats carry over -- "same room, next question".
        seats = {s["agent"] for s in board.seats(app.board.topic_id)}
        assert seats == {"claude", "codex", "me"}


@pytest.mark.asyncio
async def test_switching_topic_does_not_leave_the_old_transcript_behind(tmp_path, board):
    app = app_for(tmp_path, board)
    board.post(app.board.topic_id, "claude", "OLD-TOPIC-CHATTER", count_turn=False)
    async with app.run_test() as pilot:
        app.board.auto = False
        await pilot.pause()
        await type_line(pilot, app, "/new A different question")
        await pilot.pause()
        # The cursor was reset, so the next tick must not replay history either.
        app.refresh_board()
        assert app.board.topic["slug"] == "different-question"


@pytest.mark.asyncio
async def test_a_role_only_exists_on_a_work_topic(tmp_path, board):
    """In discussion everyone argues on equal footing, so there is no manager to
    be. The role is granted when the topic becomes work and taken back when it
    stops being work, rather than lingering as a title nobody uses."""
    app = app_for(tmp_path, board)
    async with app.run_test() as pilot:
        app.board.auto = False

        await type_line(pilot, app, "/manager claude")     # refused: not a work topic
        assert not board.is_manager(app.board.topic_id, "claude")

        await type_line(pilot, app, "/mode work")          # refused: names nobody
        assert board.topic("t")["mode"] == "debate"

        await type_line(pilot, app, "/mode work claude")   # sets both at once
        assert board.topic("t")["mode"] == "work"
        assert board.is_manager(app.board.topic_id, "claude")

        await type_line(pilot, app, "/mode discuss")       # and the role goes back
        assert board.topic("t")["mode"] == "discuss"
        assert not board.is_manager(app.board.topic_id, "claude")


@pytest.mark.asyncio
async def test_deleting_a_topic_needs_confirming(tmp_path, board):
    """One keystroke should not be able to destroy a conversation."""
    app = app_for(tmp_path, board)
    board.open_topic("keeper", "Another", "b", "me", seats=("claude", "me"))
    async with app.run_test() as pilot:
        app.board.auto = False
        await type_line(pilot, app, "/rm t")
        assert board.topic("t"), "a bare /rm must not delete anything"
        await type_line(pilot, app, "/rm t yes")
        await pilot.pause()

    with pytest.raises(Exception):
        board.topic("t")


@pytest.mark.asyncio
async def test_deleting_the_topic_you_are_on_lands_you_somewhere(tmp_path, board):
    app = app_for(tmp_path, board)
    board.open_topic("keeper", "Another", "b", "me", seats=("claude", "me"))
    async with app.run_test() as pilot:
        app.board.auto = False
        await type_line(pilot, app, "/rm yes")
        await pilot.pause()
        assert app.board.topic["slug"] == "keeper"
        assert "keeper" in str(app.sub_title), "the view did not follow"


@pytest.mark.asyncio
async def test_reset_needs_confirming_and_keeps_the_seats(tmp_path, board):
    app = app_for(tmp_path, board)
    async with app.run_test() as pilot:
        app.board.auto = False
        await type_line(pilot, app, "/reset")
        assert board.topics(), "a bare /reset must not clear anything"
        await type_line(pilot, app, "/reset yes")
        await pilot.pause()

    assert board.topics() == []
    assert {a["name"] for a in board.agents()} >= {"claude", "codex"}


@pytest.mark.asyncio
async def test_the_session_opens_on_an_empty_board(tmp_path, board):
    """You must be able to start from inside. Being told to go back to the shell
    first is the exact break this session exists to remove."""
    app = AgoraApp(tmp_path / "board.db", None, "me")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.board.topic_id is None
        assert "no topic" in str(app.sub_title)

        await type_line(pilot, app, "/new Should retries back off?")
        await pilot.pause()

        assert app.board.topic["slug"] == "should-retries-back-off"
        seats = {s["agent"] for s in board.seats(app.board.topic_id)}
        assert {"claude", "codex", "me"} <= seats


@pytest.mark.asyncio
async def test_clearing_the_board_leaves_you_inside_it(tmp_path, board):
    """Ending the session on /reset would throw you out of the very thing you
    would use to start again."""
    app = app_for(tmp_path, board)
    async with app.run_test() as pilot:
        app.board.auto = False
        await type_line(pilot, app, "/reset yes")
        await pilot.pause()
        assert app.board.topic_id is None
        assert "no topic" in str(app.sub_title)

        await type_line(pilot, app, "/new A brand new question")
        await pilot.pause()
        assert app.board.topic["slug"] == "brand-new-question"


@pytest.mark.asyncio
async def test_topic_bound_commands_say_so_rather_than_crashing(tmp_path, board):
    app = AgoraApp(tmp_path / "board.db", None, "me")
    async with app.run_test() as pilot:
        await pilot.pause()
        for line in ("hello", "/run", "/seats", "/tasks", "/effort high", "@codex hi"):
            await type_line(pilot, app, line)
        assert app.board.topic_id is None


@pytest.mark.asyncio
async def test_the_status_bar_shouts_when_it_is_your_turn(tmp_path, board):
    """A council that waits silently is one you have to sit and watch."""
    app = app_for(tmp_path, board)
    async with app.run_test() as pilot:
        await pilot.pause()
        board.ask(app.board.topic_id, "claude", "me", "Where is the config?")
        app.refresh_board()
        await pilot.pause()

        status = str(app.query_one("#status", Static).content)
        assert "YOUR TURN" in status

        app.board.handle("ops/gateway.yaml")     # answering clears it
        app.refresh_board()
        assert "YOUR TURN" not in str(app.query_one("#status", Static).content)


@pytest.mark.asyncio
async def test_the_panes_do_not_fight_for_width(tmp_path, board):
    """A screenshot showed the transcript overrunning the sidebar and the two
    painting over each other, because only one of them had a width."""
    app = app_for(tmp_path, board)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        side = app.query_one("#side").size.width
        transcript = app.query_one("#transcript").size.width

    assert side == 42, f"sidebar lost its width: {side}"
    assert transcript > 60, f"transcript squeezed to {transcript}"
    assert side + transcript <= 120, "panes overlap"


@pytest.mark.asyncio
async def test_help_text_is_not_eaten_by_markup(tmp_path, board):
    """`[slug]` is a Rich style tag, so /rm rendered with a blank description."""
    from agora.console import COMMANDS

    for cmd, why in COMMANDS.items():
        assert "[" not in why, f"{cmd} description would be swallowed as markup: {why}"


@pytest.mark.asyncio
async def test_help_is_grouped_and_says_you_can_just_type(tmp_path, board):
    app = app_for(tmp_path, board)
    written: list[str] = []
    async with app.run_test() as pilot:
        app.write_line = lambda item: written.append(str(item))
        app.board.emit = app.write_line
        app.board.handle("/help")

    text = "\n".join(written)
    assert "Talking" in text and "Deciding" in text and "Clearing up" in text
    # The thing people actually need first, which a flat command list never says.
    assert "post it" in text and "@agent" in text
    assert "<slug>" not in text.split("Topics")[0], "help still demands a slug"


@pytest.mark.asyncio
async def test_history_never_hands_richlog_a_list(tmp_path, board):
    """A message renders as several pieces. Passing the list straight to
    RichLog.write() reprs it, which put

        <rich.markdown.Markdown object at 0x...>

    into a real transcript. RichLog must receive the pieces, never the list."""
    app = app_for(tmp_path, board)
    board.post(app.board.topic_id, "claude", "## Finding" + chr(10) * 2 + "it holds",
               count_turn=False)

    got: list = []
    async with app.run_test() as pilot:
        await pilot.pause()
        log = app.query_one("#transcript", RichLog)
        original = log.write
        log.write = lambda item, *a, **k: (got.append(item), original(item, *a, **k))[1]
        app.rebind_topic()
        await pilot.pause()

    assert got, "history was not replayed"
    assert not any(isinstance(item, list) for item in got),         "a list of renderables reached RichLog and would be printed as a repr"
    from rich.markdown import Markdown
    from rich.padding import Padding
    inner = [g.renderable if isinstance(g, Padding) else g for g in got]
    assert any(isinstance(item, Markdown) for item in inner),         "the body should arrive as rendered markdown"


@pytest.mark.asyncio
async def test_every_seat_gets_a_distinct_colour(tmp_path, board):
    """Hashing names looked fine until codex and agy both landed on cyan.
    Distinctness is the point, so it is allocated, not hoped for."""
    from agora.tui import seat_colours

    palette = seat_colours(["claude", "codex", "agy", "copilot", "jyunming"])
    assert len(set(palette.values())) == 5, f"colours collided: {palette}"
    # Same every time you open the topic.
    assert seat_colours(["codex", "claude"]) == seat_colours(["claude", "codex"])

    app = app_for(tmp_path, board)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.colour_for("claude") != app.colour_for("codex")
        # You are not colour-coded; you are just bold.
        assert app.board.me in {s["agent"] for s in board.seats(app.board.topic_id)}


@pytest.mark.asyncio
async def test_seats_can_be_changed_on_the_topic(tmp_path, board):
    """The council is per topic: some questions want the historian, some want the
    engine, and paying four CLIs to sit through a question two of them cannot
    help with is the cost this exists to control."""
    board.add_agent("agy", "agy", driver="spawn")
    app = app_for(tmp_path, board)
    async with app.run_test() as pilot:
        here = lambda: {s["agent"] for s in board.seats(app.board.topic_id)}
        assert "agy" not in here()

        await type_line(pilot, app, "/seats add agy")
        assert "agy" in here()

        await type_line(pilot, app, "/seats rm codex")
        assert "codex" not in here()

        await type_line(pilot, app, "/seats add nobody")     # not registered
        assert "nobody" not in here()


@pytest.mark.asyncio
async def test_removing_a_seat_keeps_what_it_said_and_frees_the_room(tmp_path, board):
    app = app_for(tmp_path, board)
    async with app.run_test() as pilot:
        board.post(app.board.topic_id, "codex", "an argument that still counts",
                   count_turn=False)
        board.ask(app.board.topic_id, "claude", "codex", "does that hold?")
        await type_line(pilot, app, "/seats rm codex")

    assert any(m["author"] == "codex" for m in board.transcript(app.board.topic_id))
    # A question nobody can answer would block the room forever.
    assert board.open_mentions(app.board.topic_id) == []


@pytest.mark.asyncio
async def test_the_manager_cannot_simply_be_removed(tmp_path, board):
    app = app_for(tmp_path, board, slug="w", mode="work", manager="claude")
    async with app.run_test() as pilot:
        await type_line(pilot, app, "/seats rm claude")
    assert board.is_manager(app.board.topic_id, "claude"), "manager left silently"


@pytest.mark.asyncio
async def test_each_seat_gets_a_distinct_tinted_band(tmp_path, board):
    """Colour the name and band the message, blended into the real background so
    it groups a reply rather than highlighting it."""
    from rich.padding import Padding
    from textual.color import Color

    from agora.tui import seat_colours, tint_for

    base = Color(24, 24, 32)
    pal = seat_colours(["claude", "codex", "agy", "copilot"])
    bands = {tint_for(c, base) for c in pal.values()}
    assert len(bands) == len(pal), f"bands collided: {bands}"
    assert all(bands), "a band came out empty — Color.parse rejected the colour"
    assert base.hex not in bands, "the band is indistinguishable from the background"

    app = app_for(tmp_path, board)
    async with app.run_test() as pilot:
        await pilot.pause()
        parts = app._render_message({"author": "claude", "kind": "say", "body": "hi"})
        padded = [p for p in parts if isinstance(p, Padding)]
        assert len(padded) == 2, "header and body should both carry the band"
        assert padded[0].style.bgcolor is not None

        # Board notices are not a seat speaking, so they stay untinted.
        sys_parts = app._render_message(
            {"author": "agora", "kind": "system", "body": "paused"})
        sys_padded = [p for p in sys_parts if isinstance(p, Padding)]
        assert all(p.style.bgcolor is None for p in sys_padded)


@pytest.mark.asyncio
async def test_a_mention_with_punctuation_finds_the_seat_and_starts_them(tmp_path, board):
    """"@agy, what do you think?" reported "'agy,' holds no seat on this topic",
    and asking started nothing at all -- the @ branch returned before the wake."""
    app = app_for(tmp_path, board)
    started: list[str] = []
    async with app.run_test() as pilot:
        app.board.auto = True
        app.drive = lambda: started.append("drive")     # don't spawn a real CLI
        await type_line(pilot, app, "@codex, what do you think?")
        await pilot.pause()

    asks = board.open_mentions(app.board.topic_id)
    assert [(a["asker"], a["target"]) for a in asks] == [("me", "codex")]
    assert asks[0]["question"].endswith("what do you think?")
    assert started, "asking a question must wake the council, not sit there"


@pytest.mark.asyncio
async def test_running_out_of_rounds_says_so(tmp_path, board):
    """Silently doing nothing is indistinguishable from being ignored."""
    app = app_for(tmp_path, board, max_rounds=1)
    said: list[str] = []
    async with app.run_test() as pilot:
        app.board.auto = True
        app.write_line = lambda item: said.append(str(item))
        app.board.emit = app.write_line
        app.drive = lambda: said.append("DRIVE")
        await type_line(pilot, app, "anything")

    text = " ".join(said)
    # You typing is the authorisation: one round is granted so the conversation
    # you are sitting in continues, and only one, so it cannot run off unattended.
    assert "granted one more" in text and "DRIVE" in text
    assert board.topic("t")["max_rounds"] == 2
    # ...and the seats get the turns to use it, or they stay capped and silent.
    assert all(s["max_turns"] == 2 for s in board.seats(app.board.topic_id))


@pytest.mark.asyncio
async def test_quoting_attaches_a_reply_and_shows_what_it_answers(tmp_path, board):
    """Like a chat client: say which message you are answering."""
    app = app_for(tmp_path, board)
    mid = board.post(app.board.topic_id, "claude",
                     "fresh-context review catches more requirement errors",
                     count_turn=False)
    async with app.run_test() as pilot:
        app.board.auto = False
        await type_line(pilot, app, f"/quote {mid}")
        assert app.board._quoting == mid
        await type_line(pilot, app, "only for wrong-behaviour bugs")

        posted = board.transcript(app.board.topic_id)[-1]
        assert posted["reply_to"] == mid
        assert app.board._quoting is None, "the quote should not stick to the next one"

        quoted = app._quoted_line(posted)
        assert quoted is not None and "claude" in str(quoted)


@pytest.mark.asyncio
async def test_ansi_from_the_console_is_decoded_not_printed(tmp_path, board):
    """Console styles with ANSI escapes; a Rich widget takes them literally, which
    put black boxes behind the help text and corrupted the lines around it."""
    app = app_for(tmp_path, board)
    async with app.run_test() as pilot:
        await pilot.pause()
        log = app.query_one("#transcript", RichLog)
        got: list = []
        original = log.write
        log.write = lambda item, *a, **k: (got.append(item), original(item, *a, **k))[1]
        app.board.handle("/help")
        await pilot.pause()

    assert got, "help wrote nothing"
    for item in got:
        assert "\x1b" not in str(item), f"raw ANSI reached the widget: {item!r}"


@pytest.mark.asyncio
async def test_the_input_box_says_what_to_do_when_you_are_asked(tmp_path, board):
    """The clearest place to say "answer here" is the box you would type into."""
    app = app_for(tmp_path, board)
    async with app.run_test() as pilot:
        await pilot.pause()
        box = app.query_one("#say", Input)
        assert "type to speak" in box.placeholder

        board.ask(app.board.topic_id, "claude", "me", "Where is the config?")
        app.refresh_board()
        await pilot.pause()

        assert "answer" in box.placeholder and "claude" in box.placeholder
        assert box.has_class("waiting")


@pytest.mark.asyncio
async def test_typing_a_slash_offers_the_commands(tmp_path, board):
    """Discovering commands by reading /help and remembering them is a poor deal
    when the screen can just say."""
    app = app_for(tmp_path, board)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import OptionList
        hint = app.query_one("#hint", OptionList)
        assert not hint.has_class("showing"), "the hint should cost nothing at rest"

        def offered():
            return [hint.get_option_at_index(i).id for i in range(hint.option_count)]

        app.query_one("#say", Input).value = "/se"
        await pilot.pause()
        await pilot.pause()          # the first Changed after mount needs a settle
        assert hint.has_class("showing")
        assert any(o.startswith("/seats") for o in offered())

        # A bare slash lists everything, so nothing has to be guessed.
        app.query_one("#say", Input).value = "/"
        await pilot.pause()
        assert hint.option_count > 10, f"only {hint.option_count} commands offered"

        # Arrows walk it without taking the cursor out of the box.
        assert hint.highlighted == 0
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert hint.highlighted == 2
        assert app.focused is app.query_one("#say", Input)

        # Tab takes the highlighted one, ready for its arguments.
        chosen = offered()[2].split(" ")[0]
        await pilot.press("tab")
        await pilot.pause()
        assert app.query_one("#say", Input).value == chosen + " "

        app.query_one("#say", Input).value = "just talking"
        await pilot.pause()
        assert not hint.has_class("showing")


@pytest.mark.asyncio
async def test_the_round_is_on_screen_next_to_the_turn_budget(tmp_path, board):
    """A seat showing 2/6 on a 3-round topic reads as a contradiction; the round
    count was simply not displayed anywhere."""
    app = app_for(tmp_path, board, max_rounds=3)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "round 1/3" in str(app.query_one("#status", Static).content)

    # And a seat can no longer be given more turns than there are rounds to use.
    seats = board.seats(app.board.topic_id)
    assert all(s["max_turns"] == 3 for s in seats), \
        f"turn budget outruns the rounds: {[dict(s)['max_turns'] for s in seats]}"


@pytest.mark.asyncio
async def test_a_seat_can_be_created_and_renamed_from_the_session(tmp_path, board):
    """Several seats can run the same CLI under names you chose, which is most of
    what makes a four-way transcript readable."""
    app = app_for(tmp_path, board)
    async with app.run_test() as pilot:
        await type_line(pilot, app, "/seats add reviewer codex")
        assert board.agent("reviewer")["kind"] == "codex"
        assert board.seat(app.board.topic_id, "reviewer") is not None

        await type_line(pilot, app, "/me jyunming")
        assert app.board.me == "jyunming"

    # A rename must not orphan what was already said.
    assert board.topic("t")["opened_by"] == "jyunming"
    assert board.seat(app.board.topic_id, "jyunming") is not None
    with pytest.raises(StoreError):
        board.agent("me")


@pytest.mark.asyncio
async def test_quoting_does_not_require_hunting_for_an_id(tmp_path, board):
    """The thing you want to answer is almost always the last thing said."""
    app = app_for(tmp_path, board)
    first = board.post(app.board.topic_id, "claude", "fresh context catches more",
                       count_turn=False)
    last = board.post(app.board.topic_id, "codex", "only for wrong behaviour",
                      count_turn=False)
    async with app.run_test() as pilot:
        await type_line(pilot, app, "/quote")            # the latest, from anyone
        assert app.board._quoting == last

        await type_line(pilot, app, "/quote claude")     # that seat's latest
        assert app.board._quoting == first

        await type_line(pilot, app, f"/quote {last}")    # or an explicit id
        assert app.board._quoting == last

        await type_line(pilot, app, "/quote 0")          # and off again
        assert app.board._quoting is None


@pytest.mark.asyncio
async def test_a_proposal_can_be_read_in_full_before_ruling_on_it(tmp_path, board):
    """A ruling is the one thing here that cannot be undone, so the body and the
    objections have to be readable at the moment you decide."""
    app = app_for(tmp_path, board)
    pid = board.propose(app.board.topic_id, "claude", "Adopt backoff",
                        "Exponential, capped at 6 attempts, with jitter.")
    board.vote(pid, "codex", "object", "stampede risk is overstated")

    said: list[str] = []
    async with app.run_test() as pilot:
        app.write_line = lambda item: said.append(str(item))
        app.board.emit = app.write_line
        app.board.handle(f"/proposals {pid}")

    text = " ".join(said)
    assert "capped at 6 attempts" in text          # the body, not just the title
    assert "codex" in text and "object" in text
    assert "stampede risk is overstated" in text   # and why they objected
    assert f"/approve {pid}" in text               # what to do about it


@pytest.mark.asyncio
async def test_a_message_can_be_read_in_full_after_it_scrolls_away(tmp_path, board):
    app = app_for(tmp_path, board)
    mid = board.post(app.board.topic_id, "claude", "a long argument " * 40,
                     count_turn=False)
    reply = board.post(app.board.topic_id, "codex", "I disagree", count_turn=False,
                       reply_to=mid)

    said: list[str] = []
    async with app.run_test() as pilot:
        app.write_line = lambda item: said.append(str(item))
        app.board.emit = app.write_line
        app.board.handle(f"/show {reply}")

    text = " ".join(said)
    assert "I disagree" in text
    assert f"replying to #{mid}" in text and "claude" in text


@pytest.mark.asyncio
async def test_proposal_numbers_are_not_message_numbers(tmp_path, board):
    """Two counters, and typing one where the other is expected quotes the wrong
    thing without complaining. The screen has to say which is which."""
    app = app_for(tmp_path, board)
    for i in range(4):                       # push message ids away from proposal ids
        board.post(app.board.topic_id, "claude", f"filler {i}", count_turn=False)
    pid = board.propose(app.board.topic_id, "claude", "Adopt backoff", "body")
    msg_id = board.proposal(pid)["message_id"]
    assert pid != msg_id, "the fixture needs the two counters to differ"

    said: list[str] = []
    async with app.run_test() as pilot:
        app.write_line = lambda item: said.append(str(item))
        app.board.emit = app.write_line
        app.board.handle(f"/proposals {pid}")

    text = " ".join(said)
    assert f"proposal #{pid}" in text
    assert f"/quote {msg_id}" in text, "the message id to reply to must be given"


@pytest.mark.asyncio
async def test_minutes_can_be_written_decisions_only(tmp_path, board, monkeypatch):
    """What you hand to someone is usually what was ruled; the transcript is the
    evidence behind it, not the thing itself."""
    monkeypatch.chdir(tmp_path)
    app = app_for(tmp_path, board)
    board.post(app.board.topic_id, "claude", "SOME-LONG-ARGUMENT", count_turn=False)
    pid = board.propose(app.board.topic_id, "claude", "Adopt backoff", "with jitter")
    board.decide(pid, "me", approve=True, rationale="agreed")

    async with app.run_test() as pilot:
        await type_line(pilot, app, "/minutes decisions")
        await type_line(pilot, app, "/minutes")

    brief = (tmp_path / "t-decisions.md").read_text(encoding="utf-8")
    full = (tmp_path / "t-minutes.md").read_text(encoding="utf-8")

    assert "Adopt backoff" in brief and "agreed" in brief
    assert "SOME-LONG-ARGUMENT" not in brief, "the transcript should be omitted"
    assert "SOME-LONG-ARGUMENT" in full, "the full minutes should keep it"


@pytest.mark.asyncio
async def test_an_open_proposal_is_visible_when_you_open_the_topic(tmp_path, board):
    """A proposal raised before you opened the topic was waiting on you
    invisibly: only live arrivals were announced."""
    app = app_for(tmp_path, board)
    pid = board.propose(app.board.topic_id, "claude", "Book direct", "body")

    written: list = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app.write_line = lambda item: written.extend(
            item if isinstance(item, list) else [item])
        app.rebind_topic()
        await pilot.pause()

        text = " ".join(str(w) for w in written)
        assert f"proposal #{pid}" in text
        assert f"/approve {pid}" in text, "it must say how to rule on it"

        # And the status bar and input both say something is waiting.
        app.refresh_board()
        assert "awaiting your ruling" in str(app.query_one("#status", Static).content)
        assert "/approve" in app.query_one("#say", Input).placeholder


@pytest.mark.asyncio
async def test_a_proposal_banner_carries_the_proposal(tmp_path, board):
    """You cannot rule on a title. The banner was the title alone, which reads as
    a notification that a decision exists somewhere else."""
    from rich.markdown import Markdown

    app = app_for(tmp_path, board)
    pid = board.propose(app.board.topic_id, "claude", "Book direct",
                        "1. Use aggregators to search.\n2. Book with the airline.")
    board.vote(pid, "codex", "object", "OTAs are cheaper")

    async with app.run_test() as pilot:
        await pilot.pause()
        parts = app._render_proposal(board.proposal(pid))

    text = " ".join(str(p) for p in parts)
    md = [p for p in parts if isinstance(p, Markdown)]
    assert md and "Book with the airline" in md[0].markup, "the body must be there"
    assert "codex object" in text and "OTAs are cheaper" in text
    assert f"/approve {pid}" in text


@pytest.mark.asyncio
async def test_concluding_refuses_to_paper_over_loose_ends(tmp_path, board, monkeypatch):
    """Minutes of an abandoned discussion read exactly like minutes of a settled
    one unless somebody says which it was."""
    monkeypatch.chdir(tmp_path)
    app = app_for(tmp_path, board)
    pid = board.propose(app.board.topic_id, "claude", "Book direct", "body")

    async with app.run_test() as pilot:
        await type_line(pilot, app, "/conclude we are done here")
        assert board.topic("t")["status"] != "resolved", "closed over an open proposal"

        # But a question one agent left hanging for another is not your duty and
        # must not stop you closing a meeting you are chairing.
        board.ask(app.board.topic_id, "claude", "codex", "does that hold?")

        board.decide(pid, "me", approve=True, rationale="agreed")
        await type_line(pilot, app, "/conclude book direct, train only if much cheaper")

    t = board.topic("t")
    assert t["status"] == "resolved"
    note = board.closing_note(app.board.topic_id)
    assert note is not None and "train only if much cheaper" in note["body"]
    # ...and the minutes were written in the same step.
    text = (tmp_path / "t-minutes.md").read_text(encoding="utf-8")
    assert "## Conclusion" in text and "train only if much cheaper" in text


@pytest.mark.asyncio
async def test_force_closes_it_as_it_stands(tmp_path, board, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = app_for(tmp_path, board)
    board.propose(app.board.topic_id, "claude", "Book direct", "body")

    async with app.run_test() as pilot:
        await type_line(pilot, app, "/conclude force no agreement reached")

    assert board.topic("t")["status"] == "resolved"
    text = (tmp_path / "t-minutes.md").read_text(encoding="utf-8")
    assert "no agreement reached" in text
    assert "Still awaiting a ruling" in text, "an unruled proposal must still show"


@pytest.mark.asyncio
async def test_an_unconcluded_meeting_says_so_in_its_minutes(tmp_path, board):
    from agora.minutes import render
    app = app_for(tmp_path, board)
    board.post(app.board.topic_id, "claude", "an argument", count_turn=False)

    text = render(board, app.board.topic_id)
    assert "has not been concluded" in text


@pytest.mark.asyncio
async def test_a_concluded_meeting_can_be_reopened(tmp_path, board, monkeypatch):
    """Concluding is not meant to be a trap."""
    monkeypatch.chdir(tmp_path)
    app = app_for(tmp_path, board)
    async with app.run_test() as pilot:
        await type_line(pilot, app, "/conclude done")
        assert board.topic("t")["status"] == "resolved"
        await type_line(pilot, app, "/reopen")
        assert board.topic("t")["status"] == "open"
        # And the council can speak again.
        board.post(app.board.topic_id, "claude", "one more thing")


@pytest.mark.asyncio
async def test_the_human_seat_shows_what_it_said_not_a_turn_budget(tmp_path, board):
    """A turn budget is a cost control on metered CLIs. Yours is not metered and
    your posts never spend one, so 0/4 implied a limit that does not exist."""
    app = app_for(tmp_path, board)
    board.post(app.board.topic_id, "me", "first", count_turn=False)
    board.post(app.board.topic_id, "me", "second", count_turn=False)

    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#seats", DataTable)
        rows = {str(table.get_cell_at((r, 0))):
                (str(table.get_cell_at((r, 1))), str(table.get_cell_at((r, 2))))
                for r in range(table.row_count)}

    # The brief is posted as a system message by whoever opened the topic; it is
    # not something they said.
    assert rows["me"] == ("—", "2 said")
    # Agents keep a real budget, because theirs is real.
    assert "/" in rows["claude"][1]
    # And posting still costs an agent nothing.
    assert board.seat(app.board.topic_id, "claude")["turns_used"] == 0

    # When the room is waiting on you, that is what the state column should say.
    board.ask(app.board.topic_id, "claude", "me", "which airport?")
    again = AgoraApp(tmp_path / "board.db", "t", "me")
    again.board.auto = False
    async with again.run_test() as pilot:
        await pilot.pause()
        table = again.query_one("#seats", DataTable)
        row = {str(table.get_cell_at((r, 0))): str(table.get_cell_at((r, 1)))
               for r in range(table.row_count)}
    assert row["me"] == "asked ×1"


@pytest.mark.asyncio
async def test_enter_takes_the_highlighted_hint(tmp_path, board):
    """A list you can walk implies that Enter picks the thing you walked to."""
    from textual.widgets import OptionList

    app = app_for(tmp_path, board)
    async with app.run_test() as pilot:
        await pilot.pause()
        box = app.query_one("#say", Input)
        hint = app.query_one("#hint", OptionList)

        box.value = "/se"
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        chosen = (hint.get_option_at_index(hint.highlighted).id or "").split(" ")[0]

        await pilot.press("enter")
        await pilot.pause()
        assert box.value == chosen + " ", "Enter did not take the highlighted one"
        assert not hint.has_class("showing")

        # But a command you have typed in full runs on the first Enter.
        box.value = "/seats"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert box.value == "", "a complete command should have been submitted"


@pytest.mark.asyncio
async def test_seats_keeps_its_verb_and_corrects_you_in_one_read(tmp_path, board):
    """Guessing that `/seats Santa claude` means add would make the command mean
    different things depending on whether a word happens to be a CLI name. A
    command that quietly reinterprets you is worse than one that says what it
    expected -- so the message has to be enough to fix it in one read."""
    app = app_for(tmp_path, board)
    said: list[str] = []
    async with app.run_test() as pilot:
        app.write_line = lambda item: said.append(str(item))
        app.board.emit = app.write_line

        app.board.handle("/seats Santa claude")
        text = " ".join(said)
        assert "expected add or rm first" in text
        assert "/seats add Santa claude" in text, "it should name the exact fix"

        app.board.handle("/seats add Santa claude")
        assert board.agent("Santa")["kind"] == "claude"
        assert board.seat(app.board.topic_id, "Santa") is not None


@pytest.mark.asyncio
async def test_arrows_walk_what_you_typed_before(tmp_path, board):
    """With no list open, the arrows should mean what they mean in every other
    prompt."""
    app = app_for(tmp_path, board)
    async with app.run_test() as pilot:
        await pilot.pause()
        box = app.query_one("#say", Input)
        await type_line(pilot, app, "first thing")
        await type_line(pilot, app, "second thing")

        await pilot.press("up")
        await pilot.pause()
        assert box.value == "second thing"
        await pilot.press("up")
        await pilot.pause()
        assert box.value == "first thing"
        await pilot.press("down")
        await pilot.pause()
        assert box.value == "second thing"
        await pilot.press("down")
        await pilot.pause()
        assert box.value == "", "stepping past the newest returns to a blank line"


@pytest.mark.asyncio
async def test_clicking_a_proposal_row_opens_it(tmp_path, board):
    """Clicking a row should open that thing, not make you retype an id you can
    already see."""
    app = app_for(tmp_path, board)
    pid = board.propose(app.board.topic_id, "claude", "Book direct",
                        "the whole reasoning")

    said: list[str] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app.write_line = lambda item: said.append(str(item))
        app.board.emit = app.write_line
        app._open_work_row(str(pid))

    text = " ".join(said)
    assert "the whole reasoning" in text and f"/approve {pid}" in text


@pytest.mark.asyncio
async def test_choosing_a_model_sticks_to_the_seat(tmp_path, board):
    app = app_for(tmp_path, board)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._pick_model("codex")
        await pilot.pause()
        # The picker is a modal; answering it is what the callback does.
        app.screen.dismiss("gpt-5.6-sol")
        await pilot.pause()

    import json
    cfg = json.loads(board.agent("codex")["driver_cfg"])
    assert cfg["model"] == "gpt-5.6-sol"


def test_model_lists_fall_back_to_known_names_when_a_cli_cannot_be_asked():
    """Only agy enumerates its models. A picker that only offered a guessed list
    would be wrong the week a new model shipped, hence the text box."""
    import asyncio as aio

    from agora.models import KNOWN, LISTERS, available

    assert "agy" in LISTERS and "codex" not in LISTERS
    assert aio.run(available("codex")) == list(KNOWN["codex"])
    assert aio.run(available("nosuchcli")) == []


@pytest.mark.asyncio
async def test_an_agent_to_agent_question_does_not_veto_the_chair(tmp_path, board,
                                                                 monkeypatch):
    """A real meeting could not be concluded because Gravity had asked Santa
    something. That is a loose end worth recording, not a reason the chair cannot
    end the meeting."""
    monkeypatch.chdir(tmp_path)
    app = app_for(tmp_path, board)
    board.ask(app.board.topic_id, "claude", "codex", "does that hold?")

    async with app.run_test() as pilot:
        await type_line(pilot, app, "/conclude enough for today")

    assert board.topic("t")["status"] == "resolved"
    text = (tmp_path / "t-minutes.md").read_text(encoding="utf-8")
    assert "Questions left unanswered" in text, "it should still be on the record"
    assert "does that hold?" in text
