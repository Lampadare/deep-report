"""ApprovalGate in 'file' mode writes the request and polls for a response."""

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deep_report.orchestrator.approval import ApprovalGate
from deep_report.orchestrator.progress import ProgressWriter


def _make_gate(tmp_path: Path, mode: str = "file") -> ApprovalGate:
    progress = ProgressWriter(tmp_path)
    return ApprovalGate(tmp_path, interactive=True, progress=progress, approval_mode=mode)


def test_auto_mode_short_circuits(tmp_path):
    gate = _make_gate(tmp_path, mode="auto")
    assert gate.request_approval("pre_research", {"x": 1}) is True
    # auto mode does NOT write a request file
    assert not (tmp_path / "state" / "pending_approval.json").exists()


def test_file_mode_writes_request_then_polls(tmp_path):
    gate = _make_gate(tmp_path, mode="file")

    # Drive a response in from a background thread
    def respond_later():
        time.sleep(0.5)
        approval_file = tmp_path / "state" / "pending_approval.json"
        data = json.loads(approval_file.read_text())
        data["response"] = {"decision": "approve", "feedback": ""}
        approval_file.write_text(json.dumps(data))

    threading.Thread(target=respond_later, daemon=True).start()

    result = gate._wait_for_file_response(
        "pre_research", {"agents": 10}, "proceed_or_quit",
        allow_feedback=False, poll_secs=0.1, timeout_secs=5,
    )
    assert result is True

    final = json.loads((tmp_path / "state" / "pending_approval.json").read_text())
    assert final["status"] == "resolved"


def test_file_mode_reject_returns_false(tmp_path):
    gate = _make_gate(tmp_path, mode="file")

    def respond_later():
        time.sleep(0.3)
        approval_file = tmp_path / "state" / "pending_approval.json"
        data = json.loads(approval_file.read_text())
        data["response"] = {"decision": "stop_early", "feedback": ""}
        approval_file.write_text(json.dumps(data))

    threading.Thread(target=respond_later, daemon=True).start()

    result = gate._wait_for_file_response(
        "iter_1", {}, "proceed_stop_quit",
        allow_feedback=False, poll_secs=0.1, timeout_secs=5,
    )
    assert result is False


def test_file_mode_timeout(tmp_path):
    gate = _make_gate(tmp_path, mode="file")
    result = gate._wait_for_file_response(
        "pre_research", {}, "proceed_or_quit",
        allow_feedback=False, poll_secs=0.05, timeout_secs=0.2,
    )
    assert result is False
    # Timeout must emit a distinct approval_timeout event so drivers can
    # tell timeout from rejection.
    events = [json.loads(line) for line in
              (tmp_path / "state" / "progress.jsonl").read_text().splitlines()]
    assert any(e["type"] == "approval_timeout" for e in events)


def test_file_mode_stale_response_from_prior_gate_is_ignored(tmp_path):
    """A response left over with status=resolved from an earlier gate must
    not be silently consumed by a new request."""
    gate = _make_gate(tmp_path, mode="file")

    # Pre-seed a "resolved" file as if a prior gate finished
    approval_file = tmp_path / "state" / "pending_approval.json"
    approval_file.parent.mkdir(parents=True, exist_ok=True)
    approval_file.write_text(json.dumps({
        "gate_id": "old_gate",
        "request_seq": 99,
        "status": "resolved",
        "response": {"decision": "approve", "feedback": ""},
    }))

    # New gate must NOT consume the stale response — it should time out quickly
    result = gate._wait_for_file_response(
        "new_gate", {}, "proceed_or_quit",
        allow_feedback=False, poll_secs=0.05, timeout_secs=0.3,
    )
    assert result is False  # timeout, not stale approval


def test_file_mode_request_write_failure_returns_false(tmp_path, monkeypatch):
    """When the request file cannot be written, fail closed (don't auto-approve)."""
    gate = _make_gate(tmp_path, mode="file")

    def boom(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(gate, "_atomic_write_json", boom)

    result = gate._wait_for_file_response(
        "pre_research", {}, "proceed_or_quit",
        allow_feedback=False, poll_secs=0.05, timeout_secs=0.5,
    )
    assert result is False  # fail closed, NOT True
