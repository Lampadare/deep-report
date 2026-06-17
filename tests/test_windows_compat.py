"""Regression tests for the 0.1.8 Windows-survivability fixes.

Most behaviors can be exercised on POSIX via monkeypatching sys.platform,
shutil.which, and module-level platform flags.
"""

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if str(REPO_SRC) not in sys.path:
    sys.path.insert(0, str(REPO_SRC))

from deep_report.orchestrator import setup_wizard
from deep_report.orchestrator.utils import agents as agents_mod
from deep_report.orchestrator.utils import keyboard as keyboard_mod


# ────────────────────────────────────────────────────────────────────────
# _resolve_claude_binary
# ────────────────────────────────────────────────────────────────────────

def test_resolve_claude_binary_returns_string(monkeypatch):
    monkeypatch.setattr(
        agents_mod.shutil, "which",
        lambda name: "/usr/local/bin/claude" if name == "claude" else None,
    )
    assert agents_mod._resolve_claude_binary() == "/usr/local/bin/claude"

    monkeypatch.setattr(agents_mod.shutil, "which", lambda name: None)
    # When nothing resolves, the function falls back to the bare "claude" string
    # so subprocess can produce a clear error.
    assert agents_mod._resolve_claude_binary() == "claude"


def test_resolve_claude_binary_prefers_cmd_on_windows(monkeypatch):
    monkeypatch.setattr(agents_mod.sys, "platform", "win32")

    def fake_which(name):
        if name == "claude":
            return None
        if name == "claude.cmd":
            return "/path/claude.cmd"
        return None

    monkeypatch.setattr(agents_mod.shutil, "which", fake_which)
    assert agents_mod._resolve_claude_binary() == "/path/claude.cmd"


# ────────────────────────────────────────────────────────────────────────
# _popen_new_process_group_kwargs
# ────────────────────────────────────────────────────────────────────────

def test_popen_new_process_group_kwargs_posix(monkeypatch):
    monkeypatch.setattr(agents_mod, "_IS_WINDOWS", False)
    kwargs = agents_mod._popen_new_process_group_kwargs()
    assert kwargs.get("start_new_session") is True
    assert "creationflags" not in kwargs


def test_popen_new_process_group_kwargs_windows(monkeypatch):
    monkeypatch.setattr(agents_mod, "_IS_WINDOWS", True)
    # subprocess.CREATE_NEW_PROCESS_GROUP only exists on Windows. Stub it on
    # POSIX so we can exercise the Windows code path here.
    sentinel = 0x00000200
    monkeypatch.setattr(
        agents_mod.subprocess, "CREATE_NEW_PROCESS_GROUP", sentinel, raising=False,
    )
    kwargs = agents_mod._popen_new_process_group_kwargs()
    assert kwargs.get("creationflags") == sentinel
    assert "start_new_session" not in kwargs


# ────────────────────────────────────────────────────────────────────────
# _terminate_process_group
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX killpg path")
def test_terminate_process_group_handles_attribute_error(monkeypatch):
    """A proc whose pid is invalid (or already dead) must not raise."""
    monkeypatch.setattr(agents_mod, "_IS_WINDOWS", False)

    class BadProc:
        pid = -1  # invalid pid → os.getpgid raises

        def kill(self):
            # Simulate the fallback also failing — must still not propagate.
            raise OSError("already gone")

    # Must not raise.
    agents_mod._terminate_process_group(BadProc())


# ────────────────────────────────────────────────────────────────────────
# setup_wizard — UnicodeDecodeError tolerance
# ────────────────────────────────────────────────────────────────────────

def _patch_home_and_cwd(monkeypatch, tmp_path: Path) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    fake_cwd = tmp_path / "cwd"
    fake_cwd.mkdir()
    monkeypatch.setattr(setup_wizard.Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(setup_wizard.Path, "cwd", classmethod(lambda cls: fake_cwd))
    return fake_home


def test_setup_wizard_handles_unicodedecodeerror(monkeypatch, tmp_path):
    """Bytes that fail strict cp1252 but are valid UTF-8 must not crash discovery.

    With errors='replace' on the read, even genuinely-undecodable bytes never
    raise — the file is just treated as unreadable JSON and discovery yields {}.
    """
    fake_home = _patch_home_and_cwd(monkeypatch, tmp_path)
    # Write UTF-8 bytes containing a non-ASCII character. On Windows under the
    # cp1252 default locale this used to crash; with the fix it's decoded as
    # UTF-8 and parsed normally.
    raw = '{"projects": {"x": "é"}}'.encode("utf-8")
    (fake_home / ".claude.json").write_bytes(raw)

    # Must not raise.
    result = setup_wizard.discover_cc_servers()
    assert isinstance(result, dict)


def test_setup_wizard_handles_invalid_bytes(monkeypatch, tmp_path):
    """Genuinely undecodable bytes must not propagate a UnicodeDecodeError."""
    fake_home = _patch_home_and_cwd(monkeypatch, tmp_path)
    # \xff is not valid in UTF-8.
    (fake_home / ".claude.json").write_bytes(b"\xff\xfe\x00not-json")
    monkeypatch.setattr(setup_wizard.ui, "warning", lambda msg: None)

    # Must not raise.
    result = setup_wizard.discover_cc_servers()
    assert isinstance(result, dict)


def test_load_keys_env_handles_unicodedecodeerror(monkeypatch, tmp_path):
    """A keys.env with bytes that fail strict cp1252 must not crash load."""
    path = tmp_path / "keys.env"
    # Invalid UTF-8 bytes — would raise UnicodeDecodeError on a strict read.
    path.write_bytes(b"NAME=\xff\xfe value\n")
    monkeypatch.setattr(setup_wizard, "KEYS_ENV_PATH", path)
    monkeypatch.setattr(setup_wizard.ui, "warning", lambda msg: None)
    monkeypatch.delenv("NAME", raising=False)

    # Must not raise. With errors='replace' the bytes become replacement
    # characters and the line may still parse; either way the call returns
    # without propagating an exception.
    setup_wizard.load_keys_env()


# ────────────────────────────────────────────────────────────────────────
# KeyboardListener — Windows path
# ────────────────────────────────────────────────────────────────────────

def test_keyboard_listener_windows_path_available_when_stdin_tty(monkeypatch):
    """On Windows with a real tty and msvcrt importable, available must be True."""
    monkeypatch.setattr(keyboard_mod, "_IS_WINDOWS", True)
    fake_msvcrt = types.ModuleType("msvcrt")
    fake_msvcrt.kbhit = lambda: False
    fake_msvcrt.getwch = lambda: ""
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)

    listener = keyboard_mod.KeyboardListener(on_key=lambda ch: None)
    assert listener.available is True
