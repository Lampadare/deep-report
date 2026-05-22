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


def _write_pending(report_dir: Path, gate_id: str, request_seq: int = 1,
                   status: str = "pending"):
    state = report_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    # The --approve subcommand sanity-checks that the dir looks like a report
    # by requiring manifest.json. Fake it for tests.
    (state / "manifest.json").write_text(json.dumps({"topic": "test"}))
    (state / "pending_approval.json").write_text(json.dumps({
        "gate_id": gate_id,
        "request_seq": request_seq,
        "metadata": {"hello": "world"},
        "status": status,
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
    # Fake a report dir so manifest check passes, but omit pending_approval.json
    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "state" / "manifest.json").write_text("{}")
    result = _run("--approve", "--report-dir", str(tmp_path),
                  "--gate", "pre_research", "--decision", "approve")
    assert result.returncode == 2
    assert "no pending approval" in result.stderr


def test_approve_not_a_report_dir_exits_2(tmp_path):
    # No manifest.json → reject (blocks path traversal targets)
    result = _run("--approve", "--report-dir", str(tmp_path),
                  "--gate", "pre_research", "--decision", "approve")
    assert result.returncode == 2
    assert "not a deep-report directory" in result.stderr


def test_approve_refuses_already_resolved(tmp_path):
    _write_pending(tmp_path, "pre_research", status="resolved")
    result = _run("--approve", "--report-dir", str(tmp_path),
                  "--gate", "pre_research", "--decision", "approve")
    assert result.returncode == 2
    assert "no longer pending" in result.stderr


def test_approve_handles_corrupted_json(tmp_path):
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "manifest.json").write_text("{}")
    (state / "pending_approval.json").write_text("not json {{{")
    result = _run("--approve", "--report-dir", str(tmp_path),
                  "--gate", "pre_research", "--decision", "approve")
    assert result.returncode == 2
    assert "could not parse" in result.stderr


def test_approve_missing_flags_exits_2():
    result = _run("--approve", "--decision", "approve")
    assert result.returncode == 2
    assert "requires" in result.stderr
