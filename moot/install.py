"""Register the Moot MCP server with the CLIs that cannot take it per-run.

Claude and Copilot accept `--mcp-config` / `--additional-mcp-config` on every
invocation, so a council seat never touches their global config. Codex and Gemini
do not:

* **Gemini** has no per-run injection at all -- servers come from settings.json.
* **Codex** has an `mcp_servers` table and accepts `-c` overrides, but a server
  introduced *only* by `-c` is not launched; it has to exist in the config.

So those two get a one-time `mcp add`. The server is registered per seat name
(`moot-<seat>`), not once globally, because the agent identity is bound in the
server's argv -- that is what stops one CLI from posting as another. Two codex
seats therefore need two registrations, which is correct rather than awkward.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from .store import Store

#: Claude and Copilot are absent on purpose -- injecting per-run is strictly
#: better, and registering them globally would be a side effect nobody asked for.
NEEDS_REGISTRATION = {"codex", "gemini", "agy"}


def server_argv(agent: str, db: Path | str) -> list[str]:
    # Forward slashes: codex parses `-c` values as TOML and mangles backslashes.
    return [sys.executable.replace("\\", "/"), "-X", "utf8", "-m", "moot.mcp_server",
            "--agent", agent, "--db", str(db).replace("\\", "/")]


def install_cmd(kind: str, agent: str, db: Path | str) -> list[str] | None:
    name = f"moot-{agent}"
    argv = server_argv(agent, db)
    if kind == "codex":
        return ["codex", "mcp", "add", name, "--", *argv]
    if kind == "gemini":
        return ["gemini", "mcp", "add", name, *argv,
                "--scope", "user", "--trust", "--description", f"Moot council seat {agent}"]
    if kind == "agy":
        return ["agy", "mcp", "add", name, *argv]
    return None


def install_seat(store: Store, agent: str, *, dry_run: bool = False) -> tuple[bool, str]:
    meta = store.agent(agent)
    kind = meta["kind"]
    if kind not in NEEDS_REGISTRATION:
        return True, f"{kind} takes the server per-run; nothing to install"

    cmd = install_cmd(kind, agent, store.path)
    assert cmd is not None
    resolved = shutil.which(cmd[0])
    if resolved is None:
        return False, f"{cmd[0]} not on PATH"
    if dry_run:
        return True, " ".join(cmd)

    proc = subprocess.run([resolved, *cmd[1:]], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()[:300]
    return True, f"registered as moot-{agent}"


def uninstall_cmd(kind: str, agent: str) -> list[str] | None:
    name = f"moot-{agent}"
    if kind in {"codex", "gemini", "agy"}:
        return [kind, "mcp", "remove", name]
    return None
