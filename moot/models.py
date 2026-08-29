"""What models a seat could run.

Only one of these CLIs will tell you: `agy models` prints an id and a label per
line. The rest take `--model <name>` and offer no way to enumerate, so the honest
design is a short list of known names *plus* somewhere to type one — a picker that
only ever offered a guessed list would be wrong the week a model shipped.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys

#: Names worth offering when the CLI cannot be asked. Deliberately short: these
#: are a convenience, not a claim about what exists.
KNOWN: dict[str, tuple[str, ...]] = {
    "claude": ("opus", "sonnet", "haiku"),
    "codex": ("gpt-5.6-sol", "gpt-5.6-sol-mini"),
    "copilot": ("auto", "claude-sonnet-4.5", "gpt-5"),
    "gemini": ("gemini-3.1-pro", "gemini-3.7-flash"),
    "agy": (),          # asked directly; see LISTERS
}

#: CLIs that can enumerate their own models, and how.
LISTERS: dict[str, list[str]] = {
    "agy": ["models"],
}


def _parse(out: str) -> list[str]:
    """First column of each line: `id<TAB>Human Label`."""
    names: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.lower().startswith(("fetching", "error", "usage")):
            continue
        name = line.split("\t")[0].split()[0].strip()
        if name and name not in names:
            names.append(name)
    return names


async def available(kind: str, timeout: float = 25.0) -> list[str]:
    """Models for this CLI: asked where possible, known names otherwise.

    Never raises. A picker that fails because a subprocess misbehaved is worse
    than one showing a shorter list.
    """
    argv = LISTERS.get(kind)
    binary = shutil.which(kind)
    if argv and binary:
        try:
            extra = {}
            if sys.platform == "win32":
                extra["creationflags"] = subprocess.CREATE_NO_WINDOW
            proc = await asyncio.create_subprocess_exec(
                binary, *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "PYTHONUTF8": "1"},
                **extra,
            )
            out, _err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            found = _parse(out.decode("utf-8", errors="replace"))
            if found:
                return found
        except Exception:
            pass
    return list(KNOWN.get(kind, ()))
