-- Mooting: cross-vendor agent council.
--
-- Design note: this file IS the protocol. Everything else (MCP server, supervisor,
-- web UI) is a view onto these tables. If an agent CLI dies, or a driver flakes, or
-- the supervisor is not running at all, the board is still here and a human or an
-- agent can still read it and move the topic forward. That is the invariant:
--   the board is the substrate, the supervisor is only an accelerator.

PRAGMA journal_mode = WAL;      -- N MCP server processes + supervisor + UI, one file
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- seats & topics

CREATE TABLE IF NOT EXISTS agents (
    name        TEXT PRIMARY KEY,           -- stable handle used in every message
    kind        TEXT NOT NULL,              -- claude|codex|copilot|gemini|human|external
    display     TEXT,
    -- How the supervisor wakes this seat. NULL kind=human/external means "never wake,
    -- they read the board themselves". See docs/DRIVERS.md for why this is per-CLI.
    driver      TEXT,                       -- stdio_json|acp|spawn|none
    driver_cfg  TEXT NOT NULL DEFAULT '{}', -- JSON: cwd, model, extra argv
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS topics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT UNIQUE NOT NULL,
    title       TEXT NOT NULL,
    brief       TEXT NOT NULL DEFAULT '',   -- the question put to the council
    status      TEXT NOT NULL DEFAULT 'open',   -- open|paused|resolved|aborted
    -- What kind of conversation this is. Adversarial framing is right when a
    -- decision turns on finding the flaw, and wrong when the room is trying to
    -- build something -- a seat told "disagreement is the product" will
    -- manufacture disagreement to justify its turn.
    mode        TEXT NOT NULL DEFAULT 'debate', -- debate|discuss
    -- Reasoning effort for this topic's seats, overriding the council default.
    -- The dominant term in wall-clock: 279s vs 31.8s on the same prompt.
    effort      TEXT,                           -- low|medium|high, NULL = default
    -- Cost governor. Live debate auto-triggers billed turns on subscription CLIs;
    -- hitting a cap PAUSES for a human, it never silently continues.
    max_rounds  INTEGER NOT NULL DEFAULT 3,
    round       INTEGER NOT NULL DEFAULT 0,
    opened_by   TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at   TEXT
);

CREATE TABLE IF NOT EXISTS seats (
    topic_id    INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    agent       TEXT    NOT NULL REFERENCES agents(name),
    role        TEXT    NOT NULL DEFAULT 'participant',  -- participant|arbiter
    turns_used  INTEGER NOT NULL DEFAULT 0,
    max_turns   INTEGER NOT NULL DEFAULT 6,   -- per-seat cap, independent of rounds
    -- Position in the CLI's own session store, so a resume is deterministic.
    -- Never use --last / --continue: it races when two topics drive one CLI.
    cli_session TEXT,
    state       TEXT NOT NULL DEFAULT 'idle', -- idle|waking|thinking|capped|failed
    last_seen   INTEGER NOT NULL DEFAULT 0,   -- events.id cursor: catch-up on next turn
    PRIMARY KEY (topic_id, agent)
);

-- ---------------------------------------------------------------- the record

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id    INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    author      TEXT    NOT NULL,           -- agent name, or a human's handle
    kind        TEXT    NOT NULL DEFAULT 'say',  -- say|propose|object|support|ruling|system
    body        TEXT    NOT NULL,
    reply_to    INTEGER REFERENCES messages(id),
    proposal_id INTEGER,                    -- set for propose/object/support/ruling
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_topic ON messages(topic_id, id);

-- A proposal is the only thing that can change the world, and only a human closes it.
-- This is the "human decides" requirement expressed as a schema constraint rather
-- than as a prompt instruction, because prompt instructions are advice, not fences.
CREATE TABLE IF NOT EXISTS proposals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id    INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    message_id  INTEGER NOT NULL REFERENCES messages(id),
    author      TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    body        TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'open',  -- open|approved|rejected|withdrawn
    decided_by  TEXT,                       -- MUST be a human seat; enforced in store.py
    rationale   TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    decided_at  TEXT
);

-- Read on every loop of the supervisor, to answer "is a decision pending".
CREATE INDEX IF NOT EXISTS idx_proposals_topic ON proposals(topic_id, status);

CREATE TABLE IF NOT EXISTS votes (
    proposal_id INTEGER NOT NULL REFERENCES proposals(id) ON DELETE CASCADE,
    agent       TEXT    NOT NULL,
    stance      TEXT    NOT NULL,           -- support|object|abstain
    rationale   TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (proposal_id, agent)
);

-- ---------------------------------------------------------------- the clock

