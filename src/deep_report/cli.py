#!/usr/bin/env python3
"""CLI entry point for deep-report."""

import sys
import shutil
import subprocess
from pathlib import Path


def check_claude_cli():
    """Verify Claude CLI is available."""
    # On Windows, npm installs the CLI as claude.cmd; check both shims.
    if not any(shutil.which(n) for n in ("claude", "claude.cmd", "claude.exe")):
        from .orchestrator.ui import ui
        ui.error("Claude CLI not found.")
        ui.info("deep-report requires Claude Code to be installed.")
        ui.info("Install from: https://claude.ai/download")
        ui.info("After installing, run 'claude' once to authenticate.")
        sys.exit(1)


def check_claude_auth():
    """Probe Claude CLI authentication. Warns on failure, never blocks."""
    from .orchestrator.utils.agents import _resolve_claude_binary
    try:
        result = subprocess.run(
            [_resolve_claude_binary(), "--print", "--model", "haiku", "say ok"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            from .orchestrator.ui import ui
            ui.warning("Claude CLI may not be authenticated. Run 'claude' to log in.")
    except subprocess.TimeoutExpired:
        from .orchestrator.ui import ui
        ui.warning("Claude CLI auth check timed out. Run 'claude' to verify authentication.")
    except Exception:
        # Don't block on unexpected errors
        pass


def main():
    # Handle --install-skill as alias for --setup-skill
    if "--install-skill" in sys.argv:
        sys.argv[sys.argv.index("--install-skill")] = "--setup-skill"

    # Treat bare "help" / "h" / "?" as --help so they don't get parsed as a topic
    if len(sys.argv) >= 2 and sys.argv[1] in ("help", "h", "?"):
        sys.argv[1] = "--help"

    if "--version" in sys.argv:
        from . import __version__
        print(f"deep-report {__version__}")
        return

    if "--help" not in sys.argv and "-h" not in sys.argv:
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
    except (OSError, NotImplementedError):
        # Windows without Developer Mode / Admin can't symlink.
        # Fall back to copytree so the skill still installs (caveat: future
        # deep-report upgrades require re-running --setup-skill to refresh).
        try:
            import shutil
            shutil.copytree(skill_src, skill_dst)
            ui.info("Installed skill as copy (re-run --setup-skill after upgrades)")
        except Exception as e:
            ui.error(f"Could not install skill: {e}")
            return

    ui.success(f"Skill installed: {skill_dst} -> {skill_src}")
    ui.info("You can now use /deep-report in Claude Code")


if __name__ == "__main__":
    main()
