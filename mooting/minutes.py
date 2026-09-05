"""Take the meeting out of the board.

A council is only worth convening if what it decided outlives the terminal it
happened in. This renders a topic as markdown you can commit, paste into a PR, or
hand to whoever was not in the room.

Two things it deliberately does *not* do:

**It does not summarise.** Minutes that paraphrase are minutes you have to
distrust, and there is no way to check them without the transcript you no longer
have. Every position appears in the words the seat used. What is dropped is only
scaffolding -- round markers, wake failures, pause notices -- which is noise about
the machinery rather than anything anyone said.

**It does not decide what the conclusion was.** The conclusion is whatever a human
approved, which the board already records. If nothing was approved the minutes say
so, rather than promoting the last confident-sounding paragraph.
"""

from __future__ import annotations

import subprocess
from datetime import datetime

from .store import Store

#: System notes about the machinery rather than the discussion. Rulings are also
#: `kind='system'` in places, so this filters on content, not only on kind.
_NOISE = ("--- round ", "paused:", "wake failed for", "plan approved", "task #")


def _is_noise(m) -> bool:
    if m["kind"] != "system":
        return False
    body = m["body"].strip()
    return any(body.startswith(p) for p in _NOISE)


def _fmt_when(stamp: str) -> str:
    try:
        return datetime.fromisoformat(stamp).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return stamp


def render(store: Store, topic_id: int, transcript: bool = True) -> str:
    t = store.topic(topic_id)
    seats = store.seats(topic_id)
    # Minutes are the whole record, so no window: the default stops at 500.
    msgs = store.transcript(topic_id, limit=None)
    said = [m for m in msgs if not _is_noise(m)]
    proposals = store.proposals(topic_id)
    tasks = store.tasks(topic_id)

    people = [s["agent"] for s in seats if s["kind"] not in {"human", "external"}]
    humans = [s["agent"] for s in seats if s["kind"] == "human"]
    spoke = {m["author"] for m in said}

    out: list[str] = [f"# {t['title']}", ""]

    when = f"{_fmt_when(t['created_at'])}"
    if msgs:
        when += f" – {_fmt_when(msgs[-1]['created_at'])}"
    out += [
        f"**When** {when}  ",
        f"**Council** {', '.join(people) or '—'}  ",
        f"**Chair** {', '.join(humans) or '—'}  ",
        f"**Format** {t['mode']}, {t['round'] + 1} of {t['max_rounds']} rounds"
        f"{' · ' + t['status'] if t['status'] != 'open' else ''}",
        "",
    ]
    absent = [p for p in people if p not in spoke]
    if absent:
        # Worth recording: a seat that never spoke did not agree, it was absent.
        out += [f"_Did not speak: {', '.join(absent)}._", ""]

    out += ["## The question", "", t["brief"].strip() or "_(none given)_", ""]

    # The chair's closing words go first among the outcomes: whoever reads this
    # wants "what did we settle on" before "what was argued".
    closing = store.closing_note(topic_id)
    if closing is not None:
        out += ["## Conclusion", "",
                f"_{closing['author']}, {_fmt_when(closing['created_at'])}_", "",
                closing["body"].strip(), ""]
    elif t["status"] in {"open", "paused"}:
        out += ["> **This meeting has not been concluded.** What follows is where "
                "it had got to.", ""]

    # ------------------------------------------------------------- decisions
    out += ["## Decisions", ""]
    decided = [p for p in proposals if p["status"] in {"approved", "rejected"}]
    if not decided:
        out += ["_Nothing was decided._"
                + (" Proposals are still open — see below."
                   if any(p["status"] == "open" for p in proposals) else ""), ""]
    for p in decided:
        mark = "**Approved**" if p["status"] == "approved" else "**Rejected**"
        out += [f"### {mark} — {p['title']}", ""]
        out += [f"_Proposed by {p['author']}; decided by {p['decided_by']}"
                f"{' on ' + _fmt_when(p['decided_at']) if p['decided_at'] else ''}._", ""]
        if p["rationale"]:
            out += [f"> {p['rationale'].strip()}", ""]
        out += [p["body"].strip(), ""]
        votes = store.votes(p["id"])
        if votes:
            out += ["| seat | stance | why |", "|---|---|---|"]
            for v in votes:
                why = " ".join(v["rationale"].split())[:200] or "—"
                out += [f"| {v['agent']} | {v['stance']} | {why} |"]
            out += [""]

    still_open = [p for p in proposals if p["status"] == "open"]
    if still_open:
        out += ["### Still awaiting sign-off", ""]
        out += [f"- **{p['title']}** — proposed by {p['author']}" for p in still_open]
        out += [""]

    unanswered = store.open_mentions(topic_id)
    if unanswered:
        out += ["### Questions left unanswered", ""]
        for m in unanswered:
            q = " ".join(m["question"].split())[:200]
            out += [f"- {m['asker']} → **{m['target']}**: {q}"]
        out += [""]

    # ---------------------------------------------------------------- worklog
    if tasks:
        out += worklog(store, topic_id)

    # ------------------------------------------------------------- transcript
    if transcript and said:
        out += ["## Discussion", ""]
        for m in said:
            tag = "" if m["kind"] == "say" else f" · {m['kind']}"
            out += [f"### {m['author']}{tag}  <sub>#{m['id']} · "
                    f"{_fmt_when(m['created_at'])}</sub>", ""]
            if m["reply_to"]:
                ref = store.quoted(int(m["reply_to"]))
                if ref is not None:
                    quoted = " ".join(ref["body"].split())[:160]
                    out += [f"> Replying to #{ref['id']} {ref['author']}: {quoted}…", ""]
            out += [m["body"].strip(), ""]

    out += ["---", "", f"_Minutes generated from the Mooting board "
            f"(`{t['slug']}`)._"]
    return "\n".join(out).rstrip() + "\n"


