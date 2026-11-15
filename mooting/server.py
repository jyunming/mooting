"""`mooting serve` — the board, over HTTP, for clients that are not a terminal.

Read the board, say things to it, drive the council and sign off. The bundled
page at `/` does all four through the routes below and nothing else, so the
page cannot do anything a script with the same token could not.

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
import os
import secrets
import uuid
from pathlib import Path

from .store import (NotAuthorised, Store, StoreError, agenda_points,
                    agenda_text, connect, split_points)

log = logging.getLogger("mooting.server")

#: This process, to the board's drive claim. Two servers on one board are two
#: processes, so the in-process task table cannot be what decides.
SESSION = f"serve-{os.getpid()}-{uuid.uuid4().hex[:6]}"

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

#: The seat a request acts as. `RequestKey` arrived in aiohttp 3.10 and the
#: floor is 3.9, where a plain string key is what works.
try:
    from aiohttp.web import RequestKey
    SEAT: object = RequestKey("seat", object)
except ImportError:
    SEAT = "seat"

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

        # A per-seat token says who you are. The single shared token belongs to
        # no seat, so it cannot sign off and cannot post. It can still run, stop
        # and retune a meeting, which spends quota -- see ROADMAP E5.
        seat = request.app[STORE].seat_for_token(token)
        shared = request.app[TOKEN]
        if seat is None and not (shared and secrets.compare_digest(token, shared)):
            return web.json_response({"error": "bad or missing token"}, status=401)
        request[SEAT] = seat
        return await handler(request)

    def acting(request):
        """The seat this request acts as, or None for the shared read token."""
        return request.get(SEAT)

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
            s.decide(pid, who, approve=approve, rationale=why, via="http")
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
             "you": request.app[HUMAN],
             # Who this token speaks as, or None for the shared read token. The
             # page needs it to say why Send and Sign off would be refused
             # before somebody types a reason and is told 403.
             "seat": acting(request)})

    async def topic_detail(request):
        s = request.app[STORE]
        try:
            t = s.topic(request.match_info["slug"])
        except StoreError as exc:
            return web.json_response({"error": str(exc)}, status=404)
        tid = int(t["id"])
        # Read before the transcript. Seats post from their own processes, so a
        # message landing between the two reads is then streamed rather than
        # lost. The page used to resume the stream from the last *message* id,
        # which is a different counter: every board event was replayed from
        # that number, and the messages among them appeared twice.
        head = s.head()
        out = _topic_json(s, t)
        out["head"] = head
        out["messages"] = [_message_json(m)
                           for m in s.transcript(tid, limit=100, newest=True)]
        out["proposals"] = [
            {"id": p["id"], "title": p["title"], "body": p["body"],
             "author": p["author"], "status": p["status"]}
            for p in s.proposals(tid)
        ]
        return web.json_response(out)

    async def say(request):
        """Post as the token's human seat.

        Not a sign-off: `Store.post` records a message and nothing else. Signing
        off has its own route now, and it ends where every other path does --
        at `Store.decide`, which refuses a token that is not a person's.
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
                s.set_rounds(tid, int(patch["rounds"]), who)
        except (StoreError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(_topic_json(s, s.topic(tid)))

    async def proposals(request):
        """A proposal in full -- body, votes, objections.

        Readable here because reading is what B1 established. Signing one off is
        `POST /api/proposals/{pid}/decide`, which this response names below --
        and which still ends at `Store.decide`, so the token has to belong to a
        human seat.
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
        # `running` is this process only. A second `mooting serve` on the same
        # board would pass that check and drive the same topic, so the claim that
        # actually decides is the one on the board.
        holder = s.take_drive(tid, SESSION)
        if holder is not None:
            return web.json_response({"error": "already being driven by another session",
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
            finally:
                s.release_drive(tid, SESSION)

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
    print("  this token reads, runs and stops; it cannot speak or sign off. For")
    print("  that, `mooting serve --grant <you>` and paste that token into the page.")
    print("  The URL above will be in your browser history: treat it as a password.")
    if host not in LOOPBACK:
        print("  WARNING: not loopback. Anyone who reaches this port is you.")
    web.run_app(app, host=host, port=port, print=None)
    return 0


#: Deliberately one file with no build step. It is a window onto a council and
#: the chair's controls for it, not an application: every button is one of the
#: routes above, so the page can do nothing a script with the same token cannot.
PAGE = r"""<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>mooting</title>
<style>
 :root { --bg:#16222B; --fg:#F8F7F3; --dim:#8A949B; --teal:#9FB4B3; --raised:#26333C;
         --line:rgba(159,180,179,.22); --ok:#9FCB98; --no:#E3A08F; }
 * { box-sizing:border-box; }
 body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.6 "Segoe UI",system-ui,sans-serif; }
 header, main { padding:14px 16px; max-width:900px; }
 header { border-bottom:1px solid var(--line); }
 .row { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
 h1 { margin:0; font-size:1.1rem; font-weight:600; flex:1 1 auto; }
 h2 { font-size:.95rem; margin:20px 0 8px; color:var(--teal); font-weight:600; }
 #agenda { margin:6px 0 8px; padding-left:20px; color:var(--dim); font-size:14px; }
 .msg, .prop { margin:0 0 12px; padding:8px 12px; background:var(--raised); border-radius:4px; }
 .who { font-weight:600; color:var(--teal); }
 .kind { color:var(--dim); font-size:12px; margin-left:6px; }
 pre { white-space:pre-wrap; word-wrap:break-word; margin:6px 0 0; font:13px/1.55 Consolas,monospace; }
 .note, #state { color:var(--dim); font-size:13px; }
 button, select, input, textarea { font:inherit; color:var(--fg); background:var(--raised);
   border:1px solid var(--line); border-radius:4px; padding:4px 10px; }
 button { cursor:pointer; }
 button:disabled, textarea:disabled { opacity:.45; cursor:not-allowed; }
 button.ok { border-color:var(--ok); color:var(--ok); }
 button.no { border-color:var(--no); color:var(--no); }
 textarea, input[type=text], input[type=password] { width:100%; }
 form, details { margin:14px 0; }
 summary { cursor:pointer; color:var(--teal); }
 .stances { font-size:13px; color:var(--dim); margin-top:6px; }
 .signoff { margin-top:8px; }
 #flash { min-height:1.4em; font-size:13px; color:var(--no); }
</style>
<header>
  <div class="row"><h1 id="title">connecting…</h1><select id="topic" aria-label="meeting"></select></div>
  <ul id="agenda"></ul>
  <div class="row"><span id="state"></span>
    <button id="run">Run the council</button><button id="stop">Stop</button></div>
  <p class="note">Run wakes every seat in turn; each turn spends that CLI's own quota, up to the meeting's caps.</p>
</header>
<main>
  <div id="flash" role="status"></div>
  <section id="log"></section>
  <h2>Proposals</h2>
  <section id="plist"><p class="note">none yet</p></section>
  <form id="sayform">
    <textarea id="body" rows="3" placeholder="say something to the council"></textarea>
    <div class="row" style="margin-top:6px"><button id="send">Send</button>
      <span class="note" id="sayhint"></span></div>
  </form>
  <details id="settings"><summary>Meeting settings</summary>
    <p class="note">Agenda, one point per line.</p>
    <textarea id="agendaedit" rows="4"></textarea>
    <div class="row" style="margin-top:6px">
      <label>effort <select id="effort"><option>low</option><option>medium</option><option>high</option></select></label>
      <label>rounds <input id="rounds" type="number" min="1" style="width:5em"></label>
      <button id="save">Save</button></div>
  </details>
  <details id="identity"><summary id="whoami">…</summary>
    <p class="note">A token from <code>mooting serve --grant &lt;you&gt;</code> speaks and signs off as
      that seat. It is kept in this tab only and never put in the address bar.</p>
    <div class="row"><input id="tok" type="password" autocomplete="off" placeholder="paste a seat token">
      <button id="usetok">Use it</button><button id="forget">Forget it</button></div>
  </details>
</main>
<script>
"use strict";
// Every string that came from the board -- a message, a proposal, a seat's
// name -- goes in through textContent and nothing else. A seat writes those
// strings, and once this page holds a seat token a script hidden in one would
// sign off as the chair.
const $ = id => document.getElementById(id);
const KEY = "mooting-seat-token";
const shared = new URLSearchParams(location.search).get("token") || "";
function saved() { try { return sessionStorage.getItem(KEY) || ""; } catch (e) { return ""; } }
let seatToken = saved();
// The meeting this tab was looking at, so a reload does not drop the chair
// into whichever topic happens to be newest.
function lastSlug() { try { return sessionStorage.getItem("mooting-topic"); } catch (e) { return null; } }
let seat = null, slug = lastSlug(), detail = null, abort = null;
const seen = new Set();

function token() { return seatToken || shared; }
async function api(method, path, body) {
  const headers = { Authorization: "Bearer " + token() };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let r;
  try {
    r = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch (e) { return { ok: false, status: 0, data: { error: "the server did not answer" } }; }
  let data = {};
  try { data = await r.json(); } catch (e) {}
  return { ok: r.ok, status: r.status, data };
}
function flash(text) { $("flash").textContent = text || ""; }
function el(tag, cls, text) {
  const d = document.createElement(tag);
  if (cls) d.className = cls;
  if (text !== undefined) d.textContent = text;
  return d;
}
const path = s => "/api/topics/" + encodeURIComponent(s);

function addMessage(m) {
  if (seen.has(m.id)) return;
  seen.add(m.id);
  const d = el("div", "msg");
  d.appendChild(el("span", "who", m.author));
  d.appendChild(el("span", "kind", m.kind === "say" ? "" : m.kind));
  d.appendChild(el("pre", "", m.body));
  $("log").appendChild(d);
  window.scrollTo(0, document.body.scrollHeight);
}

function canAct() { return seat !== null; }
function renderIdentity() {
  $("whoami").textContent = canAct()
    ? "speaking as " + seat
    : "watching with the shared token: you can read, run and stop. Open this to speak and sign off.";
  $("body").disabled = $("send").disabled = !canAct();
  $("sayhint").textContent = canAct() ? "" : "needs a seat token, below";
  $("forget").disabled = !seatToken;
}

function renderSide(d) {
  $("title").textContent = d.title + "  (" + d.slug + ")";
  const ul = $("agenda"); ul.replaceChildren();
  d.agenda.forEach(p => ul.appendChild(el("li", "", p)));
  $("state").textContent = d.status + " · round " + Math.min(d.round + 1, d.max_rounds) + "/" + d.max_rounds;
  if (!$("settings").open) {
    $("agendaedit").value = d.agenda.join("\n");
    $("effort").value = d.effort || "low";
    $("rounds").value = d.max_rounds;
  }
  renderProposals(d.proposals);
}

async function renderProposals(list) {
  const box = $("plist");
  const open = list.filter(p => p.status === "open");
  const rest = list.filter(p => !open.includes(p));
  box.replaceChildren();
  if (!list.length) { box.appendChild(el("p", "note", "none yet")); return; }
  for (const p of open) box.appendChild(await proposalCard(p));
  rest.forEach(p => box.appendChild(el("p", "note", "#" + p.id + " " + p.title + " — " + p.status)));
}

async function proposalCard(p) {
  const d = el("div", "prop");
  d.appendChild(el("span", "who", "#" + p.id + " " + p.title));
  d.appendChild(el("span", "kind", "by " + p.author + " · " + p.status));
  d.appendChild(el("pre", "", p.body));
  const full = await api("GET", "/api/proposals/" + encodeURIComponent(p.id));
  if (full.ok && full.data.votes && full.data.votes.length) {
    const s = el("div", "stances");
    full.data.votes.forEach(v => s.appendChild(el("div", "", v.agent + ": " + v.stance + (v.rationale ? " — " + v.rationale : ""))));
    d.appendChild(s);
  }
  const area = el("div", "signoff");
  const start = el("button", "", "Sign off…");
  start.disabled = !canAct();
  if (!canAct()) start.title = "needs a seat token";
  area.appendChild(start);
  start.onclick = () => {
    area.replaceChildren();
    const why = el("input"); why.type = "text"; why.placeholder = "why (the seats read this)";
    const yes = el("button", "ok", "Approve"), no = el("button", "no", "Reject"), back = el("button", "", "Cancel");
    const row = el("div", "row"); row.style.marginTop = "6px";
    [yes, no, back].forEach(b => row.appendChild(b));
    area.appendChild(why); area.appendChild(row);
    why.focus();
    const decide = async approve => {
      yes.disabled = no.disabled = true;
      const r = await api("POST", "/api/proposals/" + encodeURIComponent(p.id) + "/decide",
                          { approve, why: why.value });
      if (!r.ok) { flash(r.data.error || ("refused: " + r.status)); yes.disabled = no.disabled = false; return; }
      flash("");
      refresh();
    };
    yes.onclick = () => decide(true);
    no.onclick = () => decide(false);
    back.onclick = () => refresh();
  };
  d.appendChild(area);
  return d;
}

async function refresh() {
  if (!slug) return;
  const r = await api("GET", path(slug));
  if (r.ok) { detail = r.data; renderSide(detail); }
}
let pending = null;
function refreshSoon() { clearTimeout(pending); pending = setTimeout(refresh, 300); }

async function open(s) {
  if (abort) abort.abort();
  slug = s; seen.clear(); $("log").replaceChildren(); flash("");
  try { sessionStorage.setItem("mooting-topic", s); } catch (e) {}
  const r = await api("GET", path(s));
  if (!r.ok) { flash(r.data.error || "could not open " + s); return; }
  detail = r.data;
  renderSide(detail);
  detail.messages.forEach(addMessage);
  follow(s, detail.head);
}

function follow(s, cursor) {
  // Not EventSource: it cannot send headers, and the token belongs in one.
  // The board's event ids are the cursor, so a reconnect resumes from the last
  // one seen and misses nothing.
  const ctl = new AbortController(); abort = ctl;
  (async () => {
    while (!ctl.signal.aborted) {
      try {
        const r = await fetch("/api/stream?slug=" + encodeURIComponent(s) + "&since=" + cursor,
                              { headers: { Authorization: "Bearer " + token() }, signal: ctl.signal });
        const reader = r.body.getReader(), dec = new TextDecoder();
        let buf = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let cut;
          while ((cut = buf.indexOf("\n\n")) >= 0) {
            const frame = buf.slice(0, cut); buf = buf.slice(cut + 2);
            const id = /^id: (\d+)$/m.exec(frame), data = /^data: (.*)$/m.exec(frame);
            if (id) cursor = parseInt(id[1], 10);
            if (!data) continue;
            const e = JSON.parse(data[1]);
            if (e.message) addMessage(e.message);
            else refreshSoon();       // a proposal, a vote, a sign-off, a status change
          }
        }
      } catch (err) { if (ctl.signal.aborted) return; }
      $("state").textContent = "reconnecting…";
      await new Promise(r => setTimeout(r, 2000));
    }
  })();
}

$("sayform").onsubmit = async ev => {
  ev.preventDefault();
  const text = $("body").value.trim();
  if (!text) return;
  $("send").disabled = true;
  const r = await api("POST", path(slug) + "/messages", { body: text });
  $("send").disabled = !canAct();
  if (!r.ok) { flash(r.data.error || ("refused: " + r.status)); return; }
  $("body").value = ""; flash("");
};
$("run").onclick = async () => {
  const r = await api("POST", path(slug) + "/run");
  flash(r.ok ? "" : (r.data.error || ("refused: " + r.status)));
  if (r.ok) $("state").textContent = "running…";
};
$("stop").onclick = async () => {
  const r = await api("POST", path(slug) + "/stop");
  flash(r.ok ? (r.data.stopping ? "" : "nothing was running") : (r.data.error || ("refused: " + r.status)));
};
$("save").onclick = async () => {
  const agenda = $("agendaedit").value.split("\n").map(x => x.trim()).filter(Boolean);
  const r = await api("PATCH", path(slug),
                      { agenda, effort: $("effort").value, rounds: parseInt($("rounds").value, 10) });
  if (!r.ok) { flash(r.data.error || ("refused: " + r.status)); return; }
  flash(""); $("settings").open = false; detail = Object.assign(detail, r.data); renderSide(detail);
};
$("usetok").onclick = async () => {
  const t = $("tok").value.trim();
  if (!t) return;
  const before = seatToken; seatToken = t;
  const r = await api("GET", "/api/topics");
  if (!r.ok || !r.data.seat) {
    seatToken = before; flash("that token was not accepted as a seat's");
    return;
  }
  try { sessionStorage.setItem(KEY, t); } catch (e) {}
  $("tok").value = ""; $("identity").open = false; flash("");
  seat = r.data.seat; renderIdentity(); open(slug);
};
$("forget").onclick = () => {
  try { sessionStorage.removeItem(KEY); } catch (e) {}
  seatToken = ""; boot();
};
$("topic").onchange = () => open($("topic").value);

async function boot() {
  const r = await api("GET", "/api/topics");
  if (!r.ok && seatToken) {           // a revoked seat token: fall back to the shared one
    try { sessionStorage.removeItem(KEY); } catch (e) {}
    seatToken = ""; return boot();
  }
  if (!r.ok) { $("title").textContent = "bad or missing token — open the URL printed by `mooting serve`"; return; }
  seat = r.data.seat || null;
  renderIdentity();
  const topics = r.data.topics;
  const sel = $("topic"); sel.replaceChildren();
  topics.forEach(t => { const o = el("option", "", t.slug + " — " + t.status); o.value = t.slug; sel.appendChild(o); });
  const live = topics.find(t => t.slug === slug) || topics.find(t => t.status === "open")
            || topics.find(t => t.status !== "resolved") || topics[0];
  if (!live) { $("title").textContent = "no topics yet"; return; }
  sel.value = live.slug;
  open(live.slug);
}
boot();
</script>
"""
