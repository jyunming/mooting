# Positioning

A working document, not a published page. `docs/WHY.md` is the reasoning behind
the design; this is the reasoning behind what the project says about itself, and
what it should stop saying.

Everything here was checked against live sources on 2026-08-31. Where a claim
depends on a competitor's current behaviour it carries the date, because that
kind of claim goes stale.

## The claim

**Human sign-off here is not a mode you enable. It is the absence of the tool
that would make it a mode.** There is no `mooting_decide` — not disabled,
absent, so it never appears in any seat's tool list. `Store.decide` refuses a
non-human caller as a second line, and it is the only path out of `draft`.

The second sentence, and only the second: **every seat runs on a CLI
subscription you already pay for, and Mooting holds no key of its own.**

Two claims were considered and set aside.

*Independence* — "no vendor between you and your agents" — is true and no longer
distinguishing. Concord MCP and Senate share the shape, and MCP's 2026-07-28
spec deprecated sampling on a 12-month clock because servers calling back to
client models created trust complications. Server-side restraint is becoming the
ecosystem default rather than a position. It earns a supporting sentence.

*Better outcomes* — "four vendors were trained by different companies and miss
different things" — is the most attractive sentence available and has nothing
behind it. No measurement shows that four rival seats catch more than one good
agent and an attentive person. Hold it until there is one. The measurement is
cheap and the project is the instrument: run councils and single agents over
seeded defects, publish the disagreements, state the conditions the way
`docs/WHY.md` states "31.8 s a turn".

## Who this is for

**Somebody who already pays for two or more agent CLIs and has a question worth
more than one opinion.** That is the anchor, because it is findable and it needs
no explaining: they already have Claude, Codex and Copilot open in different
windows.

**The question does not have to be about code, and saying it is costs readers.**
The councils actually held on this board have been which game engine to build a
scientific game in, whether split air-conditioning is more efficient, a career
question, and webhook retry policy. One of those four is code. The seats are
coding agents because that is what a person with these subscriptions has to
hand, not because the subject has to be a repository — and the working directory
matters far less than the docs imply for most meetings.

They have usually had the incident — an agent deleted the failing test instead
of fixing it, or an overnight run spent a month of quota, or something merged
that they only understood a week later. They did not turn the agents off. They
started watching more closely, which does not scale.

The incumbent is not another tool. It is four terminals, the same question
pasted into each, answers carried between windows by hand, and the judgment
happening in one person's head with no record of it. That person already
convenes this council. Mooting gives it a table, a turn order, a transcript, and
a place where their own call is written down.

**Lead every demonstration with a meeting topic, not a work topic.** A design
review, a migration question, a refactor with three defensible approaches.
Disagreement between vendors pays most there and the execution risk is zero.
Gated execution is the second act, and it is adopted after the transcript has
earned some trust.

Four audiences are worth turning down.

- **The orchestration enthusiast.** They want throughput and will read "only a
  person decides" as friction. Courting them means competing on autonomy, which
  is the axis this project gave up on purpose.
- **Enterprise compliance.** The governance frameworks are a real signal and
  their buyers cannot procure a v0.1.1 package from a single maintainer with no
  users and no review. Borrow the vocabulary; do not chase the buyer yet.
- **Teams.** There is no multi-user story. One keyboard is the chair.
- **The safety audience.** This is a working tool with a governance property. It
  is not a safety artifact, and claiming otherwise invites a standard of
  adversarial scrutiny the design does not meet.

One group is worth cultivating as readers rather than users: the people writing
about agent governance. Singapore's IMDA framework asks for human approval
checkpoints; the Cloud Security Alliance's Agentic Trust Framework defines
autonomy levels that gate execution on explicit approval. Both are prose. The
useful sentence is that what those documents require in prose, this enforces in
a schema — said once, in `docs/WHY.md`, never as conformance and never in a
headline.

## Showing an absence

