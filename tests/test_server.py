"""The board over HTTP — milestone B1.

The fence is the point. Locally a caller's identity comes from the operating
system; over a socket it comes from a token, so these tests care much more about
who is refused than about JSON shapes.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import pytest_asyncio

from mooting.store import StoreError, connect

aiohttp = pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer          # noqa: E402

from mooting.server import LOOPBACK, STORE, build_app, serve   # noqa: E402

TOKEN = "test-token-not-a-secret"


@pytest.fixture()
def board(tmp_path):
    s = connect(tmp_path / "board.db", init=True)
    s.add_agent("jeremy", "human")
    s.add_agent("santa", "claude", driver="spawn")
    tid = s.open_topic("reno", "Renovation", "Renovation", "jeremy",
                       seats=("santa", "jeremy"))
    s.set_brief(tid, "- what budget\n- which rooms", "jeremy")
    s.post(tid, "santa", "**Direct answer first.**", count_turn=False)
    yield s
    s.close()


def no_spawn(store, topic_id):
    """A supervisor that cannot start a CLI.

    Every fixture here uses one. `default_supervisor` builds real drivers, and a
    single stray POST to /run would spend four subscriptions from a test run.
    """
    from mooting.drivers import FakeDriver
    from mooting.supervisor import Caps, Supervisor
    return Supervisor(store, {"santa": FakeDriver(store)},
                      Caps(max_rounds=1, max_turns_per_seat=1))


@pytest_asyncio.fixture
async def client(tmp_path, board):
    app = build_app(tmp_path / "board.db", TOKEN, human="jeremy",
                    supervisor=no_spawn)
    async with TestClient(TestServer(app)) as c:
        yield c
    app[STORE].close()


def auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


# ----------------------------------------------------------------- the fence

@pytest.mark.asyncio
async def test_every_api_route_refuses_a_missing_or_wrong_token(client):
    """Whoever holds the token *is* that human seat, so an unauthenticated
    caller must not be able to read the board either -- a council transcript is
    not public just because it is not a ruling."""
    for path in ("/api/topics", "/api/topics/reno", "/api/events",
                 "/api/stream?slug=reno"):
        assert (await client.get(path)).status == 401, f"{path} was open"
        assert (await client.get(path, headers=auth("wrong"))).status == 401, path

    r = await client.post("/api/topics/reno/messages", json={"body": "hi"})
    assert r.status == 401, "posting was open"


@pytest.mark.asyncio
async def test_the_page_itself_is_served_without_a_token(client):
    """The page is an empty shell -- it can do nothing until the token it reads
    out of the URL is put into a header."""
    r = await client.get("/")
    assert r.status == 200
    assert "mooting" in (await r.text())


def test_a_non_loopback_bind_is_refused_by_default(tmp_path):
    """Binding an interface hands anyone who reaches it a human seat."""
    with pytest.raises(StoreError) as exc:
        serve(tmp_path / "board.db", host="0.0.0.0", port=0, token=TOKEN,
              human="jeremy")
    assert "refusing to bind" in str(exc.value)
    assert "127.0.0.1" in LOOPBACK


# ------------------------------------------------------------------ reading

@pytest.mark.asyncio
async def test_topics_carry_the_agenda_and_a_reachable_turn_budget(client):
    r = await client.get("/api/topics", headers=auth())
    body = await r.json()
    assert body["you"] == "jeremy"
    topic = body["topics"][0]
    assert topic["slug"] == "reno"
    assert topic["agenda"] == ["what budget", "which rooms"]
    # the same rule the TUI panel uses: never advertise a budget that cannot
    # be spent, because rounds bind before turns do
    santa = next(s for s in topic["seats"] if s["agent"] == "santa")
    assert santa["turns_max"] <= topic["max_rounds"]


@pytest.mark.asyncio
async def test_topic_detail_returns_the_transcript(client):
    r = await client.get("/api/topics/reno", headers=auth())
    body = await r.json()
    assert any(m["author"] == "santa" for m in body["messages"])
    assert "**Direct answer first.**" in body["messages"][-1]["body"], \
        "markdown must arrive as written; rendering is the client's business"


@pytest.mark.asyncio
async def test_an_unknown_topic_is_404_not_500(client):
    r = await client.get("/api/topics/nope", headers=auth())
    assert r.status == 404


# ------------------------------------------------------------------ writing

@pytest.mark.asyncio
async def test_posting_lands_on_the_board_as_the_token_holder(client, board):
    """A message is attributed to whoever's token sent it, not to a seat the
    server was started as -- otherwise two people share one voice."""
    token = board.grant_token("jeremy")
    r = await client.post("/api/topics/reno/messages",
                          json={"body": "the boiler is oil, 1750L a year"},
                          headers=auth(token))
    assert r.status == 200
    tid = int(board.topic("reno")["id"])
    said = [m for m in board.transcript(tid) if m["author"] == "jeremy"]
    assert any("1750L" in m["body"] for m in said)


@pytest.mark.asyncio
async def test_the_shared_token_may_read_but_not_speak(client):
    """The token printed at startup belongs to no seat. It is a way to watch a
    council, not to join one."""
    assert (await client.get("/api/topics", headers=auth())).status == 200
    r = await client.post("/api/topics/reno/messages", json={"body": "hi"},
                          headers=auth())
    assert r.status == 403, "a read token posted a message"


@pytest.mark.asyncio
async def test_an_empty_message_is_refused(client):
    r = await client.post("/api/topics/reno/messages", json={"body": "   "},
                          headers=auth())
    assert r.status == 400


@pytest.mark.asyncio
async def test_a_ruling_needs_a_token_issued_to_a_human_seat(client, board):
    """The one action that cannot be undone. Locally `Store.decide` refuses a
    non-human because identity comes from the operating system; there is none
    over a socket, so the token *is* the identity -- and a token can only ever
    be issued to a human seat."""
    tid = int(board.topic("reno")["id"])
    pid = board.propose(tid, "santa", "Cap at 6", "body")

    # the shared read token is nobody, so it cannot rule
    refused = await client.post(f"/api/proposals/{pid}/decide",
                                json={"approve": True}, headers=auth())
    assert refused.status == 403, "a token belonging to no seat ruled"
    assert board.proposal(pid)["status"] == "open"

    # and an agent can never hold one in the first place
    with pytest.raises(Exception):
        board.grant_token("santa")

    # a human's token rules, and the board records that it came from outside
    token = board.grant_token("jeremy")
    ok = await client.post(f"/api/proposals/{pid}/decide",
                           json={"approve": True, "why": "capped at 6"},
                           headers=auth(token))
    assert ok.status == 200, await ok.text()
    assert board.proposal(pid)["status"] == "approved"

    remote = [e for e in board.events_since(0, tid) if e.kind == "remote"]
    assert any(e.payload.get("action") == "decide" and e.actor == "jeremy"
               for e in remote), "a remote ruling left no audit trail"


# ------------------------------------------------------------------ the stream

@pytest.mark.asyncio
async def test_the_stream_resumes_from_a_cursor_and_carries_the_message(client, board):
    """A client that drops must not miss a round, and must not have to fetch the
    topic again to learn what was said."""
    tid = int(board.topic("reno")["id"])
    before = board.head()
    board.post(tid, "santa", "a second thing", count_turn=False)

    r = await client.get(f"/api/stream?slug=reno&since={before}", headers=auth())
    assert r.status == 200
    assert r.headers["Content-Type"].startswith("text/event-stream")

    frames = ""
    while "a second thing" not in frames:
        chunk = await r.content.read(512)
        if not chunk:
            break
        frames += chunk.decode("utf-8", "replace")
    r.close()

    payloads = [json.loads(line[len("data: "):])
                for line in frames.splitlines() if line.startswith("data: ")]
    said = [p["message"]["body"] for p in payloads if p.get("message")]
    assert "a second thing" in said, f"the message did not ride with its event: {frames[:300]}"
    assert not any("Direct answer first" in b for b in said), \
        "the cursor was ignored and history was replayed"


# ------------------------------------------------------------ B2: actions

@pytest_asyncio.fixture
async def driven(tmp_path, board):
    """A server whose supervisor spawns nothing.

    `default_supervisor` builds real CLI drivers. A test suite that quietly
    starts four subscription CLIs is a trap, and this project has fallen into
    it once already.
    """
    app = build_app(tmp_path / "board.db", TOKEN, human="jeremy",
                    supervisor=no_spawn)
    async with TestClient(TestServer(app)) as c:
        yield c
    app[STORE].close()


@pytest.mark.asyncio
async def test_the_agenda_can_be_set_over_http(driven, board):
    r = await driven.patch("/api/topics/reno",
                           json={"agenda": "cap the retries; who owns the runbook"},
                           headers=auth())
    assert r.status == 200
    assert (await r.json())["agenda"] == ["cap the retries", "who owns the runbook"]


@pytest.mark.asyncio
async def test_rounds_go_through_the_same_rule_as_the_session(driven, board):
    r = await driven.patch("/api/topics/reno", json={"rounds": 7}, headers=auth())
    assert (await r.json())["max_rounds"] == 7
    tid = int(board.topic("reno")["id"])
    assert {s["max_turns"] for s in board.seats(tid)} == {7}, \
        "turns must follow rounds here too, or a seat goes quiet mid-meeting"

    bad = await driven.patch("/api/topics/reno", json={"rounds": 0}, headers=auth())
    assert bad.status == 400


@pytest.mark.asyncio
async def test_a_proposal_is_readable_without_being_rulable(driven, board):
    tid = int(board.topic("reno")["id"])
    pid = board.propose(tid, "santa", "Cap at 6", "**do this**")
    board.vote(pid, "santa", "support", "standard answer")

    r = await driven.get(f"/api/proposals/{pid}", headers=auth())
    body = await r.json()
    assert body["title"] == "Cap at 6"
    assert body["votes"][0]["stance"] == "support"
    assert "human seat" in body["decide"]

    # still refused for the shared token: reading a proposal is not ruling on it
    assert (await driven.post(f"/api/proposals/{pid}/decide",
                              json={"approve": True},
                              headers=auth())).status == 403
    assert board.proposal(pid)["status"] == "open"


@pytest.mark.asyncio
async def test_running_twice_is_refused_so_one_board_has_one_council(tmp_path, board):
    """Two clients pressing Run must not start two councils on one board --
    every seat would be woken twice, on one budget.

    The first council is held open explicitly. With a supervisor that returns
    immediately, whether the refusal is even reachable depends on how the loop
    schedules that first task: on 3.11 it was still pending when the second
    request arrived and on 3.12 it had already finished, so the same code passed
    on one and failed on the other. The guard is "not two at once", so the test
    has to hold one open to mean anything.
    """
    release = asyncio.Event()

    class Held:
        async def run_topic(self, topic_id):
            await release.wait()
            return "released"

    app = build_app(tmp_path / "board.db", TOKEN, human="jeremy",
                    supervisor=lambda store, tid: Held())
    try:
        async with TestClient(TestServer(app)) as c:
            first = await c.post("/api/topics/reno/run", headers=auth())
            assert first.status == 200

            second = await c.post("/api/topics/reno/run", headers=auth())
            assert second.status == 409, "a second council was allowed to start"

            stopped = await c.post("/api/topics/reno/stop", headers=auth())
            assert stopped.status == 200

            # And once it has actually stopped, running again is allowed: the
            # rule is one at a time, not once ever.
            await asyncio.sleep(0)
            again = await c.post("/api/topics/reno/run", headers=auth())
            assert again.status == 200, "the topic stayed locked after stopping"
            assert (await c.post("/api/topics/reno/stop",
                                 headers=auth())).status == 200
    finally:
        release.set()
        app[STORE].close()


@pytest.mark.asyncio
async def test_a_council_actually_runs_and_lands_on_the_board(driven, board):
    tid = int(board.topic("reno")["id"])
    before = len(board.transcript(tid))
    assert (await driven.post("/api/topics/reno/run", headers=auth())).status == 200

    for _ in range(50):
        await asyncio.sleep(0.05)
        if len(board.transcript(tid)) > before:
            break
    assert len(board.transcript(tid)) > before, "the council said nothing"


# ------------------------------------------------------------ several humans

@pytest.mark.asyncio
async def test_two_people_are_two_seats_not_one_shared_voice(client, board):
    """`you` stops meaning one person the moment a council is reachable from
    more than one place. Every ask, ruling and turn on the board is attributed
    by name already; what B3 adds is that a token names *which* name."""
    board.add_agent("alice", "human")
    with board.tx() as c:
        c.execute("INSERT OR IGNORE INTO seats (topic_id, agent) VALUES "
                  "((SELECT id FROM topics WHERE slug='reno'), 'alice')")

    jeremy, alice = board.grant_token("jeremy"), board.grant_token("alice")
    assert jeremy != alice

    for token, who in ((jeremy, "jeremy"), (alice, "alice")):
        r = await client.post("/api/topics/reno/messages",
                              json={"body": f"{who} was here"},
                              headers=auth(token))
        assert r.status == 200, await r.text()

    tid = int(board.topic("reno")["id"])
    said = {m["author"]: m["body"] for m in board.transcript(tid)
            if m["kind"] == "say"}
    assert said.get("jeremy") == "jeremy was here"
    assert said.get("alice") == "alice was here", \
        f"a second person's words landed under somebody else's name: {said}"

    # and a ruling names whoever actually made it
    pid = board.propose(tid, "santa", "Cap at 6", "body")
    r = await client.post(f"/api/proposals/{pid}/decide",
                          json={"approve": True}, headers=auth(alice))
    assert r.status == 200
    assert (await r.json())["by"] == "alice"
    remote = [e for e in board.events_since(0, tid) if e.kind == "remote"]
    assert any(e.actor == "alice" and e.payload.get("action") == "decide"
               for e in remote), "the ruling was not attributed to alice"
