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

from agora.store import connect                          # noqa: E402
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
