"""Behavioural tests for the walking skeleton.

These deliberately assert on *computed board state* after a debate actually runs
-- turn counts, cursors, proposal status -- rather than on the shape of the code.
A test that greps for a guard clause goes green while the guard is bypassed
somewhere else; a test that runs the loop and counts the turns does not.
"""

from __future__ import annotations

import asyncio

import pytest

from mooting import store as store_mod
from mooting.drivers import FakeDriver
from mooting.store import NotAuthorised, StoreError, connect
from mooting.supervisor import Caps, Supervisor


@pytest.fixture()
def board(tmp_path):
    s = connect(tmp_path / "board.db", init=True)
    s.add_agent("human", "human", display="the arbiter")
    for name in ("claude", "codex", "gemini"):
        s.add_agent(name, name, driver="spawn")
    yield s
    s.close()


def open_debate(board, **kw):
    return board.open_topic(
        "retry-policy", "Should failed webhook deliveries use exponential backoff?",
        "The gateway retries on a fixed 30s schedule. Ops says that stampedes on recovery. Decide.",
        "human", seats=("claude", "codex", "gemini", "human"), **kw,
    )


def run(sup, topic_id):
    return asyncio.run(sup.run_topic(topic_id))


# ----------------------------------------------------------------- turn-taking

def test_debate_runs_and_every_seat_speaks(board):
    topic = open_debate(board, max_rounds=2)
    driver = FakeDriver(board)
    sup = Supervisor(board, {"claude": driver, "codex": driver, "gemini": driver},
                     Caps(max_rounds=2, max_turns_per_seat=4))

    reason = run(sup, topic)

    spoke = {m["author"] for m in board.transcript(topic) if m["kind"] == "say"}
    assert spoke == {"claude", "codex", "gemini"}, f"not everyone spoke: {spoke}"
    # The human seat is never woken -- it reads the board on its own schedule.
    assert not any(a == "human" for a, _ in driver.calls)
    assert reason  # terminated with a stated reason rather than spinning


def test_loop_terminates_when_nobody_has_anything_new(board):
    """Settling is structural: a seat only speaks if its cursor is behind. Once
    everyone is caught up, the round yields no speaker and the topic parks."""
    topic = open_debate(board, max_rounds=5)
    silent = FakeDriver(board, script=lambda st, seat, p: None)
    sup = Supervisor(board, {"claude": silent, "codex": silent, "gemini": silent})

    reason = run(sup, topic)

    assert board.topic(topic)["status"] == "paused"
    assert "nothing further to add" in reason or "awaits" in reason
    # A settled council stops at the silence, not at the ceiling. Burning the
    # remaining rounds costs one real billed wake per seat per round and ends at
    # "rounds exhausted", so granting more rounds only bought more of it.
    assert board.topic(topic)["round"] < 4, "ran the budget out on a settled council"
    # Silence must still be bounded: no seat may be woken past its ceiling.
    for s in board.seats(topic):
        assert s["turns_used"] <= s["max_turns"]


# ---------------------------------------------------------------------- caps

def test_seat_turn_cap_is_a_hard_stop(board):
    topic = open_debate(board, max_rounds=99, max_turns=2)
    driver = FakeDriver(board)
    sup = Supervisor(board, {"claude": driver, "codex": driver, "gemini": driver},
                     Caps(max_rounds=99, max_turns_per_seat=2))

    run(sup, topic)

    for s in board.seats(topic):
        if s["kind"] in {"human", "external"}:
            continue
        assert s["turns_used"] <= 2, f"{s['agent']} overspent: {s['turns_used']}"
    assert board.topic(topic)["status"] == "paused", "hitting a cap must pause for a human"


def test_hourly_wake_cap_counts_failures_too(board):
    """A metered CLI charges for a wake that produced nothing, so the ledger
    counts attempts. Otherwise a flapping adapter burns quota invisibly."""
    topic = open_debate(board)
    broken = FakeDriver(board, fail_agents={"claude"})
    sup = Supervisor(board, {"claude": broken}, Caps(max_wakes_per_agent_per_hour=3))

    for _ in range(3):
        asyncio.run(sup.wake_seat(topic, "claude"))

    assert board.wakes_in_last_hour("claude") == 3
    assert sup._next_speaker(topic) != "claude", "capped seat must not be selected again"


# --------------------------------------------------------- human holds the gate

def test_agent_cannot_decide_its_own_proposal(board):
    topic = open_debate(board)
    pid = board.propose(topic, "codex", "Adopt exponential backoff with jitter", "Per the incident review.")

    with pytest.raises(NotAuthorised):
        board.decide(pid, "codex", approve=True)
    with pytest.raises(NotAuthorised):
        board.decide(pid, "claude", approve=True, rationale="I concur")

    assert board.proposal(pid)["status"] == "open"


def test_human_decision_closes_the_proposal_and_lands_on_the_board(board):
    topic = open_debate(board)
    pid = board.propose(topic, "codex", "Adopt exponential backoff with jitter", "Per the incident review.")
    board.vote(pid, "claude", "support", "Matches the sources I read.")

    board.decide(pid, "human", approve=True, rationale="Agreed; ship it behind a flag.")

    p = board.proposal(pid)
    assert (p["status"], p["decided_by"]) == ("approved", "human")
    rulings = [m for m in board.transcript(topic) if m["kind"] == "ruling"]
    assert len(rulings) == 1 and "approved" in rulings[0]["body"]


def test_debate_pauses_once_a_proposal_needs_a_human(board):
    """The loop must not route around a pending decision by debating on."""
    topic = open_debate(board, max_rounds=9)

    def proposer(st, seat, prompt):
        if seat.agent == "codex" and not st.proposals(seat.topic_id):
            st.propose(seat.topic_id, seat.agent, "Adopt backoff with jitter", "Body.")
            return None
        return f"[{seat.agent}] I have read it."

    driver = FakeDriver(board, script=proposer)
    sup = Supervisor(board, {k: driver for k in ("claude", "codex", "gemini")},
                     Caps(max_rounds=9, max_turns_per_seat=8))

    reason = run(sup, topic)

    assert "awaits a human decision" in reason
    assert board.topic(topic)["status"] == "paused"
    assert board.proposals(topic, status="open"), "proposal should still be open"


# ----------------------------------------------- failed wake degrades, not dies

def test_failed_wake_leaves_the_cursor_alone(board):
    """The invariant that keeps a flaky adapter from losing messages."""
    topic = open_debate(board)
    board.post(topic, "claude", "First argument.")
    before = board.seat(topic, "codex")["last_seen"]

    broken = FakeDriver(board, fail_agents={"codex"})
    result = asyncio.run(Supervisor(board, {"codex": broken}).wake_seat(topic, "codex"))

    assert not result.ok
    assert board.seat(topic, "codex")["last_seen"] == before, "cursor moved despite failure"
    assert board.seat(topic, "codex")["state"] == "failed"

    # ...and the catch-up actually contains the message it missed.
    prompt, _ = Supervisor(board, {}).build_prompt(topic, "codex")
    assert "First argument." in prompt


def test_successful_wake_advances_the_cursor(board):
    topic = open_debate(board)
    board.post(topic, "claude", "First argument.")
    driver = FakeDriver(board)

    asyncio.run(Supervisor(board, {"codex": driver}).wake_seat(topic, "codex"))

    assert board.seat(topic, "codex")["last_seen"] > 0
    prompt, _ = Supervisor(board, {}).build_prompt(topic, "codex")
    assert "First argument." not in prompt, "already-seen message replayed"


# ------------------------------------------------------------------- encoding

def test_non_ascii_text_survives_the_round_trip(board):
    """Every text boundary is pinned to UTF-8; the platform default must not
    get a say, or a reply comes back garbled and reads like corruption."""
    topic = open_debate(board)
    body = "重試一定要加抖動（jitter），否則恢復時會同時湧入——見事故報告 §3。"
    mid = board.post(topic, "claude", body)

    reopened = connect(board.path)
    got = [m for m in reopened.transcript(topic) if m["id"] == mid][0]["body"]
    assert got == body
    reopened.close()


def test_topic_is_closed_to_posts_once_resolved(board):
    topic = open_debate(board)
    board.set_topic_status(topic, "resolved", "human")
    with pytest.raises(StoreError):
        board.post(topic, "claude", "one more thing")


# ------------------------------------------------------------------- mentions

def test_at_mention_makes_the_target_answer_next(board):
    """A directed ask jumps the rotation. Waiting for someone's turn to come round
    is not what "@codex, what about X?" means."""
    topic = open_debate(board)
    board.post(topic, "claude", "Fixed interval is fine. @gemini you profiled the recovery — does that hold?")

    sup = Supervisor(board, {})
    assert sup._next_speaker(topic) == "gemini", "mention did not take priority"

    # ...and the question is put in front of them, not buried in catch-up.
    prompt, _ = sup.build_prompt(topic, "gemini")
    assert "Asked of you directly" in prompt
    assert "claude" in prompt.split("Asked of you directly")[1][:200]


def test_answering_discharges_the_mention(board):
    topic = open_debate(board)
    board.post(topic, "claude", "@gemini does that hold?")
    assert len(board.open_mentions(topic)) == 1

    board.post(topic, "gemini", "No — the p99 recovery numbers say otherwise.")
    assert board.open_mentions(topic) == []
    assert Supervisor(board, {})._next_speaker(topic) != "gemini"


def test_explicit_ask_targets_one_seat(board):
    topic = open_debate(board)
    board.ask(topic, "human", "codex", "What does the engine actually do today?")

    asks = board.open_mentions(topic)
    assert len(asks) == 1 and asks[0]["target"] == "codex" and asks[0]["asker"] == "human"
    assert Supervisor(board, {})._next_speaker(topic) == "codex"
    # A human asking must not spend an agent's metered turn.
    assert board.seat(topic, "codex")["turns_used"] == 0


def test_mention_of_an_unseated_name_is_just_text(board):
    """Adding a seat spends money on someone's subscription, so an @ cannot do it."""
    topic = open_debate(board)
    board.post(topic, "claude", "@copilot and @nobody should weigh in")
    assert board.open_mentions(topic) == []
    with pytest.raises(StoreError):
        board.ask(topic, "human", "copilot", "?")


def test_mention_grants_priority_not_extra_budget(board):
    topic = open_debate(board, max_turns=1)
    board.post(topic, "gemini", "spent my only turn")
    board.post(topic, "claude", "@gemini one more thing?")

    sup = Supervisor(board, {}, Caps(max_turns_per_seat=1))
    assert sup._next_speaker(topic) != "gemini", "capped seat woken by a mention"


