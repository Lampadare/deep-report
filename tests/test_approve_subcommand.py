"""`deep-report --approve` writes a response to pending_approval.json."""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_SRC = Path(__file__).resolve().parents[1] / "src"


def _run(*args, timeout=10):
    env = {**os.environ, "PYTHONPATH": str(REPO_SRC)}
    return subprocess.run(
        [sys.executable, "-m", "deep_report", *args],
        capture_output=True, text=True, timeout=timeout, env=env,
    )


def _write_pending(report_dir: Path, gate_id: str):
    state = report_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "pending_approval.json").write_text(json.dumps({
        "gate_id": gate_id,
        "metadata": {"hello": "world"},
        "status": "pending",
        "requested_at": "2026-05-22T00:00:00",
    }))


def test_approve_writes_response(tmp_path):
    _write_pending(tmp_path, "pre_research")
    result = _run("--approve", "--report-dir", str(tmp_path),
                  "--gate", "pre_research", "--decision", "approve")
    assert result.returncode == 0, result.stderr

    data = json.loads((tmp_path / "state" / "pending_approval.json").read_text())
    assert data["response"]["decision"] == "approve"
    assert data["status"] == "responded"


def test_approve_with_feedback(tmp_path):
    _write_pending(tmp_path, "pre_research")
    result = _run("--approve", "--report-dir", str(tmp_path),
                  "--gate", "pre_research", "--decision", "reject",
                  "--feedback", "needs more depth")
    assert result.returncode == 0
    data = json.loads((tmp_path / "state" / "pending_approval.json").read_text())
    assert data["response"]["decision"] == "reject"
    assert data["response"]["feedback"] == "needs more depth"


def test_approve_gate_mismatch_exits_2(tmp_path):
    _write_pending(tmp_path, "pre_research")
    result = _run("--approve", "--report-dir", str(tmp_path),
                  "--gate", "wrong_gate", "--decision", "approve")
    assert result.returncode == 2
    assert "gate mismatch" in result.stderr


def test_approve_missing_file_exits_2(tmp_path):
    result = _run("--approve", "--report-dir", str(tmp_path),
                  "--gate", "pre_research", "--decision", "approve")
    assert result.returncode == 2
    assert "no pending approval" in result.stderr


def test_approve_missing_flags_exits_2():
    result = _run("--approve", "--decision", "approve")
    assert result.returncode == 2
    assert "requires" in result.stderr
