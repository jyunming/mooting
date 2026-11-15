"""Show that each planted G4b defect happens. Spends nothing.

Usage: python -X utf8 experiments/g4b/check_seeds.py
Exit code 0 only if all sixteen are shown. The answer key is ANSWER_KEY.md.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE / "seeds"))

import c1_kvstore, c2_mailmerge, c3_feed, c4_settings, c5_jobs, c6_ratelimit, c7_directory, c8_export  # noqa: E402

shown: dict[str, bool] = {}


def report(item: str, defect: bool, what: str) -> None:
    shown[item] = defect
    print(f"{item}: {'DEFECT' if defect else 'ok    '}  {what}")


tmp = Path(tempfile.mkdtemp(prefix="g4b-check-"))

# c1.1 -- hash() is salted per process, so a key lands in another shard next run
store_dir = tmp / "kv"
keys = [f"user{i}" for i in range(20)]
code = ("import sys; sys.path.insert(0, sys.argv[1]); import c1_kvstore as m; s = m.Store(sys.argv[2]); "
        "ks = sys.argv[3].split(',');\n"
        "if sys.argv[4] == 'put':\n    [s.put(k, k) for k in ks]\n"
        "else:\n    print(sum(s.get(k) is None for k in ks))")
env = lambda seed: {**os.environ, "PYTHONHASHSEED": str(seed)}  # noqa: E731
subprocess.run([sys.executable, "-c", code, str(HERE / "seeds"), str(store_dir), ",".join(keys), "put"],
               env=env(1), check=True)
lost = int(subprocess.run([sys.executable, "-c", code, str(HERE / "seeds"), str(store_dir), ",".join(keys), "get"],
                          env=env(2), check=True, capture_output=True, text=True).stdout)
report("c1.1", lost > 0, f"20 keys put by one process, {lost} not found by the next")

# c1.2 -- get returns the first record of a key, not the latest
s = c1_kvstore.Store(tmp / "kv2")
s.put("k", "old")
s.put("k", "new")
report("c1.2", s.get("k") != "new", f"put old then new; get = {s.get('k')!r}")

# c2.1 -- & is escaped last, so the entities just written are escaped again
got = c2_mailmerge.render("<p>{name}</p>", {"name": "<Ann & Bo>"})
report("c2.1", "&amp;lt;" in got, f"render name '<Ann & Bo>' -> {got!r}")

# c2.2 -- the customer's value is a re.sub replacement string
got = c2_mailmerge.render("Ship to {addr}", {"addr": r"C:\new\folder"})
try:
    c2_mailmerge.render("Hi {name}", {"name": r"Bob\d"})
    crash = "rendered"
except Exception as exc:  # re.error
    crash = f"{type(exc).__name__}: {exc}"
report("c2.2", "\n" in got or crash != "rendered",
       f"value C:\\new\\folder -> {got!r}; value Bob\\d -> {crash}")

# c3.1 -- rows sharing the boundary second are skipped
db = c3_feed.connect()
db.executemany("INSERT INTO events (created_at, actor, verb) VALUES (?,?,?)",
               [(1700000000, "a", "x"), (1700000000, "b", "x"), (1700000000, "c", "x"), (1700000001, "d", "x")])
real_decode = c3_feed.decode_cursor
# c3.2 would stop the walk at page 2; restore the padding so c3.1 is seen on its own
c3_feed.decode_cursor = lambda tok: real_decode(tok + "=" * (-len(tok) % 4))
got = c3_feed.everything(db, size=2)
c3_feed.decode_cursor = real_decode
report("c3.1", len(got) != 4, f"4 events, 3 in the same second, page size 2: export got {len(got)} (c3.2 patched out)")

# c3.2 -- padding is stripped on encode and not restored on decode
cur = c3_feed.encode_cursor(1700000000)
try:
    c3_feed.decode_cursor(cur)
    report("c3.2", False, "cursor round-trips")
except ValueError as exc:
    report("c3.2", True, f"cursor {cur!r} from encode_cursor -> {exc}")

# c4.1 -- the shallow merge shares the cached defaults' lists, and += grows them
(tmp / "tenants").mkdir()
(tmp / "defaults.json").write_text(json.dumps(
    {"mail": {"bcc": ["audit@x"], "extra_bcc": "boss@x"}, "limits": {"upload_mb": 10}}), encoding="utf-8")
c4_settings.DEFAULTS_FILE = tmp / "defaults.json"
c4_settings.TENANT_DIR = tmp / "tenants"
for _ in range(3):
    last = c4_settings.settings_for("acme")
other = c4_settings.settings_for("globex")["mail"]["bcc"]
report("c4.1", len(last["mail"]["bcc"]) != 2, f"third request for acme: bcc = {last['mail']['bcc']}; next tenant gets {other}")

# c4.2 -- ^ is xor
got = c4_settings.upload_limit_bytes("acme")
report("c4.2", got != 10 * 1024 * 1024, f"upload_mb 10 -> {got} bytes, expected {10 * 1024 * 1024}")

# c5.1 -- two jobs due at the same time compare Job objects
sch = c5_jobs.Scheduler()
try:
    sch.schedule(lambda: None, 100.0)
    sch.schedule(lambda: None, 100.0)
    report("c5.1", False, "two jobs at the same time scheduled")
except TypeError as exc:
    report("c5.1", True, f"two jobs at t=100 -> TypeError: {exc}")

# c5.2 -- a repeating job comes back under a new id, so cancelling it does nothing
sch = c5_jobs.Scheduler()
runs = []
jid = sch.schedule(lambda: runs.append(1), 10.0, every=10.0)
sch.tick(10.0)
ok = sch.cancel(jid)
sch.tick(20.0)
sch.tick(30.0)
report("c5.2", len(runs) > 1, f"cancel({jid}) after first run returned {ok}; job ran {len(runs)} times in all")

# c6.1 -- refill is truncated to whole tokens and `last` moves every call
t = [0.0]
lim = c6_ratelimit.Limiter(clock=lambda: t[0])
for _ in range(10):
    lim.allow("ip")
allowed = 0
for _ in range(50):                      # one request every 0.1 s for 5 s
    t[0] += 0.1
    allowed += lim.allow("ip")
report("c6.1", allowed < 20, f"after a burst, a client polling every 0.1 s for 5 s got {allowed} (rate 5/s allows ~25)")

# c6.2 -- the leftmost X-Forwarded-For entry is whatever the client sent
lim = c6_ratelimit.Limiter()
codes = [c6_ratelimit.handle(lim, {"X-Forwarded-For": f"10.0.0.{i}, 203.0.113.7"}, "192.168.1.1")[0]
         for i in range(50)]
report("c6.2", codes.count(200) == 50, f"one client, 50 requests with a made-up first XFF entry: {codes.count(200)} allowed (capacity 10)")

# c7.1 -- lower() is not Unicode case folding
db = c7_directory.connect()
c7_directory.add(db, "Jana Straße", "ops")
try:
    c7_directory.add(db, "JANA STRASSE", "ops")
    report("c7.1", True, "'Jana Straße' and 'JANA STRASSE' both accepted")
except c7_directory.NameTaken:
    report("c7.1", False, "second name refused")

# c7.2 -- LIKE wildcards in the search text are not escaped
c7_directory.add(db, "Ali Kaya", "it")
got = c7_directory.search(db, "%")
report("c7.2", len(got) >= 2, f"search('%') -> {got}")

# c8.1 -- formula injection: a cell starting with = is written as is
p = tmp / "x.csv"
c8_export.export([c8_export.Expense(date(2026, 11, 1), "eve", '=HYPERLINK("http://evil","Taxi")',
                                    Decimal("12.50"), "EUR")], p)
text = p.read_text(encoding="utf-8-sig")
report("c8.1", ',=HYPERLINK(' in text or ',"=HYPERLINK(' in text, "merchant typed as =HYPERLINK(...) reaches the file as a formula")

# c8.2 -- no newline="" on Windows doubles the carriage return
raw = p.read_bytes()
if sys.platform == "win32":
    report("c8.2", b"\r\r\n" in raw, f"row ending bytes: {raw[raw.index(b'currency') + 13:][:3]!r} (Excel shows a blank row after each)")
else:
    report("c8.2", True, "shown only on Windows; the export runs on Windows (see ANSWER_KEY.md)")

import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)
missing = [k for k, v in shown.items() if not v]
print(f"{sum(shown.values())} of {len(shown)} defects shown" + (f"; NOT shown: {missing}" if missing else ""))
sys.exit(1 if missing or len(shown) != 16 else 0)