An absence is harder to screenshot than a feature and easier to verify. A
feature demonstration shows one path working; an absence is a property of a
whole surface, and surfaces can be audited. Offer three depths and let the
reader pick.

**Ten seconds — the unanimous board.** One image. All four seats have reviewed a
proposal and every one of them is in favour, and the status line beneath still
reads `draft — awaiting your sign-off`. Then one command from the chair, and it
moves. The caption writes itself: all four said yes, and it waited for you
anyway. Agreement among agents is loud, and a status field that does not change
is the whole idea in one line. This belongs at the top of the README.

**Sixty seconds — the refusal, reproduced.** A command the reader runs
themselves: seat an agent, ask it to approve its own proposal, watch the board
record the attempt as an ordinary post while the proposal stays in draft. The
seat asserts authority and the board does not recognise it. `mooting doctor`
already spends one real turn per seat because exit codes lie; this is the same
idea pointed at the one property that matters most.

**Five minutes — the audit, and the test that keeps it true.** A short README
section naming two things to look at: the tool list the MCP server registers,
and `Store.decide` refusing a non-human caller. Then pin it — a test that
enumerates every tool the server exposes and fails if a decide-shaped one ever
appears, linked from the README as the test that keeps it true. That turns a
promise into a check that would go red the day anyone adds the tool, including
the maintainer.

The register throughout is delegation, not suspicion. The call is yours, and
here is how you can see that it stays yours.

## What to compare against

**Four terminals, first.** It starts from what the reader already does rather
than from what they should worry about.

**Concord MCP, second, on purpose.** It is the nearest twin — MCP server, local
SQLite, several vendor CLIs, no keys held — and as of 2026-08-31 its
task-closing check is ownership only, so any owning agent can close its own
work. State it with the date, link the line, and stop there. The comparison is
worth leading with precisely because everything else matches: it teaches the
reader the one question to ask of anything in this category, which is who can
close the loop. Having taught the question, the answer holds everywhere.

**GitHub Agent HQ, third.** The only other genuinely cross-vendor table, and the
contrast is one question: who sits between you and your agents? There, GitHub
does, metered through a paid Copilot seat. Here, nobody — each seat is your own
subscription, spawned as a subprocess on your machine.

Avoid three comparisons. Multica, Paseo and Omnigent are general orchestrators
with tens of thousands of stars, and any table containing them produces one
thought that no row can undo. CrewAI, LangGraph and AutoGen are frameworks, and
comparing to them invites "I could script this in an afternoon", which is a
fight about flexibility on their ground. Claude Code's agent teams should never
appear as a feature table, because Mooting loses that table on integration and
wake latency; use vendor-native multi-agent only as a category observation,
which is that every vendor's version stays inside that vendor.

## What to stop saying

**Stop leading with "no API keys."** It was the founding insight and it is now
the neighbourhood standard. One supporting sentence, and its best use is cost.

**Stop letting "argue" carry the value.** It is vivid and it makes the project
sound like a debating society. Keep the word; move the weight onto what the
reader gets, which is dissent in a form they can act on and a record of who
objected and why.

**Stop presenting four invariants as the pitch.** One of them is the position.
The others are engineering, and they read better as concrete benefits: it will
not spend your quota while you sleep, and a failed wake counts as spend because
metered CLIs charge for it; a seat in a meeting cannot touch files. The
board-as-substrate principle belongs in the docs and not in the pitch at all.

**Stop saying "multi-agent orchestration."** The phrase files this next to the
tools it should not be compared with.

**Stop putting the chair in a trailing clause.** "With a human chairing" at the
end of the opening sentence delivers the differentiator as an afterthought.

**Stop listing five surfaces.** At v0.1.1 from one maintainer, breadth reads as
unfocus. Name the console, mention that the board also reaches a phone, and put
the rest in the docs.

## What this does not claim

State these first and unprompted. Said first they are scope; found by a reader
they are a retraction.