def commits_on(task) -> int | None:
    """Commits on a task's branch that its base did not have.

    The supervisor already trusted this over a worker's own account of itself
    when the worker said nothing at all. A report is a claim; this is the
    evidence, so the log shows both and lets them disagree.
    """
    tree, base = task["worktree"], task["base_sha"]
    if not tree or not base:
        return None
    try:
        r = subprocess.run(["git", "-C", tree, "rev-list", "--count", f"{base}..HEAD"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=15)
        return int(r.stdout.strip()) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def worklog(store: Store, topic_id: int) -> list[str]:
    """What was actually done, as opposed to what was decided.

    Kept separate from the decisions because they answer different questions --
    "what did we agree" and "what came of it" -- and a plan that was approved but
    never finished should be visible as exactly that.
    """
    tasks = store.tasks(topic_id)
    if not tasks:
        return []
    out = ["## Work log", "", "| # | task | assignee | state | commits | branch |",
           "|---|---|---|---|---|---|"]
    for t in tasks:
        n = commits_on(t)
        out.append(f"| {t['id']} | {t['title']} | {t['assignee']} | {t['status']} | "
                   f"{'—' if n is None else n} | {t['branch'] or '—'} |")
    out.append("")
    for t in tasks:
        if not (t["result"] or t["acceptance"]):
            continue
        out += [f"### #{t['id']} {t['title']}", ""]
        if t["acceptance"]:
            out += [f"**Done when:** {t['acceptance'].strip()}", ""]
        if t["body"].strip():
            out += [t["body"].strip(), ""]
        if t["result"]:
            out += [f"**Reported by {t['assignee']}"
                    f"{' at ' + _fmt_when(t['updated_at']) if t['updated_at'] else ''}:**",
                    "", t["result"].strip(), ""]
        if t["worktree"]:
            out += [f"_Worktree:_ `{t['worktree']}`", ""]
    return out


def default_path(store: Store, topic_id: int) -> str:
    return f"{store.topic(topic_id)['slug']}-minutes.md"
