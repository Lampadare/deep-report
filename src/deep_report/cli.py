#!/usr/bin/env python3
"""CLI entry point for deep-report."""

import sys
import shutil
from pathlib import Path


def check_claude_cli():
    """Verify Claude CLI is available."""
    if not shutil.which("claude"):
        print("ERROR: Claude CLI not found.")
        print()
        print("deep-report requires Claude Code to be installed.")
        print("Install from: https://claude.ai/download")
        print()
        print("After installing, run 'claude' once to authenticate.")
        sys.exit(1)


def main():
    # Handle special commands first
    if "--install-skill" in sys.argv:
        install_skill()
        return

    if "--version" in sys.argv:
        from . import __version__
        print(f"deep-report {__version__}")
        return

    if "--help" in sys.argv or "-h" in sys.argv:
        # Let orchestrator handle --help
        pass
    else:
        # Check Claude CLI before running
        check_claude_cli()

    # Import and run orchestrator
    from .orchestrator.main import main as orchestrator_main
    sys.exit(orchestrator_main())


def install_skill():
    """Install/symlink the Claude Code skill."""
    import deep_report
    skill_src = Path(deep_report.__path__[0]) / "skill"
    skill_dst = Path.home() / ".claude" / "skills" / "deep-report"

    # Create skills directory if needed
    skill_dst.parent.mkdir(parents=True, exist_ok=True)

    if skill_dst.exists() or skill_dst.is_symlink():
        print(f"Skill already exists at {skill_dst}")
        response = input("Replace it? [y/N]: ").strip().lower()
        if response != 'y':
            print("Cancelled.")
            return
        if skill_dst.is_symlink():
            skill_dst.unlink()
        else:
            import shutil
            shutil.rmtree(skill_dst)

    skill_dst.symlink_to(skill_src)
    print(f"Skill installed: {skill_dst} -> {skill_src}")
    print()
    print("You can now use /deep-report in Claude Code")


if __name__ == "__main__":
    main()