**The sign-off holds at Mooting's own surface, not at the machine.** No seat can
decide through any channel Mooting offers. A deliberating seat has neither the
tool nor a shell — the driver profiles narrow it to the Mooting MCP server, deny
shell and write, or run the CLI in plan mode. An **execute-capable seat woken for
an approved task is different**: its profile drops those restrictions so it can
do the work, which gives it a shell, and the shell reaches `mooting approve`.
`Store.decide` identifies a person by name, and the CLI resolves that name
automatically when a board has one human seat.

**That window is now closed, and closing it is worth saying.** While a seat is
executing, a sign-off typed on this machine is refused, because it cannot be
told from one that seat typed. The message names the way out: a chat account is
something the seat does not hold, and a claim code binds one to a seat by
proving somebody reached the terminal. So the claim is no longer only about the
protocol — it is about the machine for as long as the machine is untrustworthy,
which is exactly while a seat holds a shell on it. What remains outside it is an
agent writing to the SQLite file directly, which is an OS sandbox's job.

The superseded version of this limit, kept because the reasoning is what
matters: the window was narrow — only a seat a person already signed off for a
specific task on a work topic could reach it — and it was real. Until it was
closed, the claim was about
the protocol, and the doc says so in the same breath.

**A council reads whatever its seats' working directories mean to them.** Found
here, not in anybody's documentation: a board asked "how can I make money"
answered with the chair's age, city and profession, none of which was on the
board. A coding CLI loads its own per-directory memory before it sees the
council's prompt, so pointing a seat at a working project brings that project's
notes into an unrelated discussion. Every cross-vendor council has this problem
and nothing else names it. `mooting doctor` now reports it, and `cwd` and `repo`
are separate settings because they want opposite things.

**The record is legible, not tamper-evident.** The board is an ordinary SQLite
file. It records who posted and who signed off; nothing yet makes that record
resistant to a later edit. "You can prove it" is not available until the event
log is chained.

**The absence is a commitment, not a law.** A fork adds the tool tomorrow. What
the project offers is a stance made auditable and pinned in CI, which is a
contract rather than an impossibility.

**A checkpoint guarantees a decision, not attention.** A chair who signs without
reading has been handed a checkpoint and spent it. This makes sign-off
unavoidable. It cannot make it thoughtful, and the word "safe" should not appear.

**Deliberation quality is unmeasured.** Legible disagreement is the claim.
Better outcomes is not, until there is a number.

**It costs more, on purpose.** Four seats spend four subscriptions on one
question, and the chair throttles throughput by design. Anyone whose problem is
having too much to review is not the reader this is for.

**The seats are borrowed.** Every adapter shims a vendor CLI that can change
without notice, and the trap list in `CLAUDE.md` is the evidence. Reliability of
any one seat is outside this project's control.

**One maintainer, v0.1.1, no users yet.** No maturity claim survives the repo
page, so make none. The counterweight is that the central claim needs no
maturity. It is checkable today, at any size, by anyone with ten seconds and a
screenshot or five minutes and a test.

## The honest read

The position is strong because of timing as much as design. The protocol walked
back the mechanism this project never used. The governance frameworks named the
checkpoint it already enforces. Every vendor shipped multi-agent and every one
of them shipped it single-vendor and self-approving.

Cross-vendor breadth is a real advantage and probably a temporary one, because
any vendor can add another model faster than this project can grow. The durable
thing underneath it is different and worth naming: **a record of who argued and
who signed, kept by someone other than the party being audited.** A vendor
cannot ship that. It is the one property that survives Claude Code shipping
excellent agent teams next quarter, and it is currently buried under the
orchestration story.

One honest caveat about what this is. A product is defined by users whose
problem it solves repeatedly, and this has a thesis and no users. The next
quarter is better spent on twenty real readers and one published measurement
than on features. The idea — that the decision should be absent by construction,
with a running existence proof — may travel further than the code does, and that
is a good outcome to plan for rather than an accident to be embarrassed by.
