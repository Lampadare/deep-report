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
        "full/agents/*.md",
        "full/seeds/*.md",
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

        for pattern in cls.ORCHESTRATOR_FORBIDDEN:
            if rel.match(pattern):
                raise PermissionError(f"Orchestrator cannot read {rel}")

    @classmethod
    def count_words_streaming(cls, path: Path) -> int:
        """Count words without loading full content into memory.

        Args:
            path: Path to file to count words in

        Returns:
            Word count
        """
        count = 0
        with open(path) as f:
            for line in f:
                count += len(line.split())
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
