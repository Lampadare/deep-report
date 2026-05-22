"""ProgressWriter emits the right NDJSON shape for events agents tail."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deep_report.orchestrator.progress import ProgressWriter


def _events(progress_file: Path):
    return [json.loads(line) for line in progress_file.read_text().splitlines() if line.strip()]


def test_phase_start_complete_shape(tmp_path):
    pw = ProgressWriter(tmp_path)
    pw.phase_start(1, "Setup")
    pw.phase_complete(1, "Setup")

    events = _events(tmp_path / "state" / "progress.jsonl")
    assert len(events) == 2
    assert events[0]["type"] == "phase_start"
    assert events[0]["phase"] == 1
    assert events[0]["name"] == "Setup"
    assert "timestamp" in events[0]
    assert "elapsed_secs" in events[0]
    assert events[1]["type"] == "phase_complete"


def test_approval_waiting_shape(tmp_path):
    pw = ProgressWriter(tmp_path)
    pw.approval_waiting("pre_research")
    events = _events(tmp_path / "state" / "progress.jsonl")
    assert events[0]["type"] == "approval_waiting"
    assert events[0]["gate_id"] == "pre_research"


def test_report_ready_shape(tmp_path):
    pw = ProgressWriter(tmp_path)
    pw.report_ready("/tmp/r/report.md", "/tmp/r/SUMMARY.md", exit_code=0)
    events = _events(tmp_path / "state" / "progress.jsonl")
    assert events[0]["type"] == "report_ready"
    assert events[0]["report_path"] == "/tmp/r/report.md"
    assert events[0]["summary_path"] == "/tmp/r/SUMMARY.md"
    assert events[0]["exit_code"] == 0
