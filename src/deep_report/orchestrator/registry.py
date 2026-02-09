#!/usr/bin/env python3
"""Central registry for tracking all deep-report runs."""

import json
import fcntl
from pathlib import Path
from datetime import datetime
from typing import Optional


REGISTRY_DIR = Path.home() / ".deep-report"
REGISTRY_FILE = REGISTRY_DIR / "registry.json"


class ReportRegistry:
    """Manages the central registry of all reports."""

    def __init__(self):
        self._ensure_registry()

    def _ensure_registry(self):
        """Create registry file if it doesn't exist."""
        REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
        if not REGISTRY_FILE.exists():
            self._write({"reports": []})

    def _read(self) -> dict:
        """Read registry with file locking."""
        try:
            with open(REGISTRY_FILE, 'r') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                data = json.load(f)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                return data
        except (json.JSONDecodeError, FileNotFoundError):
            return {"reports": []}

    def _write(self, data: dict):
        """Write registry with file locking."""
        with open(REGISTRY_FILE, 'w') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            json.dump(data, f, indent=2)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def register(self, report_dir: Path, topic: str) -> str:
        """Register a new report. Returns report ID."""
        report_id = report_dir.name
        data = self._read()

        # Remove existing entry for same path
        data["reports"] = [r for r in data["reports"] if r["path"] != str(report_dir)]

        data["reports"].append({
            "id": report_id,
            "path": str(report_dir),
            "topic": topic,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "phase": 1,
            "step": "setup",
            "complete": False,
        })
        self._write(data)
        return report_id

    def update(self, report_dir: Path, phase: int, step: str, complete: bool = False):
        """Update a report's status."""
        data = self._read()
        for report in data["reports"]:
            if report["path"] == str(report_dir):
                report["phase"] = phase
                report["step"] = step
                report["complete"] = complete
                report["updated_at"] = datetime.now().isoformat()
                break
        self._write(data)

    def list_all(self) -> list[dict]:
        """List all registered reports."""
        data = self._read()
        return self._prune_stale(data["reports"])

    def list_unfinished(self) -> list[dict]:
        """List reports where complete=False."""
        return [r for r in self.list_all() if not r["complete"]]

    def _prune_stale(self, reports: list[dict]) -> list[dict]:
        """Remove entries where the path no longer exists."""
        valid = []
        for r in reports:
            if Path(r["path"]).exists():
                valid.append(r)
        # Update registry if we pruned anything
        if len(valid) < len(reports):
            data = self._read()
            data["reports"] = valid
            self._write(data)
        return valid

    def delete(self, report_path: str) -> bool:
        """Delete a report from the registry by path.

        Returns True if deleted, False if not found.
        """
        data = self._read()
        original_len = len(data["reports"])
        data["reports"] = [r for r in data["reports"] if r["path"] != report_path]

        if len(data["reports"]) < original_len:
            self._write(data)
            return True
        return False

    def delete_by_index(self, index: int) -> bool:
        """Delete a report by its index in the list.

        Returns True if deleted, False if index invalid.
        """
        data = self._read()
        reports = self._prune_stale(data["reports"])

        # Sort by updated_at descending (same order as displayed)
        reports.sort(key=lambda r: r["updated_at"], reverse=True)

        if 0 <= index < len(reports):
            path_to_delete = reports[index]["path"]
            data["reports"] = [r for r in data["reports"] if r["path"] != path_to_delete]
            self._write(data)
            return True
        return False


# Singleton instance
registry = ReportRegistry()
