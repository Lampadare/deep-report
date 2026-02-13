#!/usr/bin/env python3
"""Role-based access enforcement for orchestrator.

Enforces strict isolation between orchestrator and agent roles:
- Orchestrator reads only summaries and metadata, never full content
- Provides streaming word count to avoid loading full files into memory
"""

from pathlib import Path


class RoleEnforcer:
    """Enforces role-based file access restrictions."""

    ORCHESTRATOR_FORBIDDEN = [
        ("full", "agents", "*.md"),
        ("full", "seeds", "*.md"),
    ]

    @classmethod
    def assert_can_read(cls, path: Path, report_dir: Path):
        """Raise PermissionError if orchestrator shouldn't read this path.

        Args:
            path: The file path to check
            report_dir: The report directory root

        Raises:
            PermissionError: If the orchestrator shouldn't access this file
        """
        try:
            rel = path.relative_to(report_dir)
        except ValueError:
            return  # Path not under report_dir, allow

        rel_parts = rel.parts
        for pattern_parts in cls.ORCHESTRATOR_FORBIDDEN:
            if len(rel_parts) != len(pattern_parts):
                continue
            match = True
            for rel_part, pattern_part in zip(rel_parts, pattern_parts):
                if pattern_part.startswith("*"):
                    # Glob pattern - check suffix
                    suffix = pattern_part[1:]
                    if not rel_part.endswith(suffix):
                        match = False
                        break
                elif rel_part != pattern_part:
                    match = False
                    break
            if match:
                raise PermissionError(f"Orchestrator cannot read {rel}")

    @classmethod
    def count_words_streaming(cls, path: Path) -> int:
        """Count words without loading full content into memory.

        Args:
            path: Path to file to count words in

        Returns:
            Word count, or 0 if file cannot be read or is binary
        """
        try:
            with open(path, 'rb') as f:
                chunk = f.read(1024)
                if b'\x00' in chunk:
                    return 0
        except (FileNotFoundError, OSError):
            return 0

        count = 0
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    count += len(line.split())
        except FileNotFoundError:
            return 0
        return count

    @classmethod
    def count_files(cls, directory: Path, pattern: str = "*.md") -> int:
        """Count files matching pattern.

        Args:
            directory: Directory to search
            pattern: Glob pattern for files

        Returns:
            Number of matching files
        """
        if not directory.exists():
            return 0
        return len(list(directory.glob(pattern)))
