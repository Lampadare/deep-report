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
