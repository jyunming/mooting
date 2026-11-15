"""Show that each G4b seed has its defect. Run before the experiment; spends nothing.

Usage: python -X utf8 experiments/g4b/check_seeds.py
Exit code 0 only if all eight defects are shown.
"""
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent / "seeds"))

import b1_money, b2_lru, b3_report, b4_names, b5_accounts, b6_shifts, b7_tiers, b8_hooks  # noqa: E402

shown = {}


def report(seed, defect, what):
    shown[seed] = defect
    print(f"{seed}: {'DEFECT' if defect else 'ok   '}  {what}")


got = b1_money.parse_amount("19.99")
report("b1", got != 1999, f"parse_amount('19.99') = {got}, expected 1999")

c = b2_lru.LRUCache(2)
c.put("a", 1)
c.put("b", 2)
c.get("a")                      # a is now the most recently used
c.put("c", 3)                   # so b should go
report("b2", c.get("a") is None, f"after get(a), put(c): a = {c.get('a')!r}, b = {c.get('b')!r}; expected a kept, b evicted")

import sqlite3  # noqa: E402
db = sqlite3.connect(":memory:")
db.execute("CREATE TABLE sales (region TEXT, cents INT, month TEXT)")
db.executemany("INSERT INTO sales VALUES (?,?,?)", [("north", 100, "2026-10"), ("south", 50, "2026-10")])
got = b3_report.monthly_report(db, "2026-10")
report("b3", got != [("north", 100), ("south", 50)], f"monthly_report on two rows = {got}, expected north 100, south 50")

try:
    got = b4_names.check_username("abc/../../etc")
    report("b4", True, f"check_username('abc/../../etc') accepted as {got!r}")
except ValueError:
    report("b4", False, "path-like name refused")

a, b = b5_accounts.Account("a", 10_000), b5_accounts.Account("b", 10_000)
stop = threading.Event()


def churn(src, dst):
    while not stop.is_set():
        b5_accounts.transfer(src, dst, 1)
        b5_accounts.transfer(dst, src, 1)


sys.setswitchinterval(1e-6)
ths = [threading.Thread(target=churn, args=p, daemon=True) for p in [(a, b), (b, a)] * 2]
[t.start() for t in ths]
[t.join(timeout=3) for t in ths]
stop.set()
stuck = [t for t in ths if t.is_alive()]
[t.join(timeout=1) for t in ths]
stuck = [t for t in ths if t.is_alive()]
report("b5", bool(stuck), f"{len(stuck)} of 4 transfer threads still blocked after stop (deadlock)")
sys.setswitchinterval(0.005)

start = b6_shifts.local(2026, 10, 24, 22)       # DST ends 25 Oct 03:00 -> 02:00
end = b6_shifts.local(2026, 10, 25, 6)
got = b6_shifts.hours_worked(start, end)
report("b6", got != 9.0, f"22:00 -> 06:00 across the October change = {got} h, actually worked 9 h")

got = b7_tiers.discount_for(100)
report("b7", got != 5, f"discount_for(100) = {got} %, the comment says exactly 100 gets 5 %")

seen = []
bus = b8_hooks.Bus()
plugins = [SimpleNamespace(events=["save"], config={"name": n},
                           handle=lambda ev, p, cfg, n=n: seen.append((n, cfg["name"]))) for n in ("p1", "p2")]
b8_hooks.register_plugins(bus, plugins)
bus.publish("save", {})
report("b8", seen != [("p1", "p1"), ("p2", "p2")], f"handlers called as (plugin, config) {seen}, expected p1/p1 and p2/p2")

missing = [k for k, v in shown.items() if not v]
print("all eight defects shown" if not missing else f"NOT shown: {missing}")
sys.exit(1 if missing else 0)