# ---------------------------------------------------------------- topic modes

def test_debate_and_discuss_frame_the_room_differently(board):
    """The only difference between the modes is what the seat is told -- but a
    seat told 'disagreement is the product' will manufacture some, so it matters."""
    fight = board.open_topic("f", "T", "B", "human", seats=("claude",), mode="debate")
    build = board.open_topic("b", "T", "B", "human", seats=("claude",), mode="discuss")
    sup = Supervisor(board, {})

    debate_prompt, _ = sup.build_prompt(fight, "claude")
    discuss_prompt, _ = sup.build_prompt(build, "claude")

    assert "Disagreement is the product" in debate_prompt
    assert "Disagreement is the product" not in discuss_prompt
    assert "not manufacture an objection" in discuss_prompt
    # Everything else about the two prompts is the same machinery.
    for p in (debate_prompt, discuss_prompt):
        assert "mooting_propose" in p and "a human holds every decision" in p


def test_debate_is_the_default_and_bad_modes_are_refused(board):
    tid = board.open_topic("d", "T", "B", "human", seats=("claude",))
    assert board.topic(tid)["mode"] == "debate"
    with pytest.raises(StoreError):
        board.open_topic("x", "T", "B", "human", seats=("claude",), mode="argue")


# ------------------------------------------------------------- concurrent rounds

def test_a_concurrent_round_wakes_every_eligible_seat_against_one_board(board):
    """The latency fix. Round wall-clock becomes max(seat), not sum(seat), and
    'simultaneous' has to mean every seat saw the same board."""
    topic = open_debate(board, max_rounds=1)
    seen: dict[str, str] = {}

    def record(st, seat, prompt):
        seen.setdefault(seat.agent, prompt)     # the first round is the one under test
        return f"[{seat.agent}] considered"

    driver = FakeDriver(board, script=record, latency_s=0.05)
    sup = Supervisor(board, {k: driver for k in ("claude", "codex", "gemini")},
                     Caps(max_rounds=1))
    run(sup, topic)

    assert set(seen) == {"claude", "codex", "gemini"}
    # Nobody saw a peer's message from their own round -- that is what makes the
    # round safe to run in parallel at all.
    for agent, prompt in seen.items():
        for other in set(seen) - {agent}:
            assert f"[{other}] considered" not in prompt


def test_sequential_mode_still_lets_each_seat_see_the_last(board):
    topic = open_debate(board, max_rounds=3)
    seen: dict[str, str] = {}

    def record(st, seat, prompt):
        seen.setdefault(seat.agent, prompt)
        return f"[{seat.agent}] considered"

    driver = FakeDriver(board, script=record)
    sup = Supervisor(board, {k: driver for k in ("claude", "codex", "gemini")},
                     Caps(max_rounds=3), turn_taking="sequential")
    run(sup, topic)

    later = [p for a, p in seen.items() if "considered" in p]
    assert later, "in sequential mode a later seat must see an earlier one's post"


def test_effort_resolves_topic_over_seat_over_council(board):
    board.add_agent("claude", "claude", driver="spawn", driver_cfg={"effort": "high"})
    hot = board.open_topic("hot", "T", "B", "human", seats=("claude",), effort="low")
    warm = board.open_topic("warm", "T", "B", "human", seats=("claude",))
    sup = Supervisor(board, {}, Caps(effort="medium"))

    assert sup._effort_for(board.topic(hot), "claude") == "low"     # topic wins
    assert sup._effort_for(board.topic(warm), "claude") == "high"   # then the seat

    board.add_agent("codex", "codex", driver="spawn")               # no seat effort
    plain = board.open_topic("plain", "T", "B", "human", seats=("codex",))
    assert sup._effort_for(board.topic(plain), "codex") == "medium"


# ------------------------------------------------ when the council asks the human

def test_a_question_to_a_human_stops_the_room(board):
    """The room waits for whoever was asked. Talking over the person you just
    asked means their answer lands in a conversation that has moved on without
    the fact only they had."""
    topic = open_debate(board)
    board.ask(topic, "claude", "human", "Where does the gateway config live?")

    sup = Supervisor(board, {})
    reason = sup._blocking_reason(topic)
    assert reason and "waiting on you" in reason

    board.post(topic, "human", "ops/gateway.yaml", count_turn=False)
    assert sup._blocking_reason(topic) is None, "answering should release the room"


def test_a_question_to_an_agent_narrows_the_round_to_them(board):
    """Same rule, applied to a peer: nobody else speaks until they answer."""
    topic = open_debate(board)
    board.ask(topic, "claude", "gemini", "You read R08 — does that hold?")

    sup = Supervisor(board, {})
    assert sup._eligible(topic) == ["gemini"]

    board.post(topic, "gemini", "No, p.114 says otherwise.")
    assert set(sup._eligible(topic)) >= {"claude", "codex"}


def test_the_unanswered_question_is_why_the_council_stopped(board):
    """...but once nobody else can proceed, the ask is the reason -- not
    'rounds exhausted', which would bury the one thing needing a person."""
    topic = open_debate(board, max_rounds=1)
    def asker(st, seat, prompt):
        if seat.agent == "claude":
            st.ask(seat.topic_id, seat.agent, "human", "Where does the gateway config live?")
        return None            # the ask is already on the board; say nothing more

    driver = FakeDriver(board, script=asker)
    sup = Supervisor(board, {k: driver for k in ("claude", "codex", "gemini")},
                     Caps(max_rounds=1, max_turns_per_seat=1))
    reason = run(sup, topic)

    assert "waiting on you" in reason and "gateway config" in reason
    assert board.topic(topic)["status"] == "paused"


def test_a_human_answer_clears_every_question_owed_by_them(board):
    """Answering in prose is how people reply; requiring a special gesture would
    leave stale asks forever."""
    topic = open_debate(board)
    board.ask(topic, "claude", "human", "Where is the engine?")
    board.ask(topic, "codex", "human", "Which reading did you mean?")
    assert len(board.open_mentions(topic, "human")) == 2

    spent_before = {s["agent"]: s["turns_used"] for s in board.seats(topic)}
    board.post(topic, "human", "engine/ in the other repo; I meant the second reading.",
               count_turn=False)

    assert board.open_mentions(topic, "human") == []
    # Your answer costs nobody a turn. (The agents did spend one each to ask.)
    assert {s["agent"]: s["turns_used"] for s in board.seats(topic)} == spent_before


def test_a_system_note_quoting_a_question_does_not_ask_again(board):
    """The pause note repeats the question that caused it. Scanning @names out of
    that would ping the person a second time on the board's own behalf."""
    topic = open_debate(board)
    board.ask(topic, "claude", "human", "Where is the engine?")
    assert len(board.open_mentions(topic, "human")) == 1

    board.post(topic, "mooting", "paused: claude is waiting on you: @human Where is the engine?",
               kind="system", count_turn=False)

    assert len(board.open_mentions(topic, "human")) == 1, "system note created a phantom ask"


def test_retuning_effort_mid_run_reaches_the_next_wake(board):
    """The brainstorming dial has to work *while* the council runs: go wide and
    cheap, then turn it up on the branch worth thinking about. The supervisor
    captures Caps once at start, so this only works because wake_seat re-reads the
    topic row each time -- assert the effort the driver actually received."""
    topic = open_debate(board, max_rounds=9)
    got: list[str | None] = []

    def record(st, seat, prompt):
        got.append(seat.effort)
        return f"[{seat.agent}] noted"

    driver = FakeDriver(board, script=record)
    sup = Supervisor(board, {"claude": driver}, Caps(effort="low"))

    asyncio.run(sup.wake_seat(topic, "claude"))
    with board.tx() as c:                       # what /effort high does
        c.execute("UPDATE topics SET effort = 'high' WHERE id = ?", (topic,))
    asyncio.run(sup.wake_seat(topic, "claude"))

    assert got == ["low", "high"], f"mid-run retune did not reach the driver: {got}"


def test_a_slug_that_would_be_unreachable_is_refused(board):
    """Every reference site accepts a slug or an id and picks by isdigit(), so an
    all-numeric slug makes a topic nobody can open by name."""
    with pytest.raises(StoreError, match="topic id"):
        board.open_topic("2026", "Plans", "brief", "human", seats=("claude",))
    with pytest.raises(StoreError, match="single word"):
        board.open_topic("my topic", "Plans", "brief", "human", seats=("claude",))
    board.open_topic("plans-2026", "Plans", "brief", "human", seats=("claude",))
    assert board.topic("plans-2026")["title"] == "Plans"


def test_deleting_a_topic_takes_its_contents_with_it(board):
    topic = open_debate(board)
    board.post(topic, "claude", "something")
    board.propose(topic, "codex", "a proposal", "body")

    counts = board.delete_topic(topic)

    assert counts["messages"] and counts["proposals"]
    assert board.transcript(topic) == []
    assert board.proposals(topic) == []
    assert board.q("SELECT * FROM seats WHERE topic_id = ?", (topic,)) == []
    # wakes carry a topic_id but no foreign key, so the cascade misses them.
    assert board.q("SELECT * FROM wakes WHERE topic_id = ?", (topic,)) == []


def test_reset_clears_topics_but_keeps_the_seats_registry(board):
    open_debate(board)
    board.open_topic("another", "T", "B", "human", seats=("claude",))

    assert board.clear_topics() == 2
    assert board.topics() == []
    assert {a["name"] for a in board.agents()} >= {"claude", "codex", "human"}


def test_a_slug_is_derived_from_the_title(board):
    """Nobody should have to invent a short name for their own question."""
    from mooting.store import slugify

    assert slugify("The workflow optimization in agentic AI software development") \
        == "workflow-optimization-in-agentic-ai"
    assert slugify("Should webhook retries use exponential backoff?") \
        == "should-webhook-retries-use-exponential"
    # a non-Latin script keeps its characters instead of slugifying to nothing.
    assert slugify("分家時養贍田該算用益權還是耗用品？") == "分家時養贍田該算用益權還是耗用品"
    # An all-numeric title would produce an unreachable slug, so it is prefixed.
    assert slugify("2026") == "topic-2026"
    # Never silently collide with an existing topic.
    assert slugify("Retry policy", taken=["retry-policy", "retry-policy-2"]) == "retry-policy-3"


