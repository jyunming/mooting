"""`mooting serve` — the board, over HTTP, for clients that are not a terminal.

Milestone B1 of docs/REMOTE.md: read the board and say things to it. Rulings are
deliberately **not** here yet, and neither is driving the council — those come
once the read path has proved itself, because a ruling is the one action that
cannot be undone and it should not be the first thing a new transport learns.

Two design notes worth knowing before changing anything here.

**The event stream is Server-Sent Events, not a websocket.** The board already
keeps a monotonic event cursor, so resuming is just "everything after id N" --
the server takes that from `Last-Event-ID` or `?since=`, whichever the client
sends. A websocket would need the same reconnect logic written by hand on both
ends to arrive in the same place. Actions are ordinary POSTs; nothing needs a
socket. (The bundled page uses `fetch` rather than `EventSource`, because
`EventSource` cannot send an Authorization header and the token belongs in one.)

**Identity is the fence, not a feature.** `Store.decide` refuses a non-human
because locally the caller's identity comes from the operating system. Over a
socket there is no operating system to ask, so whoever holds the token *is* that
seat. That is why the token is required on every request, is never read from a
query string (they land in logs and browser history), and why the server binds
to loopback unless somebody says otherwise in as many words.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from pathlib import Path

from .store import (NotAuthorised, Store, StoreError, agenda_points,
                    agenda_text, connect, split_points)

log = logging.getLogger("mooting.server")

#: Typed application keys; see build_app.
try:
    from aiohttp.web import AppKey
    STORE: "AppKey[Store]" = AppKey("store")
    TOKEN: "AppKey[str]" = AppKey("token")
    HUMAN: "AppKey[str]" = AppKey("human")
    RUNNING: "AppKey[dict]" = AppKey("running")
    SUPERVISOR: "AppKey[object]" = AppKey("supervisor")
except ImportError:                                  # aiohttp is optional
    STORE = TOKEN = HUMAN = RUNNING = SUPERVISOR = None  # type: ignore[assignment]

#: Anything outside this is a remote-control surface, and B1 does not have the
#: fences for one. Kept as data so the check cannot drift from the message.
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def _topic_json(store: Store, topic) -> dict:
    tid = int(topic["id"])
    return {
        "id": tid,
        "slug": topic["slug"],
        "title": topic["title"],
        "mode": topic["mode"],
        "status": topic["status"],
        "round": topic["round"],
        "max_rounds": topic["max_rounds"],
        "effort": topic["effort"],
        "agenda": agenda_points(topic),
        "seats": [
            {"agent": s["agent"], "role": s["role"], "state": s["state"],
             "turns_used": s["turns_used"],
             # the cap that binds, not the raw column -- see the TUI's seat panel
             "turns_max": min(s["max_turns"], topic["max_rounds"])}
            for s in store.seats(tid)
        ],
    }


def _message_json(m) -> dict:
    return {"id": m["id"], "author": m["author"], "kind": m["kind"],
            "body": m["body"], "at": m["created_at"]}


def default_supervisor(store: Store, topic_id: int):
    """A supervisor wired to the real CLIs, with the topic's own budget.

    Separate so a test can pass one that spawns nothing -- a suite that quietly
    starts four subscription CLIs is a trap, and it has caught this project once.
    """
    from .drivers.registry import build_drivers
    from .supervisor import Caps, Supervisor

    topic = store.topic(topic_id)
    budget = max((s["max_turns"] for s in store.seats(topic_id)),
                 default=Caps.max_turns_per_seat)
    return Supervisor(store, build_drivers(store),
                      Caps(effort=topic["effort"] or "low",
                           max_turns_per_seat=budget))


def build_app(db: Path | str | None, token: str, *, human: str,
              supervisor=default_supervisor):
    """The application, separate from serving it, so tests can drive it."""
    from aiohttp import web

    store = connect(db)
    app = web.Application()
    # AppKey rather than bare strings: aiohttp warns on the latter, and a
    # screenful of warnings is where a real one goes to hide.
    app[STORE] = store
    app[TOKEN] = token
    app[HUMAN] = human
    app[RUNNING] = {}
    app[SUPERVISOR] = supervisor

    async def stop_councils(app):
        """Cancel in-flight councils before the loop goes.

        A wake that is never cancelled leaves a CLI running and its pipes open,
        and the loop closes on top of them.
        """
        for task in app[RUNNING].values():
            task.cancel()
        for task in app[RUNNING].values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    app.on_cleanup.append(stop_councils)

    @web.middleware
    async def authenticate(request, handler):
        if request.path == "/" or request.path.startswith("/static"):
            return await handler(request)
        supplied = request.headers.get("Authorization", "")
        prefix = "Bearer "
        # Only the header. A token in a query string ends up in server logs,
        # browser history and Referer headers, and this token is an identity.
        if not supplied.startswith(prefix):
            return web.json_response({"error": "bad or missing token"}, status=401)
        token = supplied[len(prefix):]

        # A per-seat token says who you are. The single shared token, if one was
        # issued, says only that you may read -- it belongs to no seat, so it
        # cannot rule and cannot post.
        seat = request.app[STORE].seat_for_token(token)
        shared = request.app[TOKEN]
        if seat is None and not (shared and secrets.compare_digest(token, shared)):
            return web.json_response({"error": "bad or missing token"}, status=401)
        request["seat"] = seat
        return await handler(request)

    def acting(request):
        """The seat this request acts as, or None for the shared read token."""
        return request.get("seat")

    async def decide(request):
        """Sign off on a proposal, over HTTP.

        The fence is `Store.decide`, which refuses a non-human because locally
        identity comes from the operating system. There is none here, so the
        token is the identity -- which is why a token can only ever be issued to
        a human seat, and why the shared read token cannot reach this at all.
        """
        s = request.app[STORE]
        who = acting(request)
        if not who:
            return web.json_response(
                {"error": "this token may read but not sign off; that needs a "
                          "token issued to a seat (mooting serve --grant <seat>)"},
                status=403)
        try:
            pid = int(request.match_info["pid"])
            pr = s.proposal(pid)
        except (StoreError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=404)

        body = await request.json()
        if "approve" not in body:
            return web.json_response({"error": "say approve: true or false"},
                                     status=400)
        approve = bool(body["approve"])
        why = (body.get("why") or "").strip()
        try:
            s.decide(pid, who, approve=approve, rationale=why)
        except NotAuthorised as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except StoreError as exc:
            return web.json_response({"error": str(exc)}, status=409)

        # An approval that arrived over HTTP is a different fact from one typed
        # at the terminal, and afterwards only the board can say which it was.
        s.audit(who, "decide", {"proposal_id": pid, "approve": approve},
                topic_id=int(pr["topic_id"]))
        return web.json_response({"proposal": pid,
                                  "status": s.proposal(pid)["status"],
                                  "by": who})

    app.middlewares.append(authenticate)

    async def topics(request):
        s = request.app[STORE]
        return web.json_response(
            {"topics": [_topic_json(s, t) for t in s.topics()],
             "you": request.app[HUMAN]})

    async def topic_detail(request):
        s = request.app[STORE]
        try:
            t = s.topic(request.match_info["slug"])
        except StoreError as exc:
            return web.json_response({"error": str(exc)}, status=404)
        tid = int(t["id"])
        out = _topic_json(s, t)
        out["messages"] = [_message_json(m) for m in s.transcript(tid)[-100:]]
        out["proposals"] = [
            {"id": p["id"], "title": p["title"], "body": p["body"],
             "author": p["author"], "status": p["status"]}
            for p in s.proposals(tid)
        ]
        return web.json_response(out)

    async def say(request):
        """Post as the token's human seat.

        Not a ruling: `Store.post` records a message, and the human-only fence on
        decisions is untouched. B3 is where a ruling gets a remote path, once
        this one has been exercised.
        """
        s = request.app[STORE]
        try:
            t = s.topic(request.match_info["slug"])
        except StoreError as exc:
            return web.json_response({"error": str(exc)}, status=404)
        body = (await request.json()).get("body", "").strip()
        if not body:
            return web.json_response({"error": "empty message"}, status=400)
        try:
            who = acting(request)
            if not who:
                return web.json_response(
                    {"error": "this token may read but not speak"}, status=403)
            s.seat_human(int(t["id"]), who)     # a token-holder may take part
            mid = s.post(int(t["id"]), who, body, count_turn=False)
            s.audit(who, "say", {"message_id": mid}, topic_id=int(t["id"]))
        except StoreError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        return web.json_response({"id": mid})

    async def events(request):
        """Poll form of the stream, for clients that cannot hold a connection."""
        s = request.app[STORE]
        since = int(request.query.get("since", 0))
        topic_id = None
        if "slug" in request.query:
            topic_id = int(s.topic(request.query["slug"])["id"])
        evs = s.events_since(since, topic_id)
        return web.json_response({"events": [
            {"id": e.id, "kind": e.kind, "actor": e.actor, "payload": e.payload,
             "at": e.created_at} for e in evs]})

    async def stream(request):
        """Server-Sent Events from a cursor.

        Resumes from `Last-Event-ID` (what an `EventSource` sends by itself) or
        `?since=` (what a client using `fetch` supplies), so a dropped
        connection loses nothing either way.
        """
        s = request.app[STORE]
        since = int(request.headers.get("Last-Event-ID")
                    or request.query.get("since", 0) or 0)
        topic_id = None
        if "slug" in request.query:
            try:
                topic_id = int(s.topic(request.query["slug"])["id"])
            except StoreError as exc:
                return web.json_response({"error": str(exc)}, status=404)

        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",      # nginx would otherwise hold it back
        })
        await resp.prepare(request)
        try:
            while True:
                for e in s.events_since(since, topic_id):
                    since = e.id
                    body = {"kind": e.kind, "actor": e.actor,
                            "payload": e.payload, "at": e.created_at}
                    # Carry the message with the event. Without it every client
                    # must fetch the topic again to learn what was said, which
                    # is a race as well as a round trip.
                    if e.kind == "message":
                        row = s.q1("SELECT * FROM messages WHERE id = ?",
                                   (e.payload.get("message_id"),))
                        if row is not None:
                            body["message"] = _message_json(row)
                    data = json.dumps(body)
                    await resp.write(f"id: {e.id}\ndata: {data}\n\n".encode())
                # A comment line keeps proxies and browsers from timing the
                # connection out while a council is thinking, which is minutes.
                await resp.write(b": keepalive\n\n")
                await asyncio.sleep(1.0)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    async def patch_topic(request):
        """Retune an open topic: agenda, effort, rounds.

        Everything a session can do to a topic short of ruling on it. Each field
        goes through the same Store call the session uses, so the rules -- a
        topic runs for at least one round, turns follow rounds -- are enforced in
        one place rather than re-stated here.
        """
        s = request.app[STORE]
        try:
            t = s.topic(request.match_info["slug"])
        except StoreError as exc:
            return web.json_response({"error": str(exc)}, status=404)
        tid, who = int(t["id"]), request.app[HUMAN]
        patch = await request.json()
        try:
            if "agenda" in patch:
                points = patch["agenda"]
                if isinstance(points, str):
                    points = split_points(points)
                s.set_brief(tid, agenda_text(points) or t["title"], who)
            if "effort" in patch:
                if patch["effort"] not in {"low", "medium", "high"}:
                    return web.json_response({"error": "effort must be low|medium|high"},
                                             status=400)
                with s.tx() as c:
                    c.execute("UPDATE topics SET effort = ? WHERE id = ?",
                              (patch["effort"], tid))
            if "rounds" in patch:
                s.set_rounds(tid, int(patch["rounds"]))
        except (StoreError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(_topic_json(s, s.topic(tid)))

    async def proposals(request):
        """A proposal in full -- body, votes, objections.

        Readable here because reading is what B1 established. Ruling on one is
        still not a route; see B3.
        """
        s = request.app[STORE]
        try:
            pr = s.proposal(int(request.match_info["pid"]))
        except (StoreError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=404)
        return web.json_response({
            "id": pr["id"], "title": pr["title"], "body": pr["body"],
            "author": pr["author"], "status": pr["status"],
            "votes": [{"agent": v["agent"], "stance": v["stance"],
                       "rationale": v["rationale"]} for v in s.votes(int(pr["id"]))],
            "decide": f"POST /api/proposals/{pr['id']}/decide with "
                      f"{{approve: true|false, why: '...'}} — needs a token "
                      f"issued to a human seat",
        })

    async def run_topic(request):
        """Drive the council, in this process.

        The supervisor lives wherever the driving happens. With several clients
        that has to be one place, or two people pressing Run start two councils
        on one board and every seat is woken twice. So the server keeps one task
        per topic and refuses to start a second.
        """
        s = request.app[STORE]
        try:
            t = s.topic(request.match_info["slug"])
        except StoreError as exc:
            return web.json_response({"error": str(exc)}, status=404)
        tid = int(t["id"])
        running = request.app[RUNNING]
        if tid in running and not running[tid].done():
            return web.json_response({"error": "already running",
                                      "topic": t["slug"]}, status=409)

        make = request.app[SUPERVISOR]
        sup = make(s, tid)

        async def drive():
            try:
                reason = await sup.run_topic(tid)
                log.info("council on %s stopped: %s", t["slug"], reason)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("council on %s failed", t["slug"])

        running[tid] = asyncio.create_task(drive())
        return web.json_response({"running": t["slug"]})

    async def stop_topic(request):
        s = request.app[STORE]
        try:
            t = s.topic(request.match_info["slug"])
        except StoreError as exc:
            return web.json_response({"error": str(exc)}, status=404)
        tid = int(t["id"])
        task = request.app[RUNNING].get(tid)
        if task is None or task.done():
            return web.json_response({"running": None})
        task.cancel()
        return web.json_response({"stopping": t["slug"]})

    async def index(request):
        return web.Response(text=PAGE, content_type="text/html")

    app.router.add_get("/", index)
    app.router.add_get("/api/topics", topics)
    app.router.add_get("/api/topics/{slug}", topic_detail)
    app.router.add_post("/api/topics/{slug}/messages", say)
    app.router.add_patch("/api/topics/{slug}", patch_topic)
    app.router.add_post("/api/topics/{slug}/run", run_topic)
    app.router.add_post("/api/topics/{slug}/stop", stop_topic)
    app.router.add_get("/api/proposals/{pid}", proposals)
    app.router.add_post("/api/proposals/{pid}/decide", decide)
    app.router.add_get("/api/events", events)
    app.router.add_get("/api/stream", stream)
    return app


def serve(db, *, host: str, port: int, token: str | None, human: str,
          allow_remote: bool = False) -> int:
    from aiohttp import web

    if host not in LOOPBACK and not allow_remote:
        raise StoreError(
            f"refusing to bind {host}: whoever reaches this port can act as "
            f"`{human}`, and rulings are enforced by identity.\n"
            f"  reach it over an SSH tunnel, or pass --allow-remote once it is "
            f"behind something that authenticates.")

    token = token or secrets.token_urlsafe(24)
    app = build_app(db, token, human=human)
    print(f"  board    {app[STORE].path}")
    print(f"  you      {human}")
    print(f"  url      http://{host}:{port}/?token={token}")
    print(f"  token    {token}")
    print()
    print("  read-only page; every API call needs the token in an Authorization")
    print("  header. The URL above carries it only so the page can pick it up --")
    print("  it will be in your browser history, so treat it as a password.")
    if host not in LOOPBACK:
        print("  WARNING: not loopback. Anyone who reaches this port is you.")
    web.run_app(app, host=host, port=port, print=None)
    return 0


#: Deliberately one file with no build step. It is a window onto a council, not
#: an application, and B1 is meant to prove the stream rather than a front end.
PAGE = r"""<!doctype html>
<meta charset="utf-8"><title>mooting</title>
<style>
 :root { --bg:#16222B; --fg:#F8F7F3; --dim:#8A949B; --teal:#9FB4B3; --raised:#26333C; }
 body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.6 "Segoe UI",system-ui,sans-serif; }
 header { padding:14px 20px; border-bottom:1px solid rgba(159,180,179,.22); }
 h1 { margin:0; font-size:1.1rem; font-weight:600; }
 #agenda { margin:6px 0 0; padding-left:20px; color:var(--dim); font-size:14px; }
 main { padding:16px 20px; max-width:900px; }
 .msg { margin:0 0 14px; padding:8px 12px; background:var(--raised); border-radius:4px; }
 .who { font-weight:600; color:var(--teal); }
 .kind { color:var(--dim); font-size:12px; margin-left:6px; }
 pre { white-space:pre-wrap; word-wrap:break-word; margin:6px 0 0; font:13px/1.55 Consolas,monospace; }
 #state { color:var(--dim); font-size:13px; padding:0 20px 20px; }
