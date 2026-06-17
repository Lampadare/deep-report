#!/usr/bin/env python3
"""Central registry for tracking all deep-report runs."""

import json
import os
import tempfile
import portalocker
from pathlib import Path
from datetime import datetime
from typing import Optional


REGISTRY_DIR = Path.home() / ".deep-report"
REGISTRY_FILE = REGISTRY_DIR / "registry.json"

_registry_instance = None


class ReportRegistry:
    """Manages the central registry of all reports."""

    LOCK_FILE = REGISTRY_DIR / ".registry.lock"

    def __init__(self):
        self._ensure_registry()

    def _ensure_registry(self):
        """Create registry file if it doesn't exist."""
        REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
        if not REGISTRY_FILE.exists():
            self._write_unlocked({"reports": []})

    def _read_unlocked(self) -> dict:
        """Read registry without acquiring the cross-process lock.

        Caller must hold LOCK_EX on the lock file.
        """
        try:
            with open(REGISTRY_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return {"reports": []}

    def _write_unlocked(self, data: dict):
        """Write registry with atomic write but without acquiring the cross-process lock.

        Caller must hold LOCK_EX on the lock file (except during __init__).
        """
        fd, tmp_path = tempfile.mkstemp(
            dir=REGISTRY_DIR,
            prefix=".registry_",
            suffix=".tmp"
        )
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, REGISTRY_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _locked(self):
        """Context manager that holds LOCK_EX on the lock file for the entire read-modify-write."""
        import contextlib

        @contextlib.contextmanager
        def _lock_ctx():
            self.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
            lock_fd = os.open(str(self.LOCK_FILE), os.O_CREAT | os.O_RDWR)
            try:
                portalocker.lock(lock_fd, portalocker.LOCK_EX)
                yield
            finally:
                portalocker.unlock(lock_fd)
                os.close(lock_fd)

        return _lock_ctx()

    def _read(self) -> dict:
        """Read registry with file locking (standalone reads)."""
        with self._locked():
            return self._read_unlocked()

    def _write(self, data: dict):
        """Write registry with file locking (standalone writes)."""
        with self._locked():
            self._write_unlocked(data)

    def register(self, report_dir: Path, topic: str) -> str:
        """Register a new report. Returns report ID."""
        report_id = report_dir.name
        with self._locked():
            data = self._read_unlocked()

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
            self._write_unlocked(data)
        return report_id

    def update(self, report_dir: Path, phase: int, step: str, complete: bool = False):
        """Update a report's status."""
        with self._locked():
            data = self._read_unlocked()
            for report in data["reports"]:
                if report["path"] == str(report_dir):
                    report["phase"] = phase
                    report["step"] = step
                    report["complete"] = complete
                    report["updated_at"] = datetime.now().isoformat()
                    break
            self._write_unlocked(data)

    def list_all(self) -> list[dict]:
        """List all registered reports."""
        return self._prune_and_write_if_needed()

    def list_unfinished(self) -> list[dict]:
        """List reports where complete=False."""
        return [r for r in self.list_all() if not r["complete"]]

    def _prune_stale(self, reports: list[dict]) -> list[dict]:
        """Remove entries where the path no longer exists."""
        valid = [r for r in reports if Path(r["path"]).exists()]
        return valid

    def _prune_and_write_if_needed(self) -> list[dict]:
        """Read, prune stale entries, and write back in a single locked transaction."""
        with self._locked():
            data = self._read_unlocked()
            original_len = len(data["reports"])
            data["reports"] = [r for r in data["reports"] if Path(r["path"]).exists()]
            if len(data["reports"]) < original_len:
                self._write_unlocked(data)
            return data["reports"]

    def delete(self, report_path: str) -> bool:
        """Delete a report from the registry by path.

        Returns True if deleted, False if not found.
        """
        with self._locked():
            data = self._read_unlocked()
            original_len = len(data["reports"])
            data["reports"] = [r for r in data["reports"] if r["path"] != report_path]

            if len(data["reports"]) < original_len:
                self._write_unlocked(data)
                return True
        return False

    def delete_by_index(self, index: int) -> bool:
        """Delete a report by its index in the list.

        Returns True if deleted, False if index invalid.
        Uses a single locked transaction to avoid TOCTOU race.
        """
        with self._locked():
            data = self._read_unlocked()
            reports = [r for r in data["reports"] if Path(r["path"]).exists()]

            # Sort by updated_at descending (same order as displayed)
            reports.sort(key=lambda r: r["updated_at"], reverse=True)

            if 0 <= index < len(reports):
                path_to_delete = reports[index]["path"]
                data["reports"] = [r for r in reports if r["path"] != path_to_delete]
                self._write_unlocked(data)
                return True
        return False


def get_registry() -> ReportRegistry:
    """Lazy initialization of registry singleton."""
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = ReportRegistry()
    return _registry_instance


# Property-like access for backwards compatibility
class _RegistryProxy:
    """Proxy for lazy registry initialization."""
    def __getattr__(self, name):
        return getattr(get_registry(), name)


registry = _RegistryProxy()
