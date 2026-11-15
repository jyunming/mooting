"""Cut set R out of git history: the code as it stood just before each real fix.

Usage: python -X utf8 experiments/g4b/build_real.py
Writes experiments/g4b/real/r<N>.py. Every line is verbatim from `<fix>^`; the
only additions are a header naming the file and a `# ...` line where code was
left out. The fix commit is NOT written into the excerpt -- it is the answer.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE.parent.parent

#: (seed, fix commit, [(file, first line, last line), ...])
SEEDS = [
    ("r1", "a35d5af", [("mooting/mcp_server.py", 1, 300)]),
    ("r2", "f274e4a", [("mooting/web.py", 1, 73)]),
    ("r3", "c0fc652", [("mooting/telegram.py", 1035, 1135)]),
    ("r4", "153ef8c", [("mooting/console.py", 749, 781),
                       ("mooting/telegram.py", 1316, 1350),
                       ("mooting/telegram.py", 1432, 1452)]),
    ("r5", "3dd9041", [("mooting/console.py", 550, 565),
                       ("mooting/console.py", 684, 696),
                       ("mooting/console.py", 1316, 1360)]),
    ("r6", "69dd76f", [("mooting/telegram.py", 56, 139),
                       ("mooting/telegram.py", 490, 501),
                       ("mooting/telegram.py", 794, 806),
                       ("mooting/supervisor.py", 655, 678)]),
    ("r7", "61c09cf", [("mooting/drivers/base.py", 1, 31),
                       ("mooting/drivers/base.py", 170, 300)]),
    ("r8", "4f702e9", [("mooting/cli.py", 1, 40),
                       ("mooting/cli.py", 155, 235),
                       ("mooting/cli.py", 315, 340)]),
]


def lines_at(commit: str, path: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(REPO), "show", f"{commit}^:{path}"],
                         capture_output=True, check=True).stdout
    return out.decode("utf-8").splitlines()


def build() -> None:
    dest = HERE / "real"
    dest.mkdir(exist_ok=True)
    for seed, fix, parts in SEEDS:
        chunks: list[str] = []
        last_file = None
        for path, a, b in parts:
            src = lines_at(fix, path)
            b = min(b, len(src))
            if path != last_file:
                chunks.append(f"# ===== {path} =====")
                last_file = path
            if a > 1 and chunks[-1] != "# ...":
                chunks.append("# ...")
            chunks.extend(src[a - 1:b])
            if b < len(src):
                chunks.append("# ...")
        (dest / f"{seed}.py").write_text("\n".join(chunks) + "\n", encoding="utf-8")
        print(f"{seed}: {sum(1 for c in chunks if not c.startswith('# ...'))} lines")


if __name__ == "__main__":
    build()