</style>
<header><h1 id="title">connecting…</h1><ul id="agenda"></ul></header>
<main id="log"></main>
<div id="state"></div>
<script>
const token = new URLSearchParams(location.search).get("token") || "";
const auth = { headers: { Authorization: "Bearer " + token } };
const log = document.getElementById("log");
let slug = null;

function add(author, kind, body) {
  const d = document.createElement("div");
  d.className = "msg";
  d.innerHTML = '<span class="who"></span><span class="kind"></span><pre></pre>';
  d.querySelector(".who").textContent = author;
  d.querySelector(".kind").textContent = kind === "say" ? "" : kind;
  d.querySelector("pre").textContent = body;
  log.appendChild(d);
  window.scrollTo(0, document.body.scrollHeight);
}

async function boot() {
  const r = await fetch("/api/topics", auth);
  if (!r.ok) { document.getElementById("title").textContent =
    "bad or missing token — open the URL printed by `mooting serve`"; return; }
  const { topics } = await r.json();
  const live = topics.find(t => t.status !== "resolved") || topics[0];
  if (!live) { document.getElementById("title").textContent = "no topics yet"; return; }
  slug = live.slug;
  const detail = await (await fetch("/api/topics/" + slug, auth)).json();
  document.getElementById("title").textContent = detail.title + "  (" + detail.slug + ")";
  const ul = document.getElementById("agenda");
  detail.agenda.forEach(p => { const li = document.createElement("li");
                               li.textContent = p; ul.appendChild(li); });
  let last = 0;
  detail.messages.forEach(m => { add(m.author, m.kind, m.body); last = m.id; });
  follow(detail);
}