def test_a_derived_slug_is_always_acceptable_to_open_topic(board):
    """slugify and open_topic's validation must not disagree -- otherwise /new
    composes a name the store then refuses."""
    from mooting.store import slugify

    for title in ("2026", "!!!", "   spaces   everywhere   ", "分家", "The the the"):
        slug = slugify(title, [t["slug"] for t in board.topics()])
        board.open_topic(slug, title, "b", "human", seats=("claude",))
    assert len(board.topics()) == 5


def test_removing_a_seat_keeps_what_it_said(board):
    """Tidying the roster must not rewrite the record -- what was said was said."""
    topic = open_debate(board)
    board.post(topic, "codex", "an argument that still counts")

    counts = board.delete_agent("codex")

    assert counts["seats"] == 1 and counts["messages"] == 1
    assert board.seat(topic, "codex") is None
    assert any(m["author"] == "codex" for m in board.transcript(topic))
    with pytest.raises(StoreError):
        board.agent("codex")


def test_the_catchup_excerpt_is_bounded(board):
    """A failed wake leaves the cursor unadvanced, so an unbounded excerpt grows
    every attempt -- which is how a real prompt reached 44,845 chars and blew the
    Windows command-line limit."""
    topic = open_debate(board)
    for i in range(60):
        board.post(topic, "claude", f"message {i} " + "x" * 800, count_turn=False)

    sup = Supervisor(board, {}, Caps(max_catchup_chars=4000))
    prompt, _ = sup.build_prompt(topic, "codex")

    assert len(prompt) < 12_000, f"excerpt not bounded: {len(prompt)}"
    assert "left out" in prompt, "dropping history silently is worse than saying so"
    assert "message 59" in prompt, "the newest exchange is what a seat needs"


def test_an_oversized_argv_prompt_is_reported_as_itself(board):
    """WinError 206 surfaces as FileNotFoundError, so an over-long prompt
    otherwise reports as 'the CLI is not installed'."""
    import asyncio
    from mooting.drivers.base import Seat
    from mooting.drivers.spawn import ClaudeDriver

    d = ClaudeDriver("db")
    seat = Seat(1, "t", "claude", "claude", None, {})
    r = asyncio.run(d.wake(seat, "x" * 30_000))

    assert not r.ok
    assert "30,000 chars" in r.detail and "Windows caps" in r.detail


def test_effort_is_only_sent_where_the_cli_accepts_it(board):
    """An effort setting is a preference. An unsupported one must not cost a seat
    its turn -- both of these failed a real council:
      agy      invalid model selection: gemini-3.1-pro has no "medium" effort
      copilot  Model "auto" does not support reasoning effort configuration
    """
    from mooting.drivers.base import Seat
    from mooting.drivers.spawn import AgyDriver, ClaudeDriver, CopilotDriver

    def seat(cfg=None, effort="medium"):
        return Seat(1, "t", "a", "k", None, cfg or {}, effort=effort)

    # agy's model offers low|high only.
    assert AgyDriver("db").effort_argv(seat(effort="medium")) == []
    assert AgyDriver("db").effort_argv(seat(effort="high")) == ["--effort", "high"]

    # copilot's default model rejects the flag; naming a model is opting in.
    assert CopilotDriver("db").effort_argv(seat()) == []
    assert CopilotDriver("db").effort_argv(seat({"model": "gpt-5.3"})) == \
        ["--effort", "medium"]

    # claude takes all three.
    assert ClaudeDriver("db").effort_argv(seat()) == ["--effort", "medium"]


def test_a_failure_reports_stdout_when_stderr_is_silent(board):
    """agy reports errors as JSON on stdout and exits 1 with stderr empty, which
    surfaced as "agy exited 1: " -- an error message containing no error."""
    from mooting.drivers.spawn import AgyDriver, ClaudeDriver

    agy_json = ('{"conversation_id":"","status":"ERROR","response":"",'
                '"error":"invalid model selection: no \\"medium\\" effort"}')
    detail = AgyDriver("db").failure_detail(1, agy_json, "")
    assert "invalid model selection" in detail

    # And the generic path falls back to stdout rather than reporting nothing.
    assert "boom" in ClaudeDriver("db").failure_detail(1, "boom", "")
    assert "no output" in ClaudeDriver("db").failure_detail(1, "", "")


def test_a_seat_that_says_nothing_is_reported_not_counted_as_an_answer(board):
    """A CLI can exit clean having posted nothing. Recording that as a successful
    wake left a question apparently ignored with nothing on the board to explain
    it -- which is exactly what happened to a real @codex question."""
    topic = open_debate(board)
    board.ask(topic, "human", "codex", "please elaborate")

    silent = FakeDriver(board, script=lambda st, seat, p: None)
    result = asyncio.run(Supervisor(board, {"codex": silent}).wake_seat(topic, "codex"))

    assert result.ok, "the CLI itself did not fail"
    notes = [m["body"] for m in board.transcript(topic) if m["author"] == "mooting"]
    assert any("said nothing" in n for n in notes), "the silence was not reported"
    # The question is still owed, because nothing answered it.
    assert board.open_mentions(topic, "codex")


def test_a_seat_that_does_speak_is_not_reported_as_silent(board):
    topic = open_debate(board)
    driver = FakeDriver(board)
    asyncio.run(Supervisor(board, {"codex": driver}).wake_seat(topic, "codex"))

    notes = [m["body"] for m in board.transcript(topic) if m["author"] == "mooting"]
    assert not any("said nothing" in n for n in notes)


def test_a_post_from_someone_with_no_seat_is_refused(board):
    """Identity is bound when a CLI's MCP server launches. Get it wrong and a
    seat posts under another seat's name -- which happened: a seat named Gravity
    running agy posted as `agy`, and the supervisor then reported Gravity as
    having said nothing."""
    topic = board.open_topic("t2", "T", "B", "human", seats=("claude", "human"))
    board.add_agent("codex", "codex", driver="spawn")   # registered, but not seated

    with pytest.raises(StoreError, match="holds no seat"):
        board.post(topic, "codex", "this would be attributed to the wrong seat")

    # The board itself is not a councillor and may always speak.
    board.post(topic, "mooting", "--- round 2 ---", kind="system", count_turn=False)
    # And a seated agent is unaffected.
    board.post(topic, "claude", "an argument")


def test_the_refusal_names_the_fix(board):
    topic = open_debate(board)
    board.add_agent("Gravity", "agy", driver="spawn")
    try:
        board.post(topic, "Gravity", "posted as the wrong councillor")
    except StoreError as exc:
        assert "mooting install Gravity" in str(exc)
    else:
        raise AssertionError("the post should have been refused")


# ------------------------------------------- bookkeeping failures are visible

def test_a_store_failure_mid_wake_does_not_strand_the_seat(board, monkeypatch, caplog):
    """The driver guard only ever covered `driver.wake`. A board call around it
    -- finishing the wake row, advancing the cursor -- could raise, and the
    exception went straight into `gather(return_exceptions=True)` and was
    dropped. The seat stayed at "waking", which the TUI draws as a seat
    thinking, forever, with nothing anywhere saying why."""
    topic = open_debate(board, max_rounds=1)
    driver = FakeDriver(board)

    def explode(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(board, "advance_cursor", explode)

    sup = Supervisor(board, {"claude": driver}, Caps(max_rounds=1))
    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError):
            asyncio.run(sup.wake_seat(topic, "claude"))

    state = board.seat(topic, "claude")["state"]
    assert state != "waking", "the seat was left mid-wake with no way out"
    assert state == "failed", f"expected a terminal state, got {state!r}"


