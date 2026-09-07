# Roadmap

What is planned, in the order it should happen, and what is deliberately not
planned. [Why it works this way](WHY.md) is the record of decisions already
taken; this is the record of ones still open.

Every item is filtered on one question: **does it serve the human-only
decision?** That is the property no comparable project has, so it is the one
worth spending on. Anything that weakens it is under
[Not building](#not-building) with the reason.

## Where this stands

Surveyed 2026-08-31 against the live GitHub API and each project's own
documentation. Star counts and activity are one reading on one day.

| project | sign-off | model access | seats |
|---|---|---|---|
| **LoopTroop** (129★, active) | its own README labels the approval step "optional in future releases" | one OpenCode engine, provider-level choice | not vendor CLIs |
| **Concord MCP** (293★ in six weeks, active) | none — closing a task checks ownership, and any owning agent qualifies | none held, same as Mooting | five vendor CLIs |
| **Senate** (2★, quiet since 2026-08-05) | none — a debate skill with per-run files, no continuing board | none held | subprocess, same shape as Mooting |
| **Claude Code agent teams** | ordinary CLI permissions | your subscription | Claude only |
| **GitHub Agent HQ** | a review workflow | mediated by a paid Copilot seat | many vendors, at platform scale |

**No comparable project makes the human decision structural.** The two largest —
Claude Code's own agent teams and GitHub's Agent HQ — decide through ordinary
product permissions, which a prompt injection or a misconfiguration can erode.

Cross-vendor breadth is a real advantage and probably a temporary one, since any
vendor can add another model faster than this project can grow. **The durable
property underneath it is the record itself: who argued, who signed, kept by
someone other than the party being audited.** A vendor cannot ship that. Track J
is what makes that record worth the name, which is why it now comes first.

`POSITIONING.md` at the repository root is the argument this ordering follows.

## D — Correctness ✅ done

In the working tree, not yet released. Worth cutting as 0.1.2 on its own,
before anything in J.

Five silent faults, all of which got worse the longer a council ran, none of
which failed a test. Seven regression tests came with them; reverting D1 or D2
makes them fail.

| | |
|---|---|
| D1 | A seat more than one wake's backlog behind had its position advanced past events it was never shown. `build_prompt` now returns the last event actually included, and a seat carrying a backlog is told so. |
| D2 | Prompt bodies came from the oldest 500 messages of a topic. `transcript` grew `newest=` and `limit=None`, `messages_by_id` and `message_count` were added, and nine call sites across six files were converted. |
| D3 | The board's drive claim guarded the full-screen view only. It now guards the console, the HTTP server and the chat bot, so two sessions cannot wake every seat twice on one budget. |
| D4 | `release_plan` promoted drafts to assigned work with no identity check and was reached from nowhere; it is gone. `set_topic_status` refuses a non-human for `resolved` and `aborted`, and `set_rounds` and `grant_rounds` require a person. |
| D5 | A round with every seat capped ran the counter to its ceiling posting "round N of M". It parks with the reason instead. |

## J — The sign-off, made true and checkable

**This is the top of the roadmap because it is the difference between a claim
and a fact.** The project says an agent cannot sign off. That is true of the
protocol and not yet true of the machine, and the gap is small enough to close.

| | |
|---|---|
| J1 ✅ | **Done.** A decision typed on this machine while a seat is executing is refused, and says the way out: a chat account is something the seat does not hold. It closes the one window this project opens itself rather than pretending to fence the machine, which is not this project's to build. Superseded text below. |
| ~~J1~~ | **Close the window an execute-capable seat can reach.** A deliberating seat has neither the tool nor a shell — the adapters narrow it to the Mooting server, deny shell and write, or run in plan mode. A seat woken for an approved task has those restrictions dropped so it can work, which gives it a shell, and the shell reaches `mooting approve`. `Store.decide` identifies a person by name, and the CLI resolves that name on its own when a board has one human seat. Route sign-off through a credential a seat has never held. `grant_token` and `seat_for_token` already exist and only the HTTP surface uses them. |
| J2 ✅ | **Done.** Every event is chained to the one before it, a message's body digest travels in the event that announces it, and each sign-off is checked against the event's own actor — because an intact chain with a rewritten `decided_by` is the failure this project would care about most, and checking only the chain would miss it. `mooting doctor` reports all three, and says how many events predate the chain rather than pretending history can be made tamper-evident afterwards. |
| ~~J2~~ | **Make the record tamper-evident.** The board is an ordinary SQLite file with thirteen tables and no chaining. It records who signed off; nothing resists a later edit. Chain each event to its predecessor and give `mooting doctor` a pass that verifies the chain. This is what turns "the record is legible" into "the record is checkable", and it is the claim the audience actually needs. |
| J3 ✅ | **Done.** `tests/test_audit.py` reads every tool the MCP server registers and fails if one reads as closing a proposal, matched on words rather than an exact spelling. Verified by adding a `mooting_decide` tool and watching it go red. The refusal in `Store.decide` is pinned beside it, so both lines are held by a check anyone can run. Superseded text below. |
| ~~J3~~ | **Pin the absence in CI.** A test that enumerates every tool the MCP server registers and fails if a decide-shaped one ever appears. Link it from the README as the test that keeps it true. It converts the central claim from something a reader trusts to something they reproduce, and it goes red for the maintainer too. |
| J4 ✅ | **Done.** `mooting doctor` opens a throwaway proposal per seat and asks that CLI to approve it. Each seat's line says it could not; a seat that finds a way is named and the run exits non-zero. A probe that raises is logged and does not sink the report, because the report is the reason the command exists. |
| ~~J4~~ | **Add the refusal to `doctor`.** It already spends one real turn per seat, because exit codes lie. Ask each seat to approve its own proposal and report that none of them could. That is the sixty-second demonstration, and it exercises the property that matters most against the real CLIs. |
| J5 ✅ | **Done.** `docs/assets/signoff.png` — every seat in support, the proposal still open, the status bar asking the chair. Exported from the real TUI through Textual's pilot against a scripted board, so it cannot show something the code does not do. It sits in the README under the claim it is evidence for, and in its own section on the landing page. |
| ~~J5~~ | **Produce the one-screen proof.** A board where all four seats are in favour and the status still reads `draft — awaiting your sign-off`, then one command from the chair. `tools/screenshot.py` already drives the real TUI through Textual's pilot, so this is a scripted board rather than new machinery. |

## R — The room

**One model answers three separate asks, which is why it comes before the rest.**
Different teams need different rooms; a team should be something you set up once
rather than seat by hand; and the chair should have one dial that says how much a
meeting is worth. All three are the same object.

> **A room has a team and a chair. A meeting opened in a room inherits both.
> Effort is what the chair turns to say how much this question is worth, and it
> caps what the meeting may spend.**

| | |
|---|---|
| R1 ✅ | **Done.** `rooms` and `room_seats` hold a roster per room, `/team` sets and shows it, and a meeting opened there starts with those seats. Two rooms on one board keep separate teams — a direct message with one seat and a group with three, tested both ways. Superseded text below. |
| ~~R1~~ | **A room owns its team.** Seats are per topic today, so every new meeting is seated by hand and two groups sharing a board share their seats. Give a room a roster — its own seats, their models and working directories — and a topic opened there starts with it. |
| R2 ✅ | **Done.** A topic carries `room_id`, `topic_visible_in` gates every event the pump sends, and the `/topics` list is scoped the same way. This is what stopped a second group reading the first group's council live. Superseded text below. |
| ~~R2~~ | **A topic belongs to the room that opened it.** This is what makes two teams real, and it closes a leak: the event pump sends every event from every topic to every paired chat (`events_since(cursor, None)` and every listener), so a second group reads the first group's council live. Binding topics to rooms fixes the visibility and the `/topics` list at once. |
| R3 ✅ | **Done.** One dial: how long a seat thinks, how much it may say, and how big the meeting is. `/effort` shows all three levels and what each costs, and turning it raises rounds and turns to match — raising only, because a budget somebody granted on purpose is not something a later setting should take back. The per-hour wake ceiling stays board-wide and is now visible in `/usage` instead, since it is contention between rooms rather than a property of one meeting. |
| ~~R3~~ | **Effort sets the budget, not just the thinking.** It already picks reasoning depth and now the word budget. Make it the single dial: rounds, seats woken per round and the per-hour ceiling all derive from it, so `low` is a quick second opinion and `high` is a real deliberation. The chair sets it, and setting it is the whole cost conversation. |
| R4 ✅ | **Done as `/usage`.** Wakes, failures and time per seat, with each seat's turns on this topic and its headroom against the hourly ceiling — which is per agent across the whole board, so two rooms compete for it and neither could see why the other slowed down. The wake ledger had been recording this since the beginning and nothing ever showed it. |
| ~~R4~~ | **Report what a meeting cost when it ends.** Turns spent per seat, wakes, and what that was against the budget. Nothing shows this today, and "how much did that cost" is the question the effort dial is answering. |

## E — The chair's own loop

**These four are the human-only decision under-served by its own surfaces.**
Each is a place where the person who decides cannot reach the thing they decide
about.

| | |
|---|---|
| E1 ✅ | **Done as `/tasks accept|reject|again <id> <why>`, not `/approve`.** `/reject` already closes a proposal, and a command that means one of two things depending on which table the number is in is the thing `/seats` refuses to be. `Store.update_task` had accepted the chair all along; nothing exposed it. Testing it as a human manager found the other half: `draft_task` and `submit_plan` were unreachable too, so the plan could not be written either -- `/tasks add` and `/tasks plan` close that. A finished task also reaches the chat now -- it arrived as a system message, which the noise filter drops, so from a phone work was reported into silence and there was nothing to accept. Superseded text below. |
| ~~E1~~ | **A person managing a work topic cannot finish it.** Accepting or rejecting a finished task exists only as an MCP tool, so only an agent seat can do it. Work is reported complete only when every task is accepted, so a topic managed by a person never completes. Add `/accept` and `/reject`. |
| E2 | **The browser watches but cannot act.** The served page makes three read requests and carries no controls, while every write route already exists. |
| E3 ✅ | **Done by correcting the table, not by building the routes.** Every documented path omitted `/api`, `POST /topics` and `GET /topics/{slug}/minutes` did not exist, and the SSE stream was documented at the polling path -- so following `REMOTE.md` produced 404s all the way down. The table is the routing table now, the two absent routes are named as absent, and a test compares the two so it cannot drift again. Building them is a separate decision nobody has needed yet. Superseded text below. |
| ~~E3~~ | **The documented HTTP surface is not the one that runs.** `POST /topics` and `GET /topics/{slug}/minutes` do not exist, `/api/events` is the polling form rather than the stream, and every documented path omits the `/api` prefix. Build the two routes or correct the table. |
| E4 ✅ | **Done.** One entry in the console's dispatch table, so the console, the full-screen view and the chat bot all answer it. Superseded text below. |
| ~~E4~~ | **`/attach` is advertised in six places and reachable from none of them.** It is missing from the console's command table, so the console, the full-screen view and the chat bot all answer "unknown". The shell `mooting attach` works, and sending a document to the chat works. One entry in the dispatch table covers all three surfaces. |

## F — Tests where the invariants live

**Cover the surfaces that hold the promises before adding to them.** `web.py`
has no test at all, including the loopback check that is the only thing between
an unauthenticated live session and the network. `mcp_server.py` — everything an
agent can see — has no functional test, only a count of its decorators.

## G — Worth taking from elsewhere

| | |
|---|---|
| G1 ◐ | **Half done, and the other half needs a machine this is not.** `mooting doctor` now names any execute-capable seat whose adapter narrows nothing -- copilot today -- as a static check that costs no turn. That makes the premise visible instead of latent. The sandbox itself needs `bwrap` (Linux) or `seatbelt` (macOS); neither exists on Windows, so it cannot be built *and probed* here, and this session's rule is that an unprobed claim about a driver is not a claim. Left for a Linux or macOS machine. Superseded text below. |
| ~~G1~~ | **Sandbox each seat at the operating system, not at the flag.** Omnigent requires `bwrap` on Linux and `seatbelt` on macOS. Mooting narrows four of five adapters with vendor flags, and `tool_profile` returns nothing by default, so an adapter that omits it ships a seat with no narrowing at all. Only the Codex adapter contains a seat by directory, and it does so because its flags proved unreliable. Every seat is already a subprocess, so this is the same shape as what exists — and it is the other half of J1, since a sandboxed seat cannot reach the shell that reaches `mooting approve`. |
| G2 ✅ | **Done, narrower than written.** The gap was real but not where the row put it: the full-screen view already showed the stances and the chat did not, so somebody signing off from a phone could not see who objected. Objections and supports now travel on the proposal message itself, objections first, each cut at a word boundary. The work half needed nothing new -- G3's measurement is what a reviewer reads at accept time. Superseded text below. |
| ~~G2~~ | **Hand the chair a written packet with each proposal.** Concord generates a scope, tests, risks and provenance summary. One person deciding does not scale past a couple of seats if they read raw logs to do it. |
| G3 ✅ | **Done.** A reported task now carries `measured: N commit(s) on <branch>, M file(s) changed or new, <tip>` under whatever the worker said. The seat that says "pushed to a branch" and the seat that pushed nothing write the same sentence, and a chair reviewing should not have to count commits by hand. Running acceptance tests is the line not crossed -- that is new machinery, not a measurement already to hand. Superseded text below. |
| ~~G3~~ | **Record what is measured apart from what is asserted.** Loki Mode separates deterministic facts — a diff and its hash, test exit codes — from an agent's own verdict. This keeps confident self-reports from being read as evidence. |
| G4 ⏸ | **Designed, not run: it spends the owner's quota and that is their call.** The experiment: one small repository, five seeded defects, each put to (a) a three-seat council at `low` effort and (b) one agent alone, same prompt and same budget. Record which defects each found, and publish the disagreements verbatim next to the seeds. Cost, counted before anything wakes: 5 defects x (3 council turns + 1 solo turn) = **20 real turns**, plus reruns. State the conditions the way `WHY.md` states 31.8 s -- one measurement, on one repository, on one machine. Until it is run the honest claim stays *legible disagreement*, not better outcomes. Superseded text below. |
| ~~G4~~ | **Publish one measurement.** No number says four rival seats catch more than one good agent and an attentive person. The project is its own instrument: run councils and single agents over seeded defects, publish the disagreements, and state the conditions the way `docs/WHY.md` states "31.8 s a turn". Until that exists the claim is legible disagreement, not better outcomes. |
| G5 ✅ | **Done.** `/topic position <what you think now>` is optional and asked for rather than demanded, because most topics are opened by somebody with no prior position and a forced prompt collects noise. Where one exists, signing off offers two buttons -- it changed my mind, I already thought so -- and `/usage` reports the count over topics that were actually answered. A tap and not a text comparison: reading a position against a rationale by machine would be a guess dressed as a number. Superseded text below. |
| ~~G5~~ | **Record whether the council changed the chair's mind.** The cheapest honest measurement available, and it needs no experiment: capture the chair's opening position when a topic is opened, compare it against the rationale they sign off with, and count the times they differ. A tool that can show how often it moved the person holding the decision has evidence rather than a claim. |

## K — What a phone makes obvious ✅ done

**Every item here came from somebody using it on a mobile, not from reading the
code.** That is the same source as the five Telegram bugs in `CLAUDE.md`, and it
is why they are written down rather than left as "polish": the terminal
tolerates all of them and a chat does not.

The standing instruction behind K2 and K3 is the chair's own: *typing on a phone
is slow, so offer buttons wherever there is a fixed set of answers.*

| | |
|---|---|
| K1 ✅ | **Done as `/pair revoke <who>`.** The host's alone, and room-scoped because pairing is. An identity bound by a redeemed claim code survives it: that person proved they reached the machine, and taking that back is not a chat gesture. Superseded text below. |
| ~~K1~~ | **An approved pairing cannot be revoked.** `pair_deny` refuses a request that is still pending; there is nothing that removes somebody already let in. A guest who was welcome in a room last month has a seat on that board for good, and the only way out is editing SQLite. `/pair revoke <who>` belongs next to `/pair approve`, and it is the host's alone. |
| K2 ✅ | **Done.** Bare `/team` offers one button per agent seat, ticked when it is on. Toggles rather than a set -- a team is edited one seat at a time far more often than written from nothing -- and the command is computed from what the room holds, so tapping twice puts a name back. Superseded text below. |
| ~~K2~~ | **`/team` still has to be typed.** `wants_choices` offers buttons for effort, rounds, nudge and chair; setting the team means typing every name. It is the one command where the answers are a known list — the seats registered on this board — so it is the one that most wants toggles. |
| K3 ✅ | **Done.** Approve or Reject now offers three reasons as buttons and a Write-my-own that falls back to typing. Each preset is a sentence, because the reason is part of the record and "ok" in a decision column tells a later reader nothing. Superseded text below. |
| ~~K3~~ | **A sign-off asks for its reason as free text.** Approving is the single gesture this whole project exists for, and on a phone it is the slowest thing in the chat: tap Approve, then type. Offer the three or four reasons that actually recur as buttons, with typing still available. The reason is part of the record, so a preset must be a real sentence rather than a label. |
| K4 ✅ | **Done.** Somebody paired into the room but not seated on the meeting in front of you is listed, marked as here but not seated, with the command that seats them. A terminal session reports nobody, because there is no chat to be paired into. Superseded text below. |
| ~~K4~~ | **`/seats` lists the seats on this topic, and a person reads it as "who is in this room".** Somebody paired into a room but not yet seated on the meeting in front of you is invisible, which was reported as "I added my wife and cannot see her". Show them, marked as here but not seated. |
| K5 ✅ | **Done.** One line in `/help`. Superseded text below. |
| ~~K5~~ | **`/proposals <id>` is not in the help.** The event pump starts at `store.head()` and never replays, so re-reading a proposal that has scrolled away is only possible through a form nothing advertises — the trap `CLAUDE.md` names, left half-closed. |

## Not building

| | |
|---|---|
| **A2A** | Real — Linux Foundation governance, spec 1.0.1, a stable Python SDK — and aimed at a problem this does not have. It discovers and delegates to opaque agents across a network. Mooting spawns every seat itself and knows each one's identity and capability at registration. A second protocol surface for no new capability. |
| **B4** | Unchanged from [WHY.md](WHY.md). A `Store` over HTTP is some fifty methods to gain what SSH, `--web` and the chat bot already give. |
| **A continuous autonomy setting** | Several projects offer a dial between read-only and fully trusted. Execution here needs two independent keys and that is deliberate: a dial replaces a property that holds by construction with one that holds by configuration. |
| **Anything aimed at a compliance buyer** | The governance frameworks are a real signal, and their buyers cannot procure a v0.1.1 package from one maintainer with no users and no review. Borrow the vocabulary in one sentence; build nothing for the procurement process. |
| **Antigravity as a first-class seat** | Its `-p` mode is reported to write nothing to stdout when it is not attached to a terminal, while still exiting 0 and still billing the turn. The adapter reads that stdout. Keep it supported and keep it second, until that is fixed upstream. |

**Watch rather than build: Concord MCP.** It reached 293 stars in six weeks with
a real team, and four of its open issues are requests for more vendor CLIs. It
has presence and messaging between agents and no turn-taking or decision layer.
If it grows one, this comparison is worth running again.

## H — Documentation that is no longer true ✅ done

All six, plus two the sweep turned up: `schema.sql` did not list `assigned` as
a manager's verdict, and `WHY.md` still said "the ruling" and "a human arbiter"
in prose. Each is folded into the file it corrects.

- ~~[WHY.md](WHY.md) lists three invariants and omits the human-only decision.~~ It is four now, and that one is first.
- ~~The LoopTroop entry states a human approves before execution.~~ Its README calls that step optional in future releases, which is the difference between a setting and an absent tool -- so the row says that rather than dropping the claim.
- ~~Two docstrings in `server.py` say sign-off has no route.~~ One of them was nine lines above the response that names the endpoint.
- ~~Milestone rows B2 and B3 read as current state.~~
- ~~B5 says two people "rule as themselves".~~ Two voices, one decision: the chair signs off and `/topic chair` is how that moves.
- ~~`schema.sql` names the seat role `arbiter` and omits `in_progress`.~~

## Order

**R, then J, then H, then E, then K, then F, then G.** J is what makes the claim true, H is what
makes the documents match the code and costs almost nothing, E closes the
surfaces the chair actually uses, F covers what holds the promises, and G is
worth doing once there are people to do it for. K is small and comes from real
use, so any of it can be pulled forward when the chair is the one blocked. G4
can run in parallel with any of them and should start early, because the answer
changes what the rest is for.
