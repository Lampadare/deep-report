"""Argparse validation for --machine mode.

These tests exercise the CLI as a subprocess so we cover the real argparse path,
including stderr messages and exit codes. They MUST NOT trigger a full run —
that requires the `claude` CLI and spawns subprocesses.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_SRC = Path(__file__).resolve().parents[1] / "src"


def _run(*args, timeout=10):
    """Run the CLI with the worktree's src on PYTHONPATH."""
    env = {**os.environ, "PYTHONPATH": str(REPO_SRC)}
    return subprocess.run(
        [sys.executable, "-m", "deep_report", *args],
        capture_output=True, text=True, timeout=timeout, env=env,
    )


def test_machine_without_topic_exits_2():
    result = _run("--machine")
    assert result.returncode == 2
    assert "--machine requires a topic" in result.stderr


def test_machine_long_topic_without_name_exits_2():
    long_topic = "x" * 150
    result = _run("--machine", long_topic)
    assert result.returncode == 2
    assert "--name" in result.stderr
    assert "100 chars" in result.stderr


def test_help_lists_machine_flag():
    result = _run("--help")
    assert result.returncode == 0
    assert "--machine" in result.stdout
    assert "--name" in result.stdout


def test_version_works():
    result = _run("--version")
    assert result.returncode == 0
    assert "deep-report" in result.stdout


def test_machine_with_list_exits_2():
    result = _run("--machine", "--list")
    assert result.returncode == 2
    assert "--list" in result.stderr


def test_machine_with_delete_exits_2():
    result = _run("--machine", "--delete")
    assert result.returncode == 2
    assert "--delete" in result.stderr


def test_machine_skips_interview():
    """--machine must NEVER call run_configure_interview. Patching it to raise
    proves the machine-mode branch short-circuits before reaching it."""
    import sys as _sys
    src = str(REPO_SRC)
    if src not in _sys.path:
        _sys.path.insert(0, src)
    from unittest.mock import patch

    # The interview lives in deep_report.orchestrator.main.run_configure_interview.
    # Patch it to raise — if --machine accidentally takes the default-interview
    # branch, the test will see this exception bubble up.
    with patch("deep_report.orchestrator.main.run_configure_interview",
               side_effect=AssertionError("interview must not run in --machine mode")):
        # Argparse-only smoke: bare --machine with no topic must exit 2 (usage)
        # via the machine-mode validation block, NOT by hitting run_configure_interview.
        from deep_report.orchestrator import main as _main
        with patch.object(_sys, "argv", ["deep-report", "--machine"]):
            rc = _main.main()
        assert rc == 2  # caught by machine-mode validation, interview never reached