function follow(detail) {
  document.getElementById("state").textContent =
    "round " + (detail.round + 1) + "/" + detail.max_rounds + " · following…";
  // Not EventSource: it cannot send headers, and the token belongs in one.
  // The automatic reconnect it would have given us is a few lines here, because
  // the board's event ids are the cursor -- we resume from the last one seen.
  let cursor = detail.messages.length
      ? detail.messages[detail.messages.length - 1].id : 0;
  (async function stream() {
    for (;;) {
      try {
        const r = await fetch("/api/stream?slug=" + slug + "&since=" + cursor, auth);
        const reader = r.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let cut;
          while ((cut = buf.indexOf("\n\n")) >= 0) {
            const frame = buf.slice(0, cut); buf = buf.slice(cut + 2);
            const id = /^id: (\d+)$/m.exec(frame);
            const data = /^data: (.*)$/m.exec(frame);
            if (id) cursor = parseInt(id[1], 10);
            if (!data) continue;
            const e = JSON.parse(data[1]);
            if (e.message) add(e.message.author, e.message.kind, e.message.body);
          }
        }
      } catch (err) { /* fall through to the wait */ }
      document.getElementById("state").textContent = "reconnecting…";
      await new Promise(r => setTimeout(r, 2000));
    }
  })();
}
boot();
</script>
"""
