#!/usr/bin/env python3
"""Progress tracking for deep-report orchestrator.

Writes progress updates as JSON-lines for structured parsing.
Also supports legacy .log format for backwards compatibility.
"""

import json
import os
import time
import portalocker
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional


class ProgressWriter:
    """Writes progress updates as JSON-lines.

    Note: start_time is set at construction. If reusing a ProgressWriter
    instance across sessions, elapsed times will be relative to the original
    construction time. Create a new instance if you need fresh timing.
    """

    def __init__(self, report_dir: Path):
        self.report_dir = Path(report_dir)
        self.progress_file = self.report_dir / "state" / "progress.jsonl"
        self.legacy_file = self.report_dir / "state" / "progress.log"
        self.start_time = time.time()
        self._ensure_dir()

    def _ensure_dir(self):
        self.progress_file.parent.mkdir(parents=True, exist_ok=True)

    def _elapsed(self) -> str:
        elapsed = time.time() - self.start_time
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        return f"{mins:02d}:{secs:02d}"

    def _elapsed_secs(self) -> float:
        return time.time() - self.start_time

    def _write_event(self, event_type: str, data: dict):
        """Write a JSON-lines event durably.

        portalocker.lock is advisory on POSIX (writers cooperate) but readers like `tail -F`
        don't honor it. We build the full line in memory and do a single
        os.write inside the lock — POSIX guarantees write atomicity up to
        PIPE_BUF (typically 4 KB). Long ``summary`` payloads or large
        ``decision`` reasoning blobs can exceed this; in that case a concurrent
        ``tail -F`` reader could briefly see a partial line. Drivers consuming
        progress.jsonl should be JSONL-aware (skip lines that don't parse).

        An ``os.fsync`` after the write ensures the entry survives a crash —
        the JSONL stream is the machine-mode driver's only signal, so losing
        the tail on a crash silently breaks the driver. The cost is one
        fsync per event, which is fine for our event volumes.
        """
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "elapsed_secs": round(self._elapsed_secs(), 2),
            "type": event_type,
            **data
        }
        line = (json.dumps(event) + "\n").encode("utf-8")
        try:
            fd = os.open(self.progress_file,
                         os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        except OSError as e:
            print(f"Warning: Could not open progress file: {e}")
            return
        # fd is owned by this function from here — close unconditionally.
        try:
            try:
                portalocker.lock(fd, portalocker.LOCK_EX)
            except (OSError, portalocker.LockException) as e:
                print(f"Warning: Could not lock progress file: {e}")
                return
            try:
                os.write(fd, line)
                try:
                    os.fsync(fd)
                except OSError:
                    pass  # fsync is best-effort; tmpfs / pipes don't support it
            except OSError as e:
                print(f"Warning: Could not write progress: {e}")
            finally:
                try:
                    portalocker.unlock(fd)
                except (OSError, portalocker.LockException):
                    pass  # unlock is best-effort — close will release anyway
        finally:
            os.close(fd)

    def _write_legacy(self, line: str):
        """Write to legacy log format for backwards compatibility."""
        try:
            with open(self.legacy_file, "a", encoding='utf-8') as f:
                portalocker.lock(f.fileno(), portalocker.LOCK_EX)
                try:
                    f.write(f"[{self._elapsed()}] {line}\n")
                    f.flush()
                finally:
                    portalocker.unlock(f.fileno())
        except OSError:
            pass

    def update(self, phase: int, step: str, detail: str = ""):
        """Write a progress update."""
        self._write_event("update", {
            "phase": phase,
            "step": step,
            "detail": detail
        })
        # Legacy format
        line = f"Phase {phase} | {step}"
        if detail:
            line += f" | {detail}"
        self._write_legacy(line)

    def phase_start(self, phase: int, name: str):
        """Mark the start of a phase."""
        self._write_event("phase_start", {
            "phase": phase,
            "name": name
        })
        self._write_legacy(f"{'='*60}")
        self._write_legacy(f"Phase {phase} | STARTING: {name}")

    def phase_complete(self, phase: int, name: str):
        """Mark the completion of a phase."""
        self._write_event("phase_complete", {
            "phase": phase,
            "name": name
        })
        self._write_legacy(f"Phase {phase} | COMPLETE: {name}")
        self._write_legacy(f"{'='*60}")

    def agent_start(self, agent_id: str, total: int, current: int):
        """Mark an agent starting."""
        self._write_event("agent_start", {
            "agent_id": agent_id,
            "total": total,
            "current": current
        })
        self._write_legacy(f"Phase 3 | Agent [{current}/{total}] | {agent_id} starting...")

    def agent_complete(self, agent_id: str, success: bool, total: int, done: int,
                       duration: Optional[float] = None, retries: int = 0):
        """Mark an agent completion."""
        self._write_event("agent_complete", {
            "agent_id": agent_id,
            "success": success,
            "total": total,
            "done": done,
            "duration_secs": round(duration, 2) if duration else None,
            "retries": retries
        })
        # Legacy format
        status = "✓" if success else "✗"
        detail = f"{agent_id} {status}"
        if duration:
            detail += f" ({duration:.0f}s)"
        if retries > 0:
            detail += f" [{retries} retries]"
        self._write_legacy(f"Phase 3 | Agent [{done}/{total}] | {detail}")

    def decision(self, iteration: int, sufficient: bool, reasoning: str):
        """Log a decision agent result."""
        self._write_event("decision", {
            "iteration": iteration,
            "sufficient": sufficient,
            "reasoning": reasoning[:200]
        })
        status = "SUFFICIENT" if sufficient else "NEEDS MORE"
        self._write_legacy(f"Phase 3 | Decision (iter {iteration}) | {status}: {reasoning[:100]}")

    def error(self, phase: int, message: str):
        """Log an error."""
        self._write_event("error", {
            "phase": phase,
            "message": message
        })
        self._write_legacy(f"Phase {phase} | ERROR | {message}")

    def intervention_needed(self, issue: str):
        """Log that user intervention is needed."""
        self._write_event("intervention_needed", {
            "issue": issue
        })
        self._write_legacy(f"{'!'*60}")
        self._write_legacy(f"Phase 0 | INTERVENTION NEEDED | {issue}")
        self._write_legacy(f"{'!'*60}")

    def approval_waiting(self, gate_id: str):
        """Log that we're waiting for approval."""
        self._write_event("approval_waiting", {
            "gate_id": gate_id
        })
        self._write_legacy(f"Phase 0 | WAITING FOR APPROVAL | {gate_id}")

    def approval_received(self, gate_id: str, approved: bool):
        """Log approval result."""
        self._write_event("approval_received", {
            "gate_id": gate_id,
            "approved": approved
        })
        status = "APPROVED" if approved else "REJECTED"
        self._write_legacy(f"Phase 0 | APPROVAL {status} | {gate_id}")

    def summary(self, stats: dict):
        """Write final summary stats."""
        self._write_event("summary", stats)
        self._write_legacy(f"{'='*60}")
        self._write_legacy("Phase 5 | SUMMARY")
        for key, value in stats.items():
            self._write_legacy(f"Phase 5 |   {key} | {value}")
        self._write_legacy(f"{'='*60}")

    def report_ready(self, report_path: str, summary_path: str, exit_code: int = 0):
        """Mark the report as fully done. Single 'we're finished' event for agents tailing the log."""
        self._write_event("report_ready", {
            "report_path": report_path,
            "summary_path": summary_path,
            "exit_code": exit_code,
        })
        self._write_legacy(f"Phase 5 | REPORT READY | exit={exit_code} | {report_path}")

    def report_failed(self, phase: int, reason: str, exit_code: int = 1):
        """Mark the report as terminally failed. Mirror of report_ready for crash paths."""
        self._write_event("report_failed", {
            "phase": phase,
            "reason": reason,
            "exit_code": exit_code,
        })
        self._write_legacy(f"Phase {phase} | REPORT FAILED | exit={exit_code} | {reason}")

    def approval_timeout(self, gate_id: str, timeout_secs: float):
        """Distinct event for a gate that timed out (vs user-issued reject/stop_early)."""
        self._write_event("approval_timeout", {
            "gate_id": gate_id,
            "timeout_secs": timeout_secs,
        })
        self._write_legacy(f"Phase 0 | APPROVAL TIMEOUT | {gate_id} | {timeout_secs:.0f}s")
