#!/usr/bin/env python3
"""State management for deep-report orchestrator.

Provides persistent, auto-saving state that survives crashes and enables resumption.
"""

import fcntl
import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Any
from datetime import datetime

try:
    from pydantic import BaseModel, Field, ValidationError
    HAS_PYDANTIC = True
except ImportError:
    HAS_PYDANTIC = False


# Pydantic validation model (used if pydantic is available)
if HAS_PYDANTIC:
    class StateValidator(BaseModel):
        """Pydantic model for state validation."""
        topic: str = ""
        brief: str = ""
        report_dir: str = ""
        created_at: str = ""
        research_model: str = "sonnet"
        agent_count: int = Field(default=10, ge=1, le=30)
        download_papers: bool = True
        generate_audio: bool = False
        seed_refs_folder: Optional[str] = None
        seed_urls: list[str] = Field(default_factory=list)
        expertise_level: str = "intermediate"
        report_type: str = "deep-dive"
        current_phase: int = Field(default=0, ge=0, le=5)
        current_step: str = ""
        seeds_processed: bool = False
        seeds_summarized: bool = False
        scope_written: bool = False
        plan_written: bool = False
        threads: list[dict] = Field(default_factory=list)
        estimated_cost: float = 0.0
        research_iteration: int = Field(default=0, ge=0)
        max_iterations: int = Field(default=3, ge=1)
        completed_threads: list[str] = Field(default_factory=list)
        failed_threads: list[str] = Field(default_factory=list)
        followups: list[dict] = Field(default_factory=list)
        synthesis_strategy: str = ""
        synthesis_parts: list[str] = Field(default_factory=list)
        report_assembled: bool = False
        refs_compiled: bool = False
        audio_generated: bool = False
        papers_downloaded: bool = False
        total_input_tokens: int = 0
        total_output_tokens: int = 0
        total_cost: float = 0.0

        class Config:
            extra = "ignore"


def validate_state_data(data: dict) -> tuple[dict, list[str]]:
    """Validate state data using Pydantic if available.

    Args:
        data: Raw state data dict

    Returns:
        Tuple of (validated_data, list of warning messages)
    """
    warnings = []

    if HAS_PYDANTIC:
        try:
            validated = StateValidator(**data)
            return validated.model_dump(), warnings
        except ValidationError as e:
            for error in e.errors():
                field = ".".join(str(loc) for loc in error["loc"])
                warnings.append(f"Validation error in {field}: {error['msg']}")
            # Return original data with warnings
            return data, warnings
    else:
        # Basic validation without Pydantic
        if "agent_count" in data:
            count = data["agent_count"]
            if not isinstance(count, int) or count < 1:
                data["agent_count"] = 1
                warnings.append("agent_count was invalid, set to 1")
            elif count > 30:
                data["agent_count"] = 30
                warnings.append("agent_count was > 30, clamped to 30")

        return data, warnings


@dataclass
class ResearchThread:
    id: str
    title: str
    objective: str
    questions: list[str]
    status: str = "pending"  # pending, in_progress, completed, failed
    output_file: Optional[str] = None
    summary_file: Optional[str] = None
    word_count: int = 0
    attempts: int = 0


@dataclass
class FollowUp:
    id: str
    reason: str  # "gap", "conflict", "deepen"
    focus: str
    parent_threads: list[str]
    iteration: int
    status: str = "pending"
    output_file: Optional[str] = None
    summary_file: Optional[str] = None


