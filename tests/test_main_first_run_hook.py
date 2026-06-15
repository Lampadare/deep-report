"""First-run wizard guard in main.main() must fail-closed and be skipped for
non-interactive / non-MCP paths.

The guard lives inline in `main()` (search for "First-run onboarding"). These
tests drive the real entry point with mocked argv + heavy collaborators so we
exercise the actual conditional, not a re-implementation.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if str(REPO_SRC) not in sys.path:
    sys.path.insert(0, str(REPO_SRC))

from deep_report.orchestrator import main as main_mod
from deep_report.orchestrator import setup_wizard


def _redirect_config(tmp_path, monkeypatch):
    """Point setup_wizard at a throwaway CONFIG_PATH under tmp_path."""
    cfg = tmp_path / ".deep-report" / "mcp_config.json"
    monkeypatch.setattr(setup_wizard, "CONFIG_PATH", cfg)
    return cfg


def _force_tty(monkeypatch, value=True):
    """Make sys.stdin/stdout report as TTY (or not) regardless of test runner."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: value, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: value, raising=False)


def test_first_run_persists_empty_config_on_cancel(tmp_path, monkeypatch):
    """run_wizard returning non-zero must leave a valid config on disk with an
    empty enabled list — otherwise enabled_keys() returns None ("include
    everything") and the user's cancellation is silently ignored.
    """
    cfg = _redirect_config(tmp_path, monkeypatch)
    _force_tty(monkeypatch, True)
    assert not cfg.exists()

    # Wizard says "user cancelled".
    monkeypatch.setattr(setup_wizard, "run_wizard", lambda: 1)
    # Short-circuit the rest of main() so the test doesn't try to start a real run.
    monkeypatch.setattr(main_mod, "run_configure_interview", lambda *a, **kw: None)

    with patch.object(sys, "argv", ["deep-report", "some topic"]):
        rc = main_mod.main()

    # main() returns 1 because run_configure_interview returned None — but that's
    # after the guard. What we care about: the fail-closed save happened.
    assert rc == 1
    assert cfg.exists(), "fail-closed save_config must persist an empty config"
    data = json.loads(cfg.read_text())
    assert data.get("enabled") == [], f"enabled should be empty list, got {data.get('enabled')!r}"
    # `imported=None` ⇒ key omitted (save_config only writes it when truthy)
    assert "imported" not in data or not data["imported"]


def test_first_run_skipped_on_list(tmp_path, monkeypatch):
    """`--list` must NOT trigger the wizard even when no config exists — the
    list/resume picker doesn't need MCPs.
    """
    cfg = _redirect_config(tmp_path, monkeypatch)
    _force_tty(monkeypatch, True)
    assert not cfg.exists()

    wizard_calls = []
    monkeypatch.setattr(setup_wizard, "run_wizard",
                        lambda: wizard_calls.append("called") or 0)
    # Stop at list_and_resume so we don't poke the real registry.
    monkeypatch.setattr(main_mod, "list_and_resume", lambda ctx: 0)

    with patch.object(sys, "argv", ["deep-report", "--list"]):
        rc = main_mod.main()

    assert rc == 0
    assert wizard_calls == [], "wizard must not run on --list"
    assert not cfg.exists(), "no config should be written on --list"


def test_first_run_skipped_on_machine(tmp_path, monkeypatch):
    """`--machine` must NOT trigger the wizard — agent drivers can't answer
    prompts, and machine mode uses env-var-only discovery.
    """
    cfg = _redirect_config(tmp_path, monkeypatch)
    # TTY status shouldn't matter for --machine, but set it true to prove the
    # guard's `not args.machine` check is what's gating, not the TTY check.
    _force_tty(monkeypatch, True)
    assert not cfg.exists()

    wizard_calls = []
    monkeypatch.setattr(setup_wizard, "run_wizard",
                        lambda: wizard_calls.append("called") or 0)
    monkeypatch.setattr(main_mod, "run_new_report", lambda config, ctx: 0)

    with patch.object(sys, "argv", ["deep-report", "--machine", "topic"]):
        rc = main_mod.main()

    assert rc == 0
    assert wizard_calls == [], "wizard must not run in --machine mode"
    assert not cfg.exists(), "no config should be written in --machine mode"


def test_first_run_skipped_when_config_exists(tmp_path, monkeypatch):
    """If CONFIG_PATH already exists, the wizard must not run again."""
    cfg = _redirect_config(tmp_path, monkeypatch)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"version": 2, "enabled": ["brave-search"]}))
    _force_tty(monkeypatch, True)

    wizard_calls = []
    monkeypatch.setattr(setup_wizard, "run_wizard",
                        lambda: wizard_calls.append("called") or 0)
    monkeypatch.setattr(main_mod, "run_configure_interview", lambda *a, **kw: None)

    with patch.object(sys, "argv", ["deep-report", "some topic"]):
        rc = main_mod.main()

    assert rc == 1  # interview returned None
    assert wizard_calls == [], "wizard must not run when config already exists"
    # Existing config must be untouched.
    data = json.loads(cfg.read_text())
    assert data.get("enabled") == ["brave-search"]


def test_first_run_wrapped_try_except(tmp_path, monkeypatch):
    """If run_wizard raises, the guard must catch it, warn, and let the report
    continue (treating it as a cancellation → fail-closed empty config).
    """
    cfg = _redirect_config(tmp_path, monkeypatch)
    _force_tty(monkeypatch, True)
    assert not cfg.exists()

    def boom():
        raise RuntimeError("wizard exploded")

    monkeypatch.setattr(setup_wizard, "run_wizard", boom)
    # Capture that the report path is reached after the exception is swallowed.
    interview_calls = []

    def fake_interview(*args, **kwargs):
        interview_calls.append(args)
        return None  # cancel interview so main returns 1 without spawning a run

    monkeypatch.setattr(main_mod, "run_configure_interview", fake_interview)

    with patch.object(sys, "argv", ["deep-report", "some topic"]):
        rc = main_mod.main()

    # Exception was caught (no traceback bubbled up). Report flow continued and
    # the interview was reached → guard didn't abort the whole CLI.
    assert rc == 1
    assert interview_calls, "execution must continue past the wizard exception"
    # Fail-closed save still happened (rc=1 branch persists empty config).
    assert cfg.exists(), "exception path should still fail-closed via save_config"
    data = json.loads(cfg.read_text())
    assert data.get("enabled") == []
