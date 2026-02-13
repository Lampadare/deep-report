#!/usr/bin/env python3
"""CLI entry point for deep-report."""

import sys
import shutil
from pathlib import Path


def check_claude_cli():
    """Verify Claude CLI is available."""
    if not shutil.which("claude"):
        from .orchestrator.ui import ui
        ui.error("Claude CLI not found.")
        ui.info("deep-report requires Claude Code to be installed.")
        ui.info("Install from: https://claude.ai/download")
        ui.info("After installing, run 'claude' once to authenticate.")
        sys.exit(1)


def main():
    # Handle --install-skill as alias for --setup-skill
    if "--install-skill" in sys.argv:
        sys.argv[sys.argv.index("--install-skill")] = "--setup-skill"

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
    from .orchestrator.ui import ui

    import deep_report
    skill_src = Path(deep_report.__path__[0]) / "skill"
    skill_dst = Path.home() / ".claude" / "skills" / "deep-report"

    # Create skills directory if needed
    skill_dst.parent.mkdir(parents=True, exist_ok=True)

    if skill_dst.exists() or skill_dst.is_symlink():
        ui.info(f"Skill already exists at {skill_dst}")
        response = input("Replace it? [y/N]: ").strip().lower()
        if response != 'y':
            ui.info("Cancelled.")
            return
        if skill_dst.is_symlink():
            skill_dst.unlink()
        else:
            import shutil
            shutil.rmtree(skill_dst)

    try:
        skill_dst.symlink_to(skill_src)
    except (OSError, PermissionError) as e:
        ui.error(f"Could not create skill link: {e}")
        return

    ui.success(f"Skill installed: {skill_dst} -> {skill_src}")
    ui.info("You can now use /deep-report in Claude Code")


if __name__ == "__main__":
    main()