@dataclass
class State:
    """Persistent state for the orchestrator."""

    # Core info
    topic: str = ""
    brief: str = ""  # Detailed research instructions (if topic is long)
    report_dir: str = ""
    created_at: str = ""

    # Config from user
    research_model: str = "sonnet"
    agent_count: int = 10
    download_papers: bool = True
    generate_audio: bool = False
    seed_refs_folder: Optional[str] = None
    seed_urls: list[str] = field(default_factory=list)
    expertise_level: str = "intermediate"
    report_type: str = "deep-dive"

    # Phase tracking
    current_phase: int = 0
    current_step: str = ""

    # Phase 1 outputs
    seeds_processed: bool = False
    seeds_summarized: bool = False
    scope_written: bool = False

    # Phase 2 outputs
    plan_written: bool = False
    threads: list[dict] = field(default_factory=list)
    estimated_cost: float = 0.0

    # Phase 3 tracking
    research_iteration: int = 0
    max_iterations: int = 3
    completed_threads: list[str] = field(default_factory=list)
    failed_threads: list[str] = field(default_factory=list)
    followups: list[dict] = field(default_factory=list)

    # Phase 4 tracking
    synthesis_strategy: str = ""  # "single", "multi"
    synthesis_parts: list[str] = field(default_factory=list)
    report_assembled: bool = False
    refs_compiled: bool = False
    audio_generated: bool = False
    papers_downloaded: bool = False

    # Metrics
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost: float = 0.0

    # Internal
    _state_file: str = field(default="", repr=False)
    _save_lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    def save(self):
        """Persist state to disk with atomic write and file locking."""
        if not self._state_file:
            return

        with self._save_lock:
            # Ensure parent directory exists
            state_path = Path(self._state_file)
            state_path.parent.mkdir(parents=True, exist_ok=True)

            data = {
                f.name: getattr(self, f.name)
                for f in self.__dataclass_fields__.values()
                if not f.name.startswith("_")
            }
            data["updated_at"] = datetime.now().isoformat()

            def json_default(obj):
                """Handle non-serializable types."""
                if hasattr(obj, '__dict__'):
                    return obj.__dict__
                return str(obj)

            try:
                # Write to temp file first, then atomic rename
                fd, tmp_path = tempfile.mkstemp(
                    dir=state_path.parent,
                    prefix=".state_",
                    suffix=".tmp"
                )
                try:
                    with os.fdopen(fd, 'w') as f:
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                        try:
                            f.write(json.dumps(data, indent=2, default=json_default))
                            f.flush()
                            os.fsync(f.fileno())
                        finally:
                            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    os.rename(tmp_path, self._state_file)
                except Exception:
                    # Clean up temp file on error
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise
            except OSError as e:
                from .ui import ui
                ui.warning(f"Failed to save state: {e}")
                raise

    @classmethod
    def load(cls, state_file: Path) -> "State":
        """Load state from disk with file locking, or create new if doesn't exist."""
        state = cls()
        state._state_file = str(state_file)

        if state_file.exists():
            try:
                with open(state_file, 'r') as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                    try:
                        data = json.load(f)
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)

                # Validate and sanitize data
                validated_data, warnings = validate_state_data(data)
                for warning in warnings:
                    from .ui import ui
                    ui.warning(f"State validation: {warning}")

                for key, value in validated_data.items():
                    if hasattr(state, key) and key != "_state_file":
                        setattr(state, key, value)
            except json.JSONDecodeError as e:
                # Back up corrupted file before raising
                backup_path = state_file.with_suffix('.bak')
                try:
                    import shutil
                    shutil.copy2(state_file, backup_path)
                    from .ui import ui
                    ui.warning(f"Corrupted state backed up to {backup_path}")
                except OSError:
                    pass
                raise RuntimeError(
                    f"State file corrupted ({state_file}). "
                    f"A backup may exist at {backup_path}. "
                    f"You can delete the state file and restart, or restore from the backup."
                ) from e
            except KeyError as e:
                # Backup corrupted file before raising
                bak_path = state_file.with_suffix('.key_error.bak')
                try:
                    import shutil
                    shutil.copy2(state_file, bak_path)
                except OSError:
                    pass
                raise RuntimeError(
                    f"State file has missing data (key {e}). "
                    f"A backup was saved to {bak_path}. "
                    f"You can delete the state file and restart, or restore from the backup."
                ) from e
        else:
            state.created_at = datetime.now().isoformat()
            state.save()

        return state

    def get_thread(self, thread_id: str) -> Optional[dict]:
        """Get a thread by ID."""
        for t in self.threads:
            if t.get("id") == thread_id:
                return t
        return None

    def update_thread(self, thread_id: str, **kwargs):
        """Update a thread and save state (lock protects read-modify-write)."""
        with self._save_lock:
            for t in self.threads:
                if t.get("id") == thread_id:
                    t.update(kwargs)
                    break
            else:
                return
            self.save()

    def add_followup(self, followup: dict):
        """Add a follow-up research item."""
        self.followups.append(followup)
        self.save()

    def mark_phase_complete(self, phase: int):
        """Mark a phase as complete."""
        self.current_phase = phase
        self.current_step = f"phase_{phase}_complete"
        self.save()

        # Update central registry
        if self.report_dir:
            try:
                from .registry import registry
                registry.update(
                    Path(self.report_dir),
                    phase=phase,
                    step=self.current_step,
                    complete=(phase >= 5)
                )
            except Exception as e:
                from .ui import ui
                ui.warning(f"Registry update failed: {e}")

    def checkpoint(self, step: str):
        """Create a checkpoint at the current step."""
        self.current_step = step
        self.save()

    def get_pending_threads(self) -> list[dict]:
        """Get threads that haven't been completed."""
        return [t for t in self.threads
                if t.get("id") not in self.completed_threads
                and t.get("id") not in self.failed_threads]

    def get_pending_followups(self) -> list[dict]:
        """Get followups that haven't been completed."""
        return [f for f in self.followups if f.get("status") == "pending"]
