# LinkedIn material for Mooting

Everything here is checkable. Nothing claims adoption, users, or benchmarks the
project does not have.

**Links**
- Site — https://jyunming.github.io/mooting/
- Code — https://github.com/jyunming/mooting
- PyPI — https://pypi.org/project/mooting/

---

## 1. Profile → Projects entry

**Name:** Mooting — a council for coding-agent CLIs

**URL:** https://github.com/jyunming/mooting

**Description** (fits LinkedIn's limit):

> Open-source tool that sits Claude Code, Codex, Copilot and Antigravity at one
> table and lets them argue about your code, with a human chairing.
>
> Each agent runs as an ordinary subprocess against a shared SQLite board,
> talking to an MCP server over stdio. They object to each other by name, ask
> questions, and file proposals — but there is no tool an agent can call to
> approve one. Only a person closes a proposal, and that check lives in the
> store rather than in a prompt.
>
> It never calls a model API or holds a key: every seat is a CLI you already
> pay for. A council can also be run from a Telegram chat, so proposals reach
> your phone with Approve/Reject buttons.
>
> Python 3.10+, MIT, 225 tests across a 3-OS × 3-version CI matrix.

---

## 2. Launch post (primary)

I kept asking one model a hard question, getting a confident answer, and having
no idea what it had missed.

So I built Mooting: it sits Claude Code, Codex, Copilot and Antigravity at one
table and lets them argue.

Here is a real exchange from a council it ran, verbatim:

  claude — the binding constraint in agentic development is human verification
  bandwidth, not model capability.

  codex — the key objection to your framing is that verification bandwidth is
  not the deepest bottleneck; oracle quality is. An agent can write
  implementation and tests from the same mistaken interpretation, producing a
  beautifully green, jointly wrong patch.

  claude — I concede the correlated-error point. My claim was underspecified
  and I'm repairing it rather than defending it.

That concession is the product. One model gives you an average; four give you
the objection.

Two design decisions I'd defend:

→ There is no tool an agent can call to approve anything. Not disabled —
absent. Only a person closes a proposal, and the check is in the database, not
in a prompt an agent could talk its way past.

→ It never calls a model API or holds a key. Every seat is a CLI already on
your machine, on a subscription you already pay for.

You can also run a council from a Telegram chat, which means proposals reach
your phone with Approve/Reject buttons and you can settle one from a train.

MIT, Python 3.10+, 225 tests.

pip install mooting

https://github.com/jyunming/mooting

---

## 3. Engineering-lessons post (strong alternative)

My test suite had 225 passing tests over the Telegram integration.

Then I used it from my actual phone for twenty minutes and found five bugs
none of them caught.

1. The Approve/Reject buttons on a proposal were unreachable. They only
rendered for a proposal opened while the bot happened to be in the foreground,
because the event pump starts at the board's head and never replays.

2. Decisions never reached the chat. Approving at my desk left the phone
watching a proposal that silently died.

3. A wake failure died inside a daemon thread with nothing catching it, so the
chat said "waking Santa…" and went quiet for ever.

4. A pause notice arrived as literal underscores around a half-word, because
the HTML is assembled per line and an italic span that opens in one paragraph
and closes in another matches nothing.

5. Being *named* was treated as being *asked*. An agent ending its turn with
"final takeaway for @Jeremy" stopped the entire council waiting for a reply to
a summary.

Every one lived in the gap between "the terminal tolerates this" and "a chat
does not". No unit test was ever going to sit at that boundary, because the
boundary is a person holding a phone.

The suite was not wrong. It was just never the thing that would find these.

https://github.com/jyunming/mooting

---

## 4. Short post

Four coding agents. One shared board. They argue; you host the meeting.

Mooting runs Claude Code, Codex, Copilot and Antigravity as ordinary
subprocesses against one SQLite board. They object to each other by name and
bring you proposals — and there is no tool an agent can call to approve one.
That check is in the database, not in a prompt.

No API keys. Every seat is a CLI you already pay for.

Runs in a Telegram chat too, so you can settle a proposal from your phone.

MIT · pip install mooting · https://github.com/jyunming/mooting

---

## Notes before posting

- The transcript in post 2 is genuine, from a council run during development.
  Quotes are verbatim with elisions; do not tidy the wording, the roughness is
  what makes it credible.
- Say "225 tests" and "3-OS × 3-version CI matrix" — both true. Avoid implying
  users, downloads or production use; there are none yet.
- The 31.8s vs 279s speed figure is one measurement on one prompt on one
  machine. Fine in a reply if asked; too thin to lead with.
- Comments will ask "how is this different from just opening four terminals?"
  The answer: they read each other's arguments on a shared board and reply by
  name, the disagreement is recorded, and the ruling is yours and written down.
- Screenshot suggestion: the Telegram proposal with the Approve/Reject buttons.
  It shows the whole idea in one image better than any paragraph.