-- Single monotonic cursor. The supervisor polls it; every seat stores its own
-- last_seen against it. This is what makes "catch up on next turn" work when a
-- wake fails -- the agent asks for everything after its cursor and misses nothing.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id    INTEGER REFERENCES topics(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,              -- message|proposal|decision|seat|topic
    actor       TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}', -- JSON
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_events_topic ON events(topic_id, id);

-- Wake ledger: what the supervisor actually spent, per seat per hour. Separate from
-- turns_used because a wake can fail without producing a turn, and a failed wake on
-- a metered CLI still costs a request.
CREATE TABLE IF NOT EXISTS wakes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id    INTEGER NOT NULL,
    agent       TEXT NOT NULL,
    outcome     TEXT NOT NULL,              -- ok|timeout|error|refused
    detail      TEXT NOT NULL DEFAULT '',
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_wakes_agent ON wakes(agent, started_at);

-- ------------------------------------------------------------------ mentions

-- An @mention is a *directed wake*: it jumps the round-robin so the person asked
-- answers next, rather than whenever their turn comes round. It is stored rather
-- than re-parsed on read because the supervisor needs to know which asks are
-- still outstanding, and because "who asked whom" is part of the record.
CREATE TABLE IF NOT EXISTS mentions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id    INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    asker       TEXT NOT NULL,
    target      TEXT NOT NULL,
    question    TEXT NOT NULL DEFAULT '',
    -- 1 when somebody explicitly asked (`mooting_ask`), 0 for a bare `@name`
    -- inside an argument. Only an ask stops the council waiting on a human:
    -- being named is priority, being asked is a block.
    asking      INTEGER NOT NULL DEFAULT 1,
    answered_by INTEGER REFERENCES messages(id),   -- the reply that discharged it
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_mentions_open ON mentions(topic_id, target, answered_by);

-- --------------------------------------------------------------------- tasks

-- Team mode. A manager seat drafts tasks; the draft set is put to a human as an
-- ordinary proposal, and nothing executes until that proposal is approved. This
-- reuses the one fence already hardened (only humans close a proposal) instead of
-- inventing a second approval path that would have to be trusted separately.
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id    INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    acceptance  TEXT NOT NULL DEFAULT '',   -- how the manager will know it is done
    assignee    TEXT NOT NULL REFERENCES agents(name),
    created_by  TEXT NOT NULL,              -- the manager seat
    -- draft    : written, not yet put to a human
    -- assigned : plan approved; the worker may be woken for it
    -- done     : worker reports finished; awaiting the manager's review
    -- blocked  : worker cannot proceed and said why
    -- accepted / rejected : the manager's verdict
    status      TEXT NOT NULL DEFAULT 'draft',
    proposal_id INTEGER REFERENCES proposals(id),   -- the plan gate
    branch      TEXT,                       -- work lands here, never on main
    worktree    TEXT,                       -- isolated checkout for this task
    base_sha    TEXT,                       -- commit the branch was cut from
    result      TEXT NOT NULL DEFAULT '',   -- what the worker reported back
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_topic ON tasks(topic_id, status);

-- Source material fed to a council: a spec, a log, an image, a CSV.
--
-- The file is copied next to the board rather than referenced where it lay,
-- because a council is a record: minutes that cite a document nobody can open
-- six months later are minutes of nothing. Copying also means the board plus
-- its directory is the whole artefact.
CREATE TABLE IF NOT EXISTS attachments (
    id          INTEGER PRIMARY KEY,
    topic_id    INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,              -- as the human named it
    path        TEXT NOT NULL,              -- absolute, under the board's dir
    bytes       INTEGER NOT NULL,
    is_text     INTEGER NOT NULL DEFAULT 0, -- can it be inlined into a prompt
    note        TEXT NOT NULL DEFAULT '',   -- why it was attached
    added_by    TEXT NOT NULL REFERENCES agents(name),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_attachments_topic ON attachments(topic_id);

-- Who, in a chat, is allowed to act as which seat.
--
-- Taken from openclaw's pairing: an unknown sender is inert until somebody who
-- already has authority approves them. It is the same fence as `Store.decide`,
-- moved to the edge -- locally a caller's identity comes from the operating
-- system, and a chat has no operating system to ask.
CREATE TABLE IF NOT EXISTS pairings (
    id          INTEGER PRIMARY KEY,
    channel     TEXT NOT NULL DEFAULT 'telegram',
    chat_id     TEXT NOT NULL,              -- the room; allowlisted separately
    user_id     TEXT NOT NULL,              -- the person
    display     TEXT NOT NULL DEFAULT '',   -- what they call themselves there
    seat        TEXT REFERENCES agents(name),
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | denied
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(channel, chat_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_pairings_status ON pairings(status);

-- Small facts about this board that are not about a topic: a bot token, a
-- default, a channel setting. Kept here rather than in a config file so a board
-- is one artefact -- copy the file and everything about that council comes with
-- it, including how it reaches its chat.
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
