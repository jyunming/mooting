"""Show that each seed has its defect. Run before the experiment, spends nothing.

Usage: python -X utf8 experiments/g4/check_seeds.py
"""
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "seeds"))

import s1_page, s2_safe_join, s3_retry, s4_tokens, s5_stats  # noqa: E402

rows = list(range(30))
got = s1_page.page(rows, 1, 10)
print("s1 page 1 of 0..29 by 10:", got[:3], "... expected [0, 1, 2]", "DEFECT" if got[:1] != [0] else "ok")

with tempfile.TemporaryDirectory() as d:
    (Path(d) / "www").mkdir()
    (Path(d) / "www-private").mkdir()
    (Path(d) / "www-private" / "key").write_text("secret")
    try:
        p = s2_safe_join.safe_join(str(Path(d) / "www"), "../www-private/key")
        print("s2 ../www-private/key accepted:", Path(p).name, "DEFECT")
    except PermissionError:
        print("s2 refused: ok")


def down():
    raise ConnectionError("down")


r = s3_retry.retry(down, attempts=2, base_delay=0)
print("s3 every attempt fails, retry returned:", repr(r), "DEFECT" if not callable(r) else "")

t = s4_tokens.issue("u")
print("s4 valid right after issue:", s4_tokens.is_valid(t),
      "(machine UTC offset", __import__("time").strftime("%z") + ")",
      "DEFECT shows when offset >= +0100 (False) or < 0 (lives longer)")

sys.setswitchinterval(1e-6)
st = s5_stats.Stats()
real_get = st._counts.get


def slow_get(*a):
    v = real_get(*a)
    __import__("time").sleep(0)          # yield between the read and the write
    return v


st._counts = type("D", (dict,), {"get": lambda self, *a: slow_get(*a)})(st._counts)
real_get = dict.get.__get__(st._counts)
ths = [threading.Thread(target=lambda: [st.incr("n") for _ in range(2000)]) for _ in range(8)]
[t.start() for t in ths]
[t.join() for t in ths]
print("s5 8 threads x 2000 incr:", st.get("n"), "expected 16000", "DEFECT" if st.get("n") != 16000 else "ok")
