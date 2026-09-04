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
| J3 | **Pin the absence in CI.** A test that enumerates every tool the MCP server registers and fails if a decide-shaped one ever appears. Link it from the README as the test that keeps it true. It converts the central claim from something a reader trusts to something they reproduce, and it goes red for the maintainer too. |
| J4 | **Add the refusal to `doctor`.** It already spends one real turn per seat, because exit codes lie. Ask each seat to approve its own proposal and report that none of them could. That is the sixty-second demonstration, and it exercises the property that matters most against the real CLIs. |
| J5 | **Produce the one-screen proof.** A board where all four seats are in favour and the status still reads `draft — awaiting your sign-off`, then one command from the chair. `tools/screenshot.py` already drives the real TUI through Textual's pilot, so this is a scripted board rather than new machinery. |

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
| R1 | **A room owns its team.** Seats are per topic today, so every new meeting is seated by hand and two groups sharing a board share their seats. Give a room a roster — its own seats, their models and working directories — and a topic opened there starts with it. |
| R2 | **A topic belongs to the room that opened it.** This is what makes two teams real, and it closes a leak: the event pump sends every event from every topic to every paired chat (`events_since(cursor, None)` and every listener), so a second group reads the first group's council live. Binding topics to rooms fixes the visibility and the `/topics` list at once. |
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
| E1 | **A person managing a work topic cannot finish it.** Accepting or rejecting a finished task exists only as an MCP tool, so only an agent seat can do it. Work is reported complete only when every task is accepted, so a topic managed by a person never completes. Add `/accept` and `/reject`. |
| E2 | **The browser watches but cannot act.** The served page makes three read requests and carries no controls, while every write route already exists. |
| E3 | **The documented HTTP surface is not the one that runs.** `POST /topics` and `GET /topics/{slug}/minutes` do not exist, `/api/events` is the polling form rather than the stream, and every documented path omits the `/api` prefix. Build the two routes or correct the table. |
| E4 | **`/attach` is advertised in six places and reachable from none of them.** It is missing from the console's command table, so the console, the full-screen view and the chat bot all answer "unknown". The shell `mooting attach` works, and sending a document to the chat works. One entry in the dispatch table covers all three surfaces. |

## F — Tests where the invariants live

**Cover the surfaces that hold the promises before adding to them.** `web.py`
has no test at all, including the loopback check that is the only thing between
an unauthenticated live session and the network. `mcp_server.py` — everything an
agent can see — has no functional test, only a count of its decorators.

## G — Worth taking from elsewhere

| | |
|---|---|
| G1 | **Sandbox each seat at the operating system, not at the flag.** Omnigent requires `bwrap` on Linux and `seatbelt` on macOS. Mooting narrows four of five adapters with vendor flags, and `tool_profile` returns nothing by default, so an adapter that omits it ships a seat with no narrowing at all. Only the Codex adapter contains a seat by directory, and it does so because its flags proved unreliable. Every seat is already a subprocess, so this is the same shape as what exists — and it is the other half of J1, since a sandboxed seat cannot reach the shell that reaches `mooting approve`. |
| G2 | **Hand the chair a written packet with each proposal.** Concord generates a scope, tests, risks and provenance summary. One person deciding does not scale past a couple of seats if they read raw logs to do it. |
| G3 | **Record what is measured apart from what is asserted.** Loki Mode separates deterministic facts — a diff and its hash, test exit codes — from an agent's own verdict. This keeps confident self-reports from being read as evidence. |
| G4 | **Publish one measurement.** No number says four rival seats catch more than one good agent and an attentive person. The project is its own instrument: run councils and single agents over seeded defects, publish the disagreements, and state the conditions the way `docs/WHY.md` states "31.8 s a turn". Until that exists the claim is legible disagreement, not better outcomes. |
| G5 | **Record whether the council changed the chair's mind.** The cheapest honest measurement available, and it needs no experiment: capture the chair's opening position when a topic is opened, compare it against the rationale they sign off with, and count the times they differ. A tool that can show how often it moved the person holding the decision has evidence rather than a claim. |

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

## H — Documentation that is no longer true

Fold each into whichever commit touches the file.

- [WHY.md](WHY.md) lists three invariants and omits the human-only decision, which the same document names as the project's one distinguishing property. It is four.
- The LoopTroop entry states a human approves before execution. Its README says that step is to become optional. The correction strengthens the comparison.
- Two docstrings in `server.py` say sign-off has no route. It was added in B3, and one of them describes the endpoint nine lines further down.
- Milestone rows B2 and B3 read as current state rather than as what that milestone did.
- B5 says two people "each speak under their own name, hold their own token, and rule as themselves". Speaking is still each person's; signing off is now the chair's, and that row needs to say so.
- `schema.sql` names the seat role `arbiter`, which no code reads or writes and which is the wrong register, and omits `in_progress` from the task states it lists.

## Order

**R, then J, then H, then E, then F, then G.** J is what makes the claim true, H is what
makes the documents match the code and costs almost nothing, E closes the
surfaces the chair actually uses, F covers what holds the promises, and G is
worth doing once there are people to do it for. G4 can run in parallel with any
of them and should start early, because the answer changes what the rest is for.