def test_a_concurrent_round_says_so_when_a_seat_blows_up(board, monkeypatch, caplog):
    """`gather` hands the exceptions back; both call sites used to discard the
    list. Silence is the bug -- the round must not swallow it."""
    topic = open_debate(board, max_rounds=1)
    driver = FakeDriver(board)

    def explode(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(board, "advance_cursor", explode)

    sup = Supervisor(board, {n: driver for n in ("claude", "codex", "gemini")},
                     Caps(max_rounds=1))
    with caplog.at_level("ERROR"):
        asyncio.run(sup.run_topic(topic))

    assert any("database is locked" in r.getMessage() or
               (r.exc_info and "database is locked" in str(r.exc_info[1]))
               for r in caplog.records), \
        "the round finished without a word about the seat that failed"


def test_granting_turns_is_not_undone_by_the_default_cap(board):
    """`/rounds 10` writes the budget onto the seat row. Caps carried its own
    default of 6 and the supervisor took the *minimum*, so a seat granted 10
    stopped at 6 and said it had no turns left -- while the panel still showed
    7/10, because the number on screen was not the number that bound."""
    topic = open_debate(board, max_rounds=10)
    with board.tx() as c:
        c.execute("UPDATE seats SET max_turns = 10, turns_used = 7 WHERE topic_id = ?",
                  (topic,))

    starved = Supervisor(board, {}, Caps())                    # the old behaviour
    assert not starved._has_budget(topic, "claude"), \
        "this test no longer reproduces the bug it guards"

    budget = max(s["max_turns"] for s in board.seats(topic))
    honest = Supervisor(board, {}, Caps(max_turns_per_seat=budget))
    assert honest._has_budget(topic, "claude"), \
        "a seat granted 10 turns was still capped at the council default"


# --------------------------------------------------------------- attachments

def test_text_attachments_are_inlined_because_seats_cannot_open_files(board, tmp_path):
    """A deliberating seat has no file access -- codex runs in an empty sandbox
    on purpose. A path alone would be readable by the one execute-capable seat
    and invisible to the rest, so the council would argue about a document only
    one member had."""
    topic = open_debate(board)
    doc = tmp_path / "policy.md"
    doc.write_text("# Retry policy\n\nfixed 30s, no cap\n", encoding="utf-8")
    board.attach(topic, doc, "human", note="what the gateway does today")

    prompt, _ = Supervisor(board, {}).build_prompt(topic, "claude")
    assert "## Attached" in prompt
    assert "fixed 30s, no cap" in prompt, "the text was not inlined"
    assert "what the gateway does today" in prompt, "the note was dropped"
    assert "policy.md" in prompt


def test_a_binary_attachment_is_named_but_not_inlined(board, tmp_path):
    topic = open_debate(board)
    png = tmp_path / "plan.png"
    png.write_bytes(bytes([137, 80, 78, 71]) + bytes(300))
    board.attach(topic, png, "human")

    prompt, _ = Supervisor(board, {}).build_prompt(topic, "claude")
    assert "plan.png" in prompt
    assert "Not text" in prompt, "a binary was inlined as characters"


def test_a_large_attachment_is_truncated_rather_than_crowding_out_the_turn(board, tmp_path):
    """Source material that fills the prompt is worse than a path: the seat has
    nothing left to think with."""
    topic = open_debate(board)
    big = tmp_path / "log.txt"
    big.write_text("x" * 50_000, encoding="utf-8")
    board.attach(topic, big, "human")

    sup = Supervisor(board, {}, Caps(max_attachment_chars=2_000))
    prompt, _ = sup.build_prompt(topic, "claude")
    assert "Truncated" in prompt
    assert prompt.count("x") < 5_000, "the budget was not enforced"
    assert str(big.name) in prompt


def test_an_attachment_is_copied_so_the_record_survives_the_original(board, tmp_path):
    """Minutes citing a document nobody can open later are minutes of nothing."""
    topic = open_debate(board)
    doc = tmp_path / "spec.md"
    doc.write_text("the original", encoding="utf-8")
    board.attach(topic, doc, "human")
    doc.unlink()                                   # the source goes away

    prompt, _ = Supervisor(board, {}).build_prompt(topic, "claude")
    assert "the original" in prompt, "the attachment did not survive its source"


def test_attaching_something_that_is_not_there_says_so(board, tmp_path):
    topic = open_debate(board)
    with pytest.raises(StoreError):
        board.attach(topic, tmp_path / "nope.md", "human")


def test_a_pause_quotes_the_part_addressed_to_you():
    """Reported from a phone as "those don't seem to be a question", and they
    were not -- they were the opening paragraph of a turn aimed at another seat.

    One message naming two seats writes a mention row for each, and both store
    the whole body, so quoting from the top showed whichever seat was addressed
    first under a heading saying somebody was waiting on *you*."""
    from mooting.supervisor import addressed_to

    body = (
        "@Santa Direct response on both points:\n\n"
        "1. **On hyperscalers/fabless:** I concede that fabless giants do not "
        "run production mask calibration.\n\n"
        "**Final takeaway for @Jeremy:**\n"
        "- **Rarity:** Very rare (~a few thousand globally)."
    )

    mine = addressed_to(body, "Jeremy")
    assert mine.startswith("Final takeaway for @Jeremy"), mine
    assert "hyperscalers" not in mine, "quoted somebody else's paragraph"
    assert "**" not in mine, "markup leaked into a quotation that is italicised"

    # The same body, for the other seat, quotes their part instead.
    theirs = addressed_to(body, "Santa")
    assert theirs.startswith("@Santa Direct response"), theirs


def test_a_pause_falls_back_when_you_were_never_named():
    """A seat can direct a turn at somebody without writing their name, so the
    excerpt has to degrade to the opening rather than to nothing."""
    from mooting.supervisor import addressed_to

    body = "@Kevin - answering directly. I concede point 3 substantially."
    assert addressed_to(body, "Jeremy").startswith("@Kevin - answering")


def _room(tmp_path):
    """A board with one human and one agent seated on a topic."""
    from mooting.store import connect
    st = connect(tmp_path / "asking.db", init=True)
    st.add_agent("Jeremy", "human")
    st.add_agent("Kevin", "agy", driver="spawn")
    tid = st.open_topic("t", "T", "b", "Jeremy", seats=["Jeremy", "Kevin"])
    return st, tid


def _why_stopped(store, tid):
    from mooting.drivers import FakeDriver
    from mooting.supervisor import Supervisor
    return Supervisor(store, {"Kevin": FakeDriver(store)})._blocking_reason(tid)


def test_naming_a_human_in_an_argument_does_not_stop_the_room(tmp_path):
    """The bug this exists for: Kevin ended a turn with "Final takeaway for
    @Jeremy: ..." -- a summary, not a question -- and the council halted waiting
    for an answer to a statement. Reported from a phone as "those don't seem to
    be a question", because they were not."""
    store, tid = _room(tmp_path)
    try:
        store.post(tid, "Kevin", "@Santa most of this is for you.\n\n"
                                 "Final takeaway for @Jeremy: it is a rare role.")
        assert store.open_mentions(tid, "Jeremy"), "the mention is still recorded"
        assert not store.open_mentions(tid, "Jeremy", only_asks=True)
        assert _why_stopped(store, tid) is None, "a summary stopped the council"
    finally:
        store.close()


def test_asking_a_human_still_stops_the_room(tmp_path):
    """The behaviour worth keeping: a real question waits for its answer rather
    than letting the debate move on without the one fact only you had."""
    store, tid = _room(tmp_path)
    try:
        store.ask(tid, "Kevin", "Jeremy", "which foundry are you targeting?")
        assert store.open_mentions(tid, "Jeremy", only_asks=True)
        why = _why_stopped(store, tid)
        assert why and "waiting on you" in why, why
        assert "which foundry" in why, why
    finally:
        store.close()


def test_answering_discharges_it_and_the_room_moves(tmp_path):
    store, tid = _room(tmp_path)
    try:
        store.ask(tid, "Kevin", "Jeremy", "which foundry?")
        assert _why_stopped(store, tid) is not None
        store.post(tid, "Jeremy", "TSMC.", count_turn=False)
        assert _why_stopped(store, tid) is None, "answering left it blocked"
    finally:
        store.close()


def test_migrating_an_old_board_sorts_asks_from_mentions(tmp_path):
    """A board written before the column existed still has to end up with the
    right answer, because taking the safe default left the exact summary that
    prompted all this still holding the room.

    `ask` posts a body opening with `@target `; a name found in prose does not.
    That is enough to tell them apart after the fact."""
    import sqlite3

    from mooting.store import connect

    store, tid = _room(tmp_path)
    path = store.path
    store.post(tid, "Kevin", "Final takeaway for @Jeremy: rare role.")   # named
    store.ask(tid, "Kevin", "Jeremy", "which foundry are you targeting?")  # asked
    store.close()

    # Rewind the column away, as an older board would have it.
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE mentions DROP COLUMN asking")
    raw.commit()
    raw.close()

    again = connect(path, init=True)
    try:
        cols = {r["name"] for r in again.q("PRAGMA table_info(mentions)")}
        assert "asking" in cols, "migration did not add the column"

        rows = again.q("SELECT question, asking FROM mentions")
        named = next(r for r in rows if r["question"].startswith("Final takeaway"))
        asked = next(r for r in rows if r["question"].startswith("@Jeremy which"))
        assert named["asking"] == 0, "a summary still blocks the room"
        assert asked["asking"] == 1, "a real question stopped blocking"

        why = _why_stopped(again, tid)
        assert why and "which foundry" in why, why
    finally:
        again.close()


# --------------------------------------------------------------- catch-up (D)
#
# None of the four below failed before the fix they guard. They are here because
# every one of these faults was silent: the council kept running, the suite kept
# passing, and what a seat had been shown quietly stopped matching what had been
# said.


def test_a_seat_far_behind_is_caught_up_over_several_turns_rather_than_skipped(board):
    """The cursor may only move as far as the seat was actually shown.

    Returning the board head after a truncated fetch advanced a seat past events
    it never saw, and nothing replayed them on any later turn.
    """
    from mooting.supervisor import CATCHUP_EVENTS

    topic = open_debate(board)
    for i in range(CATCHUP_EVENTS + 60):
        board.post(topic, "human", f"point {i}", count_turn=False)

    sup = Supervisor(board, {"claude": FakeDriver(board)})
    prompt, reached = sup.build_prompt(topic, "claude")

    assert reached < board.head(), "cursor jumped past events the seat was not shown"
    assert "events behind" in prompt, "a seat carrying a backlog is not told so"

    # And the remainder is still reachable: the next turn starts where this stopped.
    board.advance_cursor(topic, "claude", reached)
    _, further = sup.build_prompt(topic, "claude")
    assert further > reached


def test_a_message_past_the_first_five_hundred_still_reaches_the_prompt(board):
    """Bodies came from `transcript`'s default window, the oldest 500 of a topic.

    Past that the events still arrived and the text did not, so a seat was handed
    a turn it could not read.
    """
    topic = open_debate(board)
    for i in range(520):
        board.post(topic, "human", f"filler {i}", count_turn=False)
    board.advance_cursor(topic, "claude", board.head())
    board.post(topic, "human", "the point that matters", count_turn=False)

    sup = Supervisor(board, {"claude": FakeDriver(board)})
    prompt, _ = sup.build_prompt(topic, "claude")
    assert "the point that matters" in prompt


def test_the_newest_window_is_the_tail_of_the_topic(board):
    """`transcript(...)[-n:]` was the tail of the oldest 500, not of the topic."""
    topic = open_debate(board)
    for i in range(600):
        board.post(topic, "human", f"m{i}", count_turn=False)

    assert board.message_count(topic) > 500
    tail = board.transcript(topic, limit=3, newest=True)
    assert tail[-1]["body"] == "m599"
    assert len(board.transcript(topic, limit=None)) == board.message_count(topic)


def test_a_council_with_every_seat_capped_stops_instead_of_running_out_the_rounds(board):
    """No seat can speak, so another round changes nothing.

    Advancing anyway woke nobody and posted "round N of M" up to the ceiling,
    which from outside reads like a council still thinking.
    """
    topic = open_debate(board, max_rounds=8)
    driver = FakeDriver(board)
    sup = Supervisor(board, {"claude": driver, "codex": driver, "gemini": driver},
                     Caps(max_turns_per_seat=1))

    reason = run(sup, topic)

    assert "capped" in reason
    assert board.topic(topic)["round"] < 7, "ran the round counter out with nobody to speak"
    assert board.topic(topic)["status"] == "paused"


def test_only_a_person_closes_a_topic_or_changes_the_budget(board):
    """The caps exist to stop a council spending on its own say-so."""
    topic = open_debate(board)

    with pytest.raises(NotAuthorised):
        board.set_topic_status(topic, "resolved", "claude")
    with pytest.raises(NotAuthorised):
        board.grant_rounds(topic, 1, "claude")
    with pytest.raises(NotAuthorised):
        board.set_rounds(topic, 5, "claude")

    # Parking is not a decision: the supervisor does it when a cap is reached.
    board.set_topic_status(topic, "paused", "mooting", "cap reached")
    assert board.topic(topic)["status"] == "paused"

    board.grant_rounds(topic, 1, "human")
    board.set_topic_status(topic, "resolved", "human")
    assert board.topic(topic)["status"] == "resolved"


def test_promoting_a_plan_has_no_path_around_the_human(board):
    """`release_plan` did that with no identity check and was reached from nowhere.

    Its docstring claimed `decide` routed through it. `decide` does the same
    update inline and in one transaction, so that a crash cannot leave a plan
    approved with its work still drafted.
    """
    assert not hasattr(board, "release_plan")


def test_a_settled_council_stops_instead_of_spending_the_rest_of_the_budget(board):
    """Seats that have said their piece are woken again every round otherwise.

    Found on a live board: rounds 5, 6 and 7 were consumed in the same second,
    the chair granted more, and the same thing happened again. `Caps` carried a
    `quiet_rounds_to_settle` setting that nothing ever read.
    """
    topic = open_debate(board, max_rounds=10)
    spoken = set()

    def once(store, seat, prompt):
        if seat.agent in spoken:
            return None
        spoken.add(seat.agent)
        return f"[{seat.agent}] my one point"

    driver = FakeDriver(board, script=once)
    sup = Supervisor(board, {"claude": driver, "codex": driver, "gemini": driver},
                     Caps(max_turns_per_seat=10))

    reason = run(sup, topic)
    wakes = board.q("SELECT COUNT(*) c FROM wakes WHERE topic_id = ?", (topic,))[0]["c"]

    assert "nothing further to add" in reason
    assert board.topic(topic)["round"] <= 2, "kept advancing with nobody speaking"
    # Three seats, one round of answers and one of silence. Ten rounds of this
    # would be thirty billed wakes for three sentences.
    assert wakes <= 8, f"{wakes} wakes to establish the council had finished"


def test_naming_a_person_mid_argument_does_not_freeze_every_agent_seat(board):
    """Found on a live board, reported as "rounds exhausted".

    Santa ended a turn with "Short version, @Jeremy: yes -- ...". That is a
    mention, not an ask. `_eligible` narrowed every later round to the human it
    named, a human seat is never woken, so the eligible list came back empty for
    ever. A named mention is not a reason to stop either, so nothing parked: the
    loop spent the whole round budget in one second and said the rounds had run
    out. Granting more rounds bought another second of the same.
    """
    topic = open_debate(board)
    board.post(topic, "claude", "Short version, @human: yes, with shutters.")

    # Named, and not asked.
    assert board.open_mentions(topic)
    assert not board.open_mentions(topic, only_asks=True)

    sup = Supervisor(board, {"claude": FakeDriver(board)})
    assert set(sup._eligible(topic)) >= {"codex", "gemini"}
    assert sup._human_ask_reason(topic) is None


def test_actually_asking_a_person_still_stops_the_council_for_them(board):
    """The other half: an ask narrows the round and is reported as the reason."""
    topic = open_debate(board)
    board.ask(topic, "claude", "human", "which of the two do you want?")

    assert board.open_mentions(topic, only_asks=True)
    # Nobody else carries on -- the answer would arrive into a moved-on room.
    sup = Supervisor(board, {"claude": FakeDriver(board)})
    assert sup._eligible(topic) == []
    reason = sup._human_ask_reason(topic)
    assert reason and "waiting on you" in reason


def test_a_named_human_does_not_cost_the_council_its_round_budget(board):
    """The symptom the chair actually saw: rounds vanishing with nobody speaking."""
    topic = open_debate(board, max_rounds=15)
    driver = FakeDriver(board, script=lambda st, seat, p: f"[{seat.agent}] @human noted")

    run(Supervisor(board, {"claude": driver, "codex": driver, "gemini": driver},
                   Caps(max_turns_per_seat=15)), topic)

    said = [m for m in board.transcript(topic, limit=None) if m["kind"] != "system"]
    markers = [m for m in board.transcript(topic, limit=None)
               if m["body"].startswith("--- round")]
    # Every round that was spent has seats speaking in it, rather than a run of
    # markers posted in the same second with nothing between them.
    assert len(said) > len(markers), "rounds went by with nobody speaking"


def test_asking_who_you_are_says_where_the_flag_goes(tmp_path, monkeypatch):
    """`--as` is a global option, so it belongs before the command.

    The old message said to pass it and not where, and argparse answers
    `mooting telegram --as Jeremy` with "unrecognized arguments", which reads
    like the flag does not exist. Pairing a second person is what gets somebody
    here: a board with one person answers this on its own, so the command that
    worked yesterday stops the day a colleague joins.
    """
    from mooting.cli import _human

    monkeypatch.delenv("MOOTING_HUMAN", raising=False)
    s = connect(tmp_path / "board.db", init=True)
    s.add_agent("Jeremy", "human")
    s.add_agent("Santa", "claude", driver="spawn")
    try:
        # One person, so it needs no answer.
        assert _human(s, None) == "Jeremy"

        s.add_agent("ege", "human")
        with pytest.raises(SystemExit) as caught:
            _human(s, None)
        said = str(caught.value)
        assert "Jeremy" in said and "ege" in said
        assert "mooting --as" in said, "does not show where the flag goes"

        # Naming one still works, and an agent seat still cannot.
        assert _human(s, "ege") == "ege"
        with pytest.raises(SystemExit):
            _human(s, "Santa")
    finally:
        s.close()


# ------------------------------------------------------------------- the chair
#
# Anybody may call a meeting and argue in it. One person closes it. Before this,
# approving somebody into a room handed them exactly the authority of the person
# who opened it, and "a human decided" stopped identifying which human.


def test_anybody_may_argue_and_only_the_chair_closes_it(board):
    board.add_agent("ege", "human")
    topic = open_debate(board)
    board.seat_human(topic, "ege")
    pid = board.propose(topic, "claude", "Cap at 6", "body")

    # A second person speaks under their own name, as before.
    board.post(topic, "ege", "I think six is too many", count_turn=False)
    assert any(m["author"] == "ege" for m in board.transcript(topic, limit=None))

    with pytest.raises(NotAuthorised) as caught:
        board.decide(pid, "ege", approve=True, rationale="fine by me")
    assert "not the chair" in str(caught.value)
    assert "human" in str(caught.value).lower() or "argue" in str(caught.value)

    board.decide(pid, "human", approve=True, rationale="agreed")
    assert board.proposal(pid)["status"] == "approved"


def test_the_chair_defaults_to_whoever_opened_the_meeting(board):
    """No backfill, and no board where nobody signs off: NULL means the opener."""
    topic = open_debate(board)
    assert board.chair(topic) == "human"
    assert board.topic(topic)["chair"] is None


def test_the_chair_can_be_handed_over_but_not_taken(board):
    board.add_agent("ege", "human")
    topic = open_debate(board)
    board.seat_human(topic, "ege")

    with pytest.raises(NotAuthorised):
        board.set_chair(topic, "ege", "ege")          # not theirs to take
    with pytest.raises(NotAuthorised):
        board.set_chair(topic, "claude", "human")     # an agent cannot chair

    board.set_chair(topic, "ege", "human")
    assert board.chair(topic) == "ege"

    pid = board.propose(topic, "claude", "Cap at 6", "body")
    with pytest.raises(NotAuthorised):
        board.decide(pid, "human", approve=True)      # handed over means handed over
    board.decide(pid, "ege", approve=True, rationale="mine now")
    assert board.proposal(pid)["status"] == "approved"


def test_only_the_chair_concludes_the_meeting(board):
    board.add_agent("ege", "human")
    topic = open_debate(board)
    board.seat_human(topic, "ege")

    with pytest.raises(NotAuthorised):
        board.conclude(topic, "ege", "calling it")
    board.conclude(topic, "human", "calling it")
    assert board.topic(topic)["status"] == "resolved"


def test_renaming_a_person_carries_the_chair_with_them(board):
    """`/me` rewrites a name everywhere. A chair left behind would strand a topic."""
    topic = open_debate(board)
    board.rename_agent("human", "Jeremy")

    assert board.chair(topic) == "Jeremy"
    pid = board.propose(topic, "claude", "Cap at 6", "body")
    board.decide(pid, "Jeremy", approve=True, rationale="still mine")
    assert board.proposal(pid)["status"] == "approved"


def test_the_word_budget_moves_with_effort(board):
    """More thinking time is a reason to say something denser, not longer."""
    from mooting.supervisor import WORDS_BY_EFFORT

    topic = open_debate(board)
    sup = Supervisor(board, {"claude": FakeDriver(board)})

    seen = {}
    for effort in ("low", "high"):
        board.set_effort(topic, effort) if hasattr(board, "set_effort") else None
        with board.tx() as c:
            c.execute("UPDATE topics SET effort = ? WHERE id = ?", (effort, topic))
        prompt, _ = sup.build_prompt(topic, "claude")
        seen[effort] = f"under {WORDS_BY_EFFORT[effort]} words" in prompt

    assert seen["low"] and seen["high"]
    assert WORDS_BY_EFFORT["low"] < WORDS_BY_EFFORT["high"]


def test_a_paired_person_can_rename_themselves(board):
    """`/me` worked in a terminal and failed in a chat.

    `pairings.seat` carries a real foreign key and was missing from the list of
    places a name is written, so the rename moved everything else and then the
    whole transaction failed at COMMIT. Only a paired person has a row there,
    which is why it looked like Telegram was refusing the command.
    """
    board.add_agent("ege", "human")
    topic = open_debate(board)
    board.seat_human(topic, "ege")
    board.pair_approve(board.pair_request("-100", "77", "ege"), "ege", "human")
    board.post(topic, "ege", "six is too many", count_turn=False)

    board.rename_agent("ege", "Ege")

    assert board.seat_for_chat("-100", "77") == "Ege", "the pairing was left behind"
    assert [a["name"] for a in board.agents() if a["kind"] == "human"] == ["Ege", "human"]
    said = [m["author"] for m in board.transcript(topic, limit=None)
            if m["body"] == "six is too many"]
    assert said == ["Ege"], "their words were orphaned"


def test_removing_a_seat_takes_its_pairing_with_it(board):
    """The same foreign key, reached the other way.

    `/seats rm` on somebody paired raised a raw IntegrityError, which is the
    failure the task guard above it exists to prevent. A pairing onto a seat that
    is gone grants nothing and cannot be repaired from a chat.
    """
    board.add_agent("ege", "human")
    board.pair_approve(board.pair_request("-100", "77", "ege"), "ege", "human")

    counts = board.delete_agent("ege")

    assert counts["pairings"] == 1, "the pairing went quietly"
    assert board.seat_for_chat("-100", "77") is None
    assert "ege" not in [a["name"] for a in board.agents()]


def test_removing_the_chair_does_not_strand_the_meeting(board):
    """A meeting must not become undecidable because somebody left.

    The chair was stored by name with no foreign key, so deleting that person
    left the topic pointing at them: everybody else was refused as "not the
    chair", and the name itself was refused as "not a human seat". Nobody could
    sign off, ever.
    """
    board.add_agent("ege", "human")
    topic = open_debate(board)
    board.seat_human(topic, "ege")
    board.set_chair(topic, "ege", "human")
    assert board.chair(topic) == "ege"

    board.delete_agent("ege")

    # Falls back to whoever opened it, and the stored name is cleared rather
    # than left dangling.
    assert board.topic(topic)["chair"] is None
    assert board.chair(topic) == "human"

    pid = board.propose(topic, "claude", "Cap at 6", "body")
    board.decide(pid, "human", approve=True, rationale="mine again")
    assert board.proposal(pid)["status"] == "approved"


def test_a_meeting_whose_opener_is_also_gone_is_still_decidable(board):
    """Second line, for a board where `opened_by` names somebody long gone."""
    board.add_agent("ege", "human")
    topic = open_debate(board)
    board.seat_human(topic, "ege")
    with board.tx() as c:                       # opener no longer on the board
        c.execute("UPDATE topics SET opened_by = 'someone-who-left' WHERE id = ?",
                  (topic,))

    assert board.chair(topic) is None, "named a chair who is not there"

    # Undecidable is the one outcome that is not allowed, so it reverts to the
    # rule that applied before chairs existed: any person may close it.
    pid = board.propose(topic, "claude", "Cap at 6", "body")
    board.decide(pid, "ege", approve=True, rationale="somebody has to")
    assert board.proposal(pid)["status"] == "approved"


# --------------------------------------------------------------------- rooms
#
# A room owns a roster, so a meeting opened there starts with the right seats
# instead of being seated by hand every time — and two groups on one board stop
# sharing a team by accident.


def test_a_room_is_created_the_first_time_it_is_used(board):
    first = board.ensure_room("telegram", "-100123", "engine team")
    again = board.ensure_room("telegram", "-100123")

    assert first == again, "made a second room for the same chat"
    assert board.room("telegram", "-100123")["label"] == "engine team"
    assert board.room("telegram", "nobody-here") is None


def test_work_with_no_chat_behind_it_still_has_a_room(board):
    """A terminal session is not a special case that half the code remembers."""
    channel, chat = board.LOCAL_ROOM
    rid = board.ensure_room(channel, chat)
    assert board.room(*board.LOCAL_ROOM)["id"] == rid


def test_a_room_holds_a_team_in_the_order_it_was_set(board):
    rid = board.ensure_room("telegram", "-100123")
    assert board.room_team(rid) == []

    board.set_room_team(rid, ["claude", "codex"], "human")
    assert board.room_team(rid) == ["claude", "codex"]

    # Replacement, not merge: one command that sticks, one that does not.
    board.set_room_team(rid, ["gemini", "claude"], "human")
    assert board.room_team(rid) == ["gemini", "claude"]


def test_setting_a_team_needs_a_person_and_real_seats(board):
    rid = board.ensure_room("telegram", "-100123")

    with pytest.raises(NotAuthorised):
        board.set_room_team(rid, ["claude"], "claude")
    with pytest.raises(StoreError):
        board.set_room_team(rid, ["nobody-registered"], "human")
    assert board.room_team(rid) == [], "a refused change left something behind"


def test_two_rooms_hold_different_teams(board):
    """The whole point: one board, two groups, no shared seats by accident."""
    abc = board.ensure_room("telegram", "-100111", "team abc")
    dfg = board.ensure_room("telegram", "-100222", "team def")

    board.set_room_team(abc, ["claude", "codex"], "human")
    board.set_room_team(dfg, ["gemini"], "human")

    assert board.room_team(abc) == ["claude", "codex"]
    assert board.room_team(dfg) == ["gemini"]


def test_a_team_follows_a_rename_and_forgets_a_deleted_seat(board):
    """Both foreign keys onto `agents(name)`, and both have bitten before."""
    rid = board.ensure_room("telegram", "-100123")
    board.set_room_team(rid, ["claude", "codex"], "human")

    board.rename_agent("claude", "Santa")
    assert board.room_team(rid) == ["Santa", "codex"], "the team was left behind"

    counts = board.delete_agent("codex")
    assert counts["teams"] == 1, "the seat left the team quietly"
    assert board.room_team(rid) == ["Santa"]


# ------------------------------------------------- the window execution opens
#
# The two-key rule says a seat edits files only when a person approved the task
# and it was woken for one. It says nothing about the shell that comes with the
# editing, and `mooting approve 3` is one command away from there: identity is a
# name, and a seat running as the same user can pass any name.


def _mid_task(board):
    """A board with a seat executing an approved task.

    Written straight into the table rather than through `draft_task`: what is
    under test is what `executing_now` sees, not how a task got there.
    """
    topic = board.open_topic("work", "Work", "b", "human", seats=("human", "claude"))
    with board.tx() as c:
        c.execute("UPDATE topics SET mode = 'work' WHERE id = ?", (topic,))
        c.execute("INSERT INTO tasks (topic_id, title, assignee, created_by, status) "
                  "VALUES (?,?,?,?,'in_progress')",
                  (topic, "Do the thing", "claude", "human"))
    return topic


def test_a_signoff_typed_beside_a_running_seat_is_refused(board):
    topic = open_debate(board)
    pid = board.propose(topic, "claude", "Cap at 6", "body")
    _mid_task(board)

    assert board.executing_now(), "no seat is holding a shell"
    with pytest.raises(NotAuthorised) as caught:
        board.decide(pid, "human", approve=True, rationale="looks right")
    said = str(caught.value)
    assert "holds a shell" in said
    assert "from a chat" in said, "refused without saying the way out"
    assert board.proposal(pid)["status"] == "open"


def test_the_same_signoff_from_a_chat_is_fine(board):
    """A chat account is something the seat does not hold."""
    topic = open_debate(board)
    pid = board.propose(topic, "claude", "Cap at 6", "body")
    _mid_task(board)

    board.decide(pid, "human", approve=True, rationale="from my phone", via="telegram")
    assert board.proposal(pid)["status"] == "approved"


def test_the_window_closes_when_the_task_does(board):
    topic = open_debate(board)
    pid = board.propose(topic, "claude", "Cap at 6", "body")
    _mid_task(board)

    with pytest.raises(NotAuthorised):
        board.decide(pid, "human", approve=True)

    with board.tx() as c:
        c.execute("UPDATE tasks SET status = 'done'")
    assert board.executing_now() == []
    board.decide(pid, "human", approve=True, rationale="now it is quiet")
    assert board.proposal(pid)["status"] == "approved"


def test_concluding_is_held_to_the_same_rule(board):
    """Closing a meeting is a decision too, and reachable the same way."""
    topic = open_debate(board)
    _mid_task(board)

    with pytest.raises(NotAuthorised):
        board.conclude(topic, "human", "calling it")
    board.conclude(topic, "human", "calling it", via="telegram")
    assert board.topic(topic)["status"] == "resolved"


def test_nothing_changes_when_no_seat_is_executing(board):
    """The rule is about an open window, not a permanent tax on the terminal."""
    topic = open_debate(board)
    pid = board.propose(topic, "claude", "Cap at 6", "body")

    assert board.executing_now() == []
    board.decide(pid, "human", approve=True, rationale="ordinary day")
    assert board.proposal(pid)["status"] == "approved"


# --------------------------------------------------------- what a seat reads
#
# Found live: a council asked "how can I make money" answered with the chair's
# age, city and profession. None of it was on the board. The seat was pointed at
# a working directory, and its CLI had four memory files there.


def test_a_directory_with_agent_notes_is_reported(tmp_path):
    from mooting.doctor import context_leaks

    clean = tmp_path / "empty"
    clean.mkdir()
    assert context_leaks(str(clean)) == []

    (tmp_path / "CLAUDE.md").write_text("notes about me", encoding="utf-8")
    found = context_leaks(str(clean))
    assert len(found) == 1, found
    assert found[0].endswith("CLAUDE.md"), "a parent directory's notes are read too"


def test_every_kind_of_notes_counts(tmp_path):
    """Each CLI reads its own file, and a council does not care which."""
    from mooting.doctor import context_leaks

    for name in ("CLAUDE.md", "AGENTS.md", "GEMINI.md", ".cursorrules"):
        room = tmp_path / name.replace(".", "_")
        room.mkdir()
        (room / name).write_text("x", encoding="utf-8")
        assert context_leaks(str(room)), f"{name} went unnoticed"


def test_a_seats_own_context_is_not_its_repository(board):
    """The two settings want opposite things, and one setting chose leaking."""
    from mooting.drivers.base import Seat

    seat = Seat(topic_id=1, topic_slug="t", agent="santa", kind="claude",
                cli_session=None,
                cfg={"cwd": r"C:\empty", "repo": r"C:\dev\project"})
    assert seat.cwd == r"C:\empty"
    assert seat.repo == r"C:\dev\project"

    # A seat with no repository named has none, rather than quietly using the
    # directory it was given to keep a council clean.
    plain = Seat(topic_id=1, topic_slug="t", agent="sam", kind="codex",
                 cli_session=None, cfg={"cwd": r"C:\empty"})
    assert plain.repo is None


def test_usage_counts_what_a_metered_cli_charges_for(board):
    """Wakes, not turns. A wake that fails or produces nothing still cost a
    request, and `turns_used` counts neither."""
    topic = open_debate(board)

    for agent, outcome, secs in (("claude", "ok", 40), ("claude", "error", 5),
                                 ("codex", "ok", 12)):
        wid = board.record_wake(topic, agent)
        with board.tx() as c:
            c.execute("UPDATE wakes SET outcome = ?, "
                      "ended_at = datetime(started_at, ?) WHERE id = ?",
                      (outcome, f"+{secs} seconds", wid))

    by = {r["agent"]: r for r in board.usage()}
    assert by["claude"]["wakes"] == 2
    assert by["claude"]["ok"] == 1
    assert by["claude"]["failed"] == 1, "a failed wake was not counted as spend"
    assert by["claude"]["seconds"] == 45
    assert by["codex"]["failed"] == 0
    assert "gemini" not in by, "a seat that never ran should not appear"


def test_a_window_narrows_it(board):
    topic = open_debate(board)
    old = board.record_wake(topic, "claude")
    with board.tx() as c:
        c.execute("UPDATE wakes SET outcome='ok', "
                  "started_at = datetime('now','-3 hours'), "
                  "ended_at = datetime('now','-3 hours') WHERE id = ?", (old,))
    fresh = board.record_wake(topic, "codex")
    with board.tx() as c:
        c.execute("UPDATE wakes SET outcome='ok', ended_at=started_at WHERE id = ?",
                  (fresh,))

    assert {r["agent"] for r in board.usage()} == {"claude", "codex"}
    assert {r["agent"] for r in board.usage(hours=1)} == {"codex"}


def test_a_wake_still_running_is_not_counted_as_failed(board):
    """`pending` is in flight, not spent badly."""
    topic = open_debate(board)
    board.record_wake(topic, "claude")

    row = board.usage()[0]
    assert row["wakes"] == 1 and row["failed"] == 0 and row["ok"] == 0


# ------------------------------------------------ what the CLI says it spent
#
# Counted by the vendor, not by us: what matters is what came off the
# subscription, and only the CLI knows that. Read defensively, because none of
# them promise to keep the shape.


def test_a_claude_result_envelope_is_read():
    from mooting.drivers.base import usage_in

    got = usage_in('{"type":"result","subtype":"success","total_cost_usd":0.0412,'
                   '"duration_ms":31800,"usage":{"input_tokens":10432,'
                   '"output_tokens":518},"session_id":"abc"}')
    assert got == {"cost_usd": 0.0412, "tokens_in": 10432, "tokens_out": 518}


def test_a_stream_of_objects_is_read_too():
    from mooting.drivers.base import usage_in

    got = usage_in('{"type":"start"}\n'
                   '{"usage":{"prompt_tokens":9,"completion_tokens":3}}')
    assert got == {"tokens_in": 9, "tokens_out": 3}


def test_a_cli_that_says_nothing_is_not_recorded_as_free():
    """Missing is missing. A turn nobody costed stays unpriced."""
    from mooting.drivers.base import usage_in

    assert usage_in("codex says nothing about cost") == {}
    assert usage_in('{"total_cost_usd": ') == {}, "half a line must not be a number"
    assert usage_in("") == {}
    assert usage_in('{"usage": "not a dict"}') == {}


def test_the_ledger_keeps_what_it_was_told_and_nothing_else(board):
    topic = open_debate(board)

    priced = board.record_wake(topic, "claude")
    board.finish_wake(priced, "ok", "", {"tokens_in": 10432, "tokens_out": 518,
                                         "cost_usd": 0.0412})
    silent = board.record_wake(topic, "codex")
    board.finish_wake(silent, "ok", "")

    by = {r["agent"]: r for r in board.usage()}
    assert by["claude"]["tokens_in"] == 10432
    assert by["claude"]["cost_usd"] == 0.0412
    assert by["codex"]["tokens_in"] is None, "a silent CLI was recorded as zero"
    assert by["codex"]["cost_usd"] is None


# ------------------------------------------------------- effort is the dial
#
# One setting for "how much is this question worth": how long a seat thinks,
# how much it may say, and how big the meeting is. Three numbers nobody tunes
# become one somebody already understands.


def test_the_dial_moves_the_budget_with_it(board):
    from mooting.supervisor import BUDGET_BY_EFFORT

    topic = open_debate(board, max_rounds=1)
    high = BUDGET_BY_EFFORT["high"]

    rounds, turns = board.raise_budget(topic, high["rounds"], high["turns"], "human")
    assert rounds == high["rounds"]
    assert turns == high["turns"]
    assert board.topic(topic)["max_rounds"] == high["rounds"]


def test_a_budget_granted_on_purpose_survives_a_lower_setting(board):
    """`set_rounds` has always refused to reduce, and the dial follows it."""
    from mooting.supervisor import BUDGET_BY_EFFORT

    topic = open_debate(board)
    board.set_rounds(topic, 12, "human")

    low = BUDGET_BY_EFFORT["low"]
    rounds, turns = board.raise_budget(topic, low["rounds"], low["turns"], "human")

    assert rounds == 12, "a lower setting took back what was granted"
    assert turns == 12
    assert all(r["max_turns"] >= 12 for r in board.seats(topic))


def test_only_a_person_turns_the_dial(board):
    from mooting.supervisor import BUDGET_BY_EFFORT

    topic = open_debate(board)
    high = BUDGET_BY_EFFORT["high"]
    with pytest.raises(NotAuthorised):
        board.raise_budget(topic, high["rounds"], high["turns"], "claude")


def test_more_effort_is_never_a_smaller_meeting():
    """The dial has to be monotonic or it is not one dial."""
    from mooting.supervisor import BUDGET_BY_EFFORT, WORDS_BY_EFFORT

    order = ("low", "medium", "high")
    for name in ("rounds", "turns"):
        got = [BUDGET_BY_EFFORT[e][name] for e in order]
        assert got == sorted(got), f"{name} is not monotonic: {got}"
    words = [WORDS_BY_EFFORT[e] for e in order]
    assert words == sorted(words), words


# ------------------------------------------------------------- the record
#
# The board says who signed off. Until now nothing made that record answer back
# when somebody edited it, so "you can prove it" was a claim about the design
# rather than about the file.


def _tamper(board, sql, args=()):
    """Edit the board the way somebody with the file would: behind its back."""
    import sqlite3

    path = board.path
    raw = sqlite3.connect(path)
    raw.execute(sql, args)
    raw.commit()
    raw.close()


def test_a_clean_board_verifies(board):
    topic = open_debate(board)
    board.post(topic, "claude", "split units win", count_turn=False)
    pid = board.propose(topic, "claude", "Adopt them", "body")
    board.decide(pid, "human", approve=True, rationale="agreed")

    chain = board.verify_chain()
    assert chain["ok"] and chain["unchained"] == 0
    assert chain["checked"] > 0
    assert board.verify_bodies() == []
    assert board.verify_decisions() == []


def test_rewriting_a_message_stops_matching(board):
    """The body lives in another table, so the chain alone would not notice."""
    topic = open_debate(board)
    board.post(topic, "claude", "split units win", count_turn=False)

    _tamper(board, "UPDATE messages SET body = 'split units are a scam' "
                   "WHERE body = 'split units win'")

    assert board.verify_chain()["ok"], "the chain covers events, not bodies"
    assert board.verify_bodies(), "a rewritten body went unnoticed"


def test_rewriting_who_signed_off_stops_matching(board):
    """The one act this project exists to attribute, in a column beside the chain."""
    topic = open_debate(board)
    pid = board.propose(topic, "claude", "Adopt them", "body")
    board.decide(pid, "human", approve=True, rationale="agreed")

    _tamper(board, "UPDATE proposals SET decided_by = 'claude' WHERE id = ?", (pid,))

    assert board.verify_chain()["ok"], "an intact chain is not a verified record"
    wrong = board.verify_decisions()
    assert wrong and wrong[0]["expected"] == "human" and wrong[0]["found"] == "claude"


def test_removing_an_event_breaks_the_chain(board):
    topic = open_debate(board)
    for i in range(4):
        board.post(topic, "claude", f"point {i}", count_turn=False)
    victim = [e.id for e in board.events_since(0, topic)][2]

    _tamper(board, "DELETE FROM events WHERE id = ?", (victim,))

    chain = board.verify_chain()
    assert not chain["ok"], "a deleted event left the chain adding up"
    assert chain["broken_at"] > victim


def test_editing_an_event_breaks_the_chain_from_there(board):
    topic = open_debate(board)
    for i in range(3):
        board.post(topic, "claude", f"point {i}", count_turn=False)
    target = [e.id for e in board.events_since(0, topic)][1]

    _tamper(board, "UPDATE events SET actor = 'somebody else' WHERE id = ?", (target,))

    chain = board.verify_chain()
    assert not chain["ok"]
    assert chain["broken_at"] == target


def test_events_written_before_the_chain_are_reported_not_hidden(board):
    """History cannot be made tamper-evident afterwards, and saying so is the
    difference between a check and a decoration."""
    topic = open_debate(board)
    board.post(topic, "claude", "written before the column existed",
               count_turn=False)

    # What an existing board looks like the moment the column is added: nothing
    # written so far carries a link, and everything after it does.
    _tamper(board, "UPDATE events SET hash = NULL")
    board.post(topic, "claude", "written after", count_turn=False)

    chain = board.verify_chain()
    assert chain["unchained"] >= 2, "the old rows were not reported as unchecked"
    assert chain["checked"] >= 1
    assert chain["ok"], "an unchained prefix is not a broken chain"


# ------------------------------------------------------------- issue #2
#
# `cli.py` reconfigured stdout and stderr to UTF-8 and left stdin on the locale
# codec. Piped UTF-8 was decoded as cp1252: bytes with a mapping became mojibake
# stored without complaint, and bytes without one became lone surrogates that
# failed at the SQLite write, three layers below the mistake.


def test_piped_utf8_survives_the_read(tmp_path, monkeypatch):
    """The read side is reconfigured, so `-` input arrives as it was written."""
    import io
    import sys

    from mooting.cli import read_stdin

    # `丁` is U+4E01: its UTF-8 is E4 B8 81, and 0x81 is one of the five bytes
    # cp1252 leaves undefined, which is what produced the surrogate.
    raw = io.TextIOWrapper(io.BytesIO("丁 split units".encode("utf-8")),
                           encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", raw)
    got = read_stdin("--brief")

    assert got == "丁 split units"
    got.encode("utf-8")           # would raise if a surrogate had come through


def test_a_bad_read_names_the_argument(tmp_path, monkeypatch):
    """The traceback used to point at `open_topic`, three layers from the cause."""
    import io
    import sys

    from mooting.cli import read_stdin

    raw = io.TextIOWrapper(io.BytesIO(b"\xff\xfe not utf-8"), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", raw)

    with pytest.raises(SystemExit) as caught:
        read_stdin("--brief")
    assert "--brief" in str(caught.value), "the failure did not say what it was reading"


def test_surrogates_are_refused_where_they_enter_the_board(board):
    """They arrive from anywhere text is decoded with `surrogateescape`, not only
    from stdin, and they survived every layer until SQLite refused them."""
    from mooting.store import clean_text

    bad = "split units \udc81 win"
    with pytest.raises(StoreError) as caught:
        clean_text(bad, "the brief")
    assert "the brief" in str(caught.value)

    topic = open_debate(board)
    with pytest.raises(StoreError):
        board.post(topic, "claude", bad, count_turn=False)
    with pytest.raises(StoreError):
        board.propose(topic, "claude", "fine", bad)
    with pytest.raises(StoreError):
        board.open_topic("bad", bad, "b", "human", seats=("claude",))


def test_ordinary_text_passes_through_untouched(board):
    from mooting.store import clean_text

    for text in ("丁 split units", "", "plain ascii", "emoji 🙂 and ünïcode"):
        assert clean_text(text, "the message") == text


def test_the_version_has_one_answer():
    """Three disagreed -- 0.0.1 in the package, 0.1.1 in pyproject, 0.1.0
    installed -- so a report could not say which version it was against."""
    import importlib.metadata

    import mooting

    assert mooting.__version__ == importlib.metadata.version("mooting")
    assert mooting.__version__ != "0.0.1", "the hand-maintained literal is back"


# ------------------------------------------------------------- issues #4, #5


def _work_board(board):
    topic = open_debate(board)
    with board.tx() as c:
        c.execute("UPDATE topics SET mode = 'work' WHERE id = ?", (topic,))
        c.execute("UPDATE seats SET role = 'manager' WHERE topic_id = ? AND agent = 'claude'",
                  (topic,))
    return topic


def _task(board, topic, status="assigned", title="Do the thing"):
    with board.tx() as c:
        cur = c.execute("INSERT INTO tasks (topic_id, title, assignee, created_by, status) "
                        "VALUES (?,?,?,?,?)", (topic, title, "codex", "claude", status))
        return int(cur.lastrowid)


def test_granting_rounds_raises_the_turns_to_use_them(board):
    """#4. The resume path raised one of the two with a raw UPDATE, so a council
    with spent seats looked alive and said nothing."""
    topic = open_debate(board)
    with board.tx() as c:
        c.execute("UPDATE seats SET turns_used = max_turns WHERE topic_id = ?", (topic,))

    before = {r["agent"]: r["max_turns"] for r in board.seats(topic)}
    board.grant_rounds(topic, 4, "human")
    after = {r["agent"]: r["max_turns"] for r in board.seats(topic)}

    assert board.topic(topic)["max_rounds"] > 3
    assert all(after[a] == before[a] + 4 for a in before), "turns did not follow rounds"


def test_a_seat_cannot_grant_the_council_more_rounds(board):
    """The raw UPDATE had no identity check, so `--as <agent> --resume --rounds`
    let a seat top up its own budget."""
    topic = open_debate(board)
    with pytest.raises(NotAuthorised):
        board.grant_rounds(topic, 4, "claude")


def test_a_manager_can_send_work_back_to_the_queue(board):
    """#5. `rejected` was the only way to say "do it again", and it dropped the
    task out of every code path that reads one."""
    topic = _work_board(board)
    tid = _task(board, topic, status="blocked")

    board.update_task(tid, "claude", "assigned", "the cause is fixed, please rerun")

    assert board.task(tid)["status"] == "assigned"
    queued = [t["id"] for t in board.tasks(topic, status="assigned")]
    assert tid in queued, "the task did not come back to the queue"


def test_rejecting_is_still_terminal(board):
    """It should mean abandoned, which is why it needed a sibling."""
    topic = _work_board(board)
    tid = _task(board, topic, status="done")

    board.update_task(tid, "claude", "rejected", "not worth doing after all")
    assert board.tasks(topic, status="assigned") == []


def test_a_decided_task_is_not_reported_as_work_left_over(board):
    """`work paused: 6 rejected` read like six failures rather than six calls."""
    from mooting.supervisor import Caps, Supervisor

    topic = _work_board(board)
    for _ in range(3):
        board.update_task(_task(board, topic, status="done"), "claude", "rejected", "no")
    sup = Supervisor(board, {}, Caps())

    summary = sup._work_summary(topic)
    assert "paused" not in summary, summary
    assert "3 rejected" in summary

    _task(board, topic, status="assigned")
    summary = sup._work_summary(topic)
    assert "1 assigned" in summary
    assert "abandoned earlier" in summary, "the abandoned ones were counted as pending"


def test_copilot_never_sends_effort_with_the_auto_model(board):
    """#4, separately. `auto` rejects the flag and fails every wake, and it is
    the cheap default a person would reasonably name."""
    from mooting.drivers.base import Seat
    from mooting.drivers.spawn import CopilotDriver

    driver = CopilotDriver(board.path)
    for model, expect in ((None, []), ("", []), ("auto", []),
                          ("claude-opus-4.8", ["--effort", "low"])):
        seat = Seat(topic_id=1, topic_slug="t", agent="copilot", kind="copilot",
                    cli_session=None, cfg={"model": model} if model is not None else {},
                    effort="low")
        assert driver.effort_argv(seat) == expect, model


# ------------------------------------------------- what the review found (#3)


def test_http_is_on_this_machine_too(board):
    """The executing-seat guard ran only for the terminal, so the same window
    reopened through the server: a seat with a shell can read a token off the
    board and make the request itself."""
    topic = open_debate(board)
    pid = board.propose(topic, "claude", "Cap at 6", "body")
    work = board.open_topic("w", "W", "b", "human", seats=("human", "claude"))
    with board.tx() as c:
        c.execute("UPDATE topics SET mode = 'work' WHERE id = ?", (work,))
        c.execute("INSERT INTO tasks (topic_id, title, assignee, created_by, status) "
                  "VALUES (?,?,?,?,'in_progress')", (work, "t", "claude", "human"))

    for route in ("local", "http"):
        with pytest.raises(NotAuthorised):
            board.decide(pid, "human", approve=True, via=route)
    # A chat is genuinely elsewhere: the bot token lets a seat speak as the bot,
    # not as a paired person, and the sender is what the decide path checks.
    board.decide(pid, "human", approve=True, rationale="from my phone", via="telegram")
    assert board.proposal(pid)["status"] == "approved"


def test_a_handle_is_worth_guessing_at(board):
    """24 bits is cheap online and collides on a board that lives a while."""
    from mooting.store import HANDLE_BYTES, new_handle

    assert HANDLE_BYTES >= 4
    handles = {new_handle() for _ in range(500)}
    assert len(handles) == 500, "handles collided in five hundred draws"
    assert all(len(h) == HANDLE_BYTES * 2 for h in handles)


def test_a_terminal_sees_the_whole_board(board):
    """`None` asks as the terminal, and it administers the rooms."""
    room = board.ensure_room("telegram", "-100111")
    mine = board.open_topic("in-a-chat", "In a chat", "b", "human",
                            seats=("claude",), room_id=room)
    board.open_topic("at-the-desk", "At the desk", "b", "human", seats=("claude",))

    seen = {t["slug"] for t in board.topics_for_room(None)}
    assert seen == {"in-a-chat", "at-the-desk"}, seen
    assert board.topic_visible_in(mine, None), "the terminal could not see a chat's topic"


def test_a_room_still_sees_only_its_own(board):
    room = board.ensure_room("telegram", "-100111")
    other = board.ensure_room("telegram", "-100222")
    board.open_topic("theirs", "Theirs", "b", "human", seats=("claude",), room_id=other)

    seen = {t["slug"] for t in board.topics_for_room(room)}
    assert "theirs" not in seen


def test_a_room_cannot_answer_a_request_by_its_number(board):
    """Accepting the id inside a room left the sequence guessable, which is what
    the handle was added to stop."""
    pid = board.pair_request("-100111", "42", "Someone")
    row = board.pairing("-100111", "42")

    assert board.pairing_by_ref(row["ref"], chat_id="-100111") is not None
    assert board.pairing_by_ref(str(pid), chat_id="-100111") is None
    # A terminal may still use the number: it sees the whole board regardless.
    assert board.pairing_by_ref(str(pid)) is not None


# --------------------------------------------------------------- issue #6


def _work(board):
    topic = open_debate(board)
    with board.tx() as c:
        c.execute("UPDATE topics SET mode = 'work' WHERE id = ?", (topic,))
        c.execute("UPDATE seats SET role = 'manager' WHERE topic_id = ? AND agent = 'claude'",
                  (topic,))
    # Written the way `agents add --capability execute` writes it.
    with board.tx() as c:
        for name in ("claude", "codex", "gemini"):
            c.execute("UPDATE agents SET driver_cfg = ? WHERE name = ?",
                      ('{"capability": "execute"}', name))
    return topic


def _queue(board, topic, assignee, depends_on=None):
    with board.tx() as c:
        cur = c.execute("INSERT INTO tasks (topic_id, title, assignee, created_by, "
                        "status, depends_on) VALUES (?,?,?,?,'assigned',?)",
                        (topic, f"work for {assignee}", assignee, "claude", depends_on))
        return int(cur.lastrowid)


def test_one_seat_is_never_woken_twice_at_once(board):
    """agy woken for two of its own tasks timed out on both, while the same
    invocation by hand succeeded in 43 seconds. A seat is one CLI holding one
    conversation."""
    from mooting.supervisor import Caps, Supervisor

    topic = _work(board)
    _queue(board, topic, "gemini")
    _queue(board, topic, "gemini")
    _queue(board, topic, "codex")

    runnable = Supervisor(board, {}, Caps())._runnable_tasks(topic)
    assignees = [t["assignee"] for t in runnable]

    assert sorted(assignees) == ["codex", "gemini"], assignees
    assert len(assignees) == len(set(assignees)), "a seat was dispatched twice"


def test_a_task_waits_for_the_one_it_follows(board):
    """The manager could work out that two tasks touch the same files and could
    only write it where nothing read it."""
    from mooting.supervisor import Caps, Supervisor

    topic = _work(board)
    first = _queue(board, topic, "codex")
    second = _queue(board, topic, "gemini", depends_on=first)
    sup = Supervisor(board, {}, Caps())

    assert [t["id"] for t in sup._runnable_tasks(topic)] == [first]

    board.update_task(first, "codex", "done")
    board.update_task(first, "claude", "accepted", "good")
    assert [t["id"] for t in sup._runnable_tasks(topic)] == [second]


def test_a_dependency_has_to_name_a_real_task(board):
    """The column carries a foreign key, so "after #9999" cannot be written at
    all -- better than depending on the loop to cope with it later."""
    import sqlite3

    topic = _work(board)
    with pytest.raises(sqlite3.IntegrityError):
        _queue(board, topic, "codex", depends_on=9999)


def test_sequential_covers_work_too(board):
    """The flag read as though it would help and was consulted only on the
    speaking path, so work ran in parallel anyway."""
    from mooting.supervisor import Caps, Supervisor

    topic = _work(board)
    _queue(board, topic, "codex")
    _queue(board, topic, "gemini")

    parallel = Supervisor(board, {}, Caps())._runnable_tasks(topic)
    one_at_a_time = Supervisor(board, {}, Caps(),
                               turn_taking="sequential")._runnable_tasks(topic)

    assert len(parallel) == 2
    assert len(one_at_a_time) == 1
