"""load_keys_env / persist_keys_to_env_file / prompt_for_missing_keys.

Every test redirects ``setup_wizard.KEYS_ENV_PATH`` into ``tmp_path`` so the
real ``~/.deep-report/keys.env`` is never touched.
"""

import os
import sys
from pathlib import Path

import pytest

REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if str(REPO_SRC) not in sys.path:
    sys.path.insert(0, str(REPO_SRC))

from deep_report.orchestrator import setup_wizard


@pytest.fixture
def keys_path(tmp_path, monkeypatch):
    path = tmp_path / "keys.env"
    monkeypatch.setattr(setup_wizard, "KEYS_ENV_PATH", path)
    return path


# ────────────────────────────────────────────────────────────────────────
# persist_keys_to_env_file
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_persist_keys_writes_0o600(keys_path):
    setup_wizard.persist_keys_to_env_file({"A": "1"})
    assert keys_path.exists()
    assert keys_path.stat().st_mode & 0o777 == 0o600


def test_persist_keys_creates_parent_dir(tmp_path, monkeypatch):
    parent = tmp_path / "nested" / "deeper"
    path = parent / "keys.env"
    monkeypatch.setattr(setup_wizard, "KEYS_ENV_PATH", path)
    assert not parent.exists()

    setup_wizard.persist_keys_to_env_file({"A": "1"})
    assert path.exists()
    assert parent.is_dir()


def test_persist_keys_appends_new(keys_path):
    setup_wizard.persist_keys_to_env_file({"A": "1"})
    setup_wizard.persist_keys_to_env_file({"B": "2"})

    contents = keys_path.read_text()
    assert "A=1" in contents
    assert "B=2" in contents


def test_persist_keys_replaces_existing(keys_path):
    setup_wizard.persist_keys_to_env_file({"A": "1"})
    setup_wizard.persist_keys_to_env_file({"A": "2"})

    contents = keys_path.read_text()
    assert "A=2" in contents
    assert "A=1" not in contents


def test_persist_keys_strips_whitespace(keys_path):
    setup_wizard.persist_keys_to_env_file({"A": "  hello  "})
    contents = keys_path.read_text()
    assert "A=hello" in contents
    assert "A=  hello  " not in contents
    assert "A= hello" not in contents


def test_persist_keys_skips_empty_values(keys_path):
    setup_wizard.persist_keys_to_env_file({"A": "", "B": "v"})
    contents = keys_path.read_text()
    assert "B=v" in contents
    assert "A=" not in contents


# ────────────────────────────────────────────────────────────────────────
# load_keys_env
# ────────────────────────────────────────────────────────────────────────

def test_load_keys_env_sets_missing(keys_path, monkeypatch):
    keys_path.write_text("A=1\n")
    monkeypatch.delenv("A", raising=False)

    setup_wizard.load_keys_env()
    assert os.environ["A"] == "1"


def test_load_keys_env_does_not_override(keys_path, monkeypatch):
    monkeypatch.setenv("A", "existing")
    keys_path.write_text("A=new\n")

    setup_wizard.load_keys_env()
    assert os.environ["A"] == "existing"


def test_load_keys_env_silent_on_missing_file(tmp_path, monkeypatch):
    path = tmp_path / "does_not_exist.env"
    monkeypatch.setattr(setup_wizard, "KEYS_ENV_PATH", path)
    assert not path.exists()

    # Must not raise.
    setup_wizard.load_keys_env()


def test_load_keys_env_tolerates_malformed_lines(keys_path, monkeypatch):
    keys_path.write_text("no_equals_sign\nVALID_KEY=valid_value\n")
    monkeypatch.delenv("VALID_KEY", raising=False)
    monkeypatch.setattr(setup_wizard.ui, "warning", lambda msg: None)

    # Must not raise even though one line is malformed.
    setup_wizard.load_keys_env()
    assert os.environ["VALID_KEY"] == "valid_value"


# ────────────────────────────────────────────────────────────────────────
# prompt_for_missing_keys
# ────────────────────────────────────────────────────────────────────────

def test_prompt_skips_in_non_tty(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)

    assert setup_wizard.prompt_for_missing_keys({"brave-search"}) == {}


def test_prompt_collects_values(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)

    # Make sure every catalog spec's required vars look "missing" so the
    # prompt loop actually asks for them.
    for spec in setup_wizard.CATALOG:
        for var in spec.required_env:
            monkeypatch.delenv(var, raising=False)

    # Pick a spec with at least one required_env var as our enabled set.
    target = next(s for s in setup_wizard.CATALOG if s.required_env)
    enabled = {target.key}
    expected_vars = list(target.required_env)

    canned = {var: f"value-for-{var}" for var in expected_vars}
    # Also include a blank answer for the *first* var to confirm empties are
    # dropped — but only when there are 2+ vars so we still get at least one
    # collected value to assert against.
    if len(expected_vars) >= 2:
        canned[expected_vars[0]] = ""

    class _Prompt:
        def __init__(self, value):
            self._value = value

        def ask(self):
            return self._value

    def _fake_text(message, *args, **kwargs):
        # Recover the var name from the prompt — load_keys_env-style prompts
        # embed "Paste <VAR>" in the message.
        for var in canned:
            if f"Paste {var}" in message:
                return _Prompt(canned[var])
        return _Prompt("")

    import questionary
    monkeypatch.setattr(questionary, "text", _fake_text)

    result = setup_wizard.prompt_for_missing_keys(enabled)

    # Every non-empty canned answer should appear; empty ones should not.
    for var, val in canned.items():
        if val:
            assert result.get(var) == val
        else:
            assert var not in result
