"""Behavioural tests for the walking skeleton.

These deliberately assert on *computed board state* after a debate actually runs
-- turn counts, cursors, proposal status -- rather than on the shape of the code.
A test that greps for a guard clause goes green while the guard is bypassed
somewhere else; a test that runs the loop and counts the turns does not.
"""

from __future__ import annotations

import asyncio

import pytest

from agora import store as store_mod
from agora.drivers import FakeDriver
from agora.store import NotAuthorised, StoreError, connect
from agora.supervisor import Caps, Supervisor


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
    assert reason == "rounds_exhausted" or "awaits" in reason
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

def test_chinese_text_survives_the_round_trip(board):
    """cp950 is this machine's default codepage; the board must not care."""
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
        assert "agora_propose" in p and "a human holds every decision" in p


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

    board.post(topic, "agora", "paused: claude is waiting on you: @human Where is the engine?",
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
    from agora.store import slugify

    assert slugify("The workflow optimization in agentic AI software development") \
        == "workflow-optimization-in-agentic-ai"
    assert slugify("Should webhook retries use exponential backoff?") \
        == "should-webhook-retries-use-exponential"
    # Chinese keeps its characters instead of slugifying to nothing.
    assert slugify("分家時養贍田該算用益權還是耗用品？") == "分家時養贍田該算用益權還是耗用品"
    # An all-numeric title would produce an unreachable slug, so it is prefixed.
    assert slugify("2026") == "topic-2026"
    # Never silently collide with an existing topic.
    assert slugify("Retry policy", taken=["retry-policy", "retry-policy-2"]) == "retry-policy-3"


def test_a_derived_slug_is_always_acceptable_to_open_topic(board):
    """slugify and open_topic's validation must not disagree -- otherwise /new
    composes a name the store then refuses."""
    from agora.store import slugify

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
    from agora.drivers.base import Seat
    from agora.drivers.spawn import ClaudeDriver

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
    from agora.drivers.base import Seat
    from agora.drivers.spawn import AgyDriver, ClaudeDriver, CopilotDriver

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
    from agora.drivers.spawn import AgyDriver, ClaudeDriver

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
    notes = [m["body"] for m in board.transcript(topic) if m["author"] == "agora"]
    assert any("said nothing" in n for n in notes), "the silence was not reported"
    # The question is still owed, because nothing answered it.
    assert board.open_mentions(topic, "codex")


def test_a_seat_that_does_speak_is_not_reported_as_silent(board):
    topic = open_debate(board)
    driver = FakeDriver(board)
    asyncio.run(Supervisor(board, {"codex": driver}).wake_seat(topic, "codex"))

    notes = [m["body"] for m in board.transcript(topic) if m["author"] == "agora"]
    assert not any("said nothing" in n for n in notes)
