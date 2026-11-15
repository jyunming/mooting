"""Registering a seat's MCP server with the CLIs that cannot take it per run.

`conftest.py` stubs `install_seat` for every test, because the real one shells
out and once wrote into a developer's global codex config. These tests undo
that stub and replace the two things that reach the machine -- finding the
binary and running it -- so the real logic runs and nothing is registered.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from mooting.store import connect


@pytest.fixture()
def real_install(monkeypatch):
    monkeypatch.undo()                     # drop conftest's stub for this test
    import mooting.install as install
    ran = []

    def fake_run(argv, **kw):
        ran.append(argv)
        return subprocess.CompletedProcess(argv, install._next_code, "", install._next_err)

    monkeypatch.setattr(install.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(install.subprocess, "run", fake_run)
    monkeypatch.setattr(install, "_next_code", 0, raising=False)
    monkeypatch.setattr(install, "_next_err", "", raising=False)
    install.ran = ran
    yield install


@pytest.fixture()
def board(tmp_path):
    s = connect(tmp_path / "board.db", init=True)
    s.add_agent("jeremy", "human")
    s.add_agent("Kevin", "agy", driver="spawn")
    s.add_agent("Algae", "codex", driver="spawn")
    s.add_agent("Santa", "claude", driver="spawn")
    yield s
    s.close()


def test_the_server_is_registered_under_the_seats_own_name(real_install, board):
    """Bound by name, because the name in the server's argv is who it posts as.
    One registration per kind is how a seat named Gravity posted as `agy`."""
    for kind, agent in (("codex", "Algae"), ("gemini", "Gem"), ("agy", "Kevin")):
        cmd = real_install.install_cmd(kind, agent, board.path)
        assert f"mooting-{agent}" in cmd
        assert cmd[cmd.index("--agent") + 1] == agent
    assert real_install.install_cmd("claude", "Santa", board.path) is None


def test_codex_gets_forward_slashes(real_install, board):
    """codex parses the value as TOML, where `\\d` in a Windows path is an
    invalid escape and the args array arrives as a string."""
    cmd = real_install.install_cmd("codex", "Algae", r"C:\dev\board.db")
    assert not any("\\" in part for part in cmd[cmd.index("--"):])


def test_a_per_run_cli_is_left_alone(real_install, board):
    ok, detail = real_install.install_seat(board, "Santa")
    assert ok and "per-run" in detail
    assert real_install.ran == [], "registering claude globally is a side effect nobody asked for"


def test_dry_run_prints_and_runs_nothing(real_install, board):
    ok, detail = real_install.install_seat(board, "Kevin", dry_run=True)
    assert ok and detail.startswith("agy mcp add mooting-Kevin")
    assert real_install.ran == []


def test_registration_runs_the_resolved_binary(real_install, board):
    ok, detail = real_install.install_seat(board, "Kevin")
    assert ok and detail == "registered as mooting-Kevin"
    assert real_install.ran[0][0] == "/bin/agy"


def test_a_refused_registration_reports_the_cli_s_own_words(real_install, board, monkeypatch):
    monkeypatch.setattr(real_install, "_next_code", 1)
    monkeypatch.setattr(real_install, "_next_err", "server mooting-Algae already exists\n")
    ok, detail = real_install.install_seat(board, "Algae")
    assert not ok and detail == "server mooting-Algae already exists"


def test_a_missing_cli_is_reported_not_raised(real_install, board, monkeypatch):
    monkeypatch.setattr(real_install.shutil, "which", lambda name: None)
    ok, detail = real_install.install_seat(board, "Algae")
    assert not ok and detail == "codex not on PATH"


def test_uninstall_names_the_same_server(real_install):
    assert real_install.uninstall_cmd("agy", "Kevin") == ["agy", "mcp", "remove", "mooting-Kevin"]
    assert real_install.uninstall_cmd("claude", "Santa") is None


def test_the_server_command_is_this_interpreter(real_install, board):
    argv = real_install.server_argv("Kevin", board.path)
    assert argv[0] == sys.executable.replace("\\", "/")
    assert argv[argv.index("-m") + 1] == "mooting.mcp_server"
