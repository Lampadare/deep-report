#!/usr/bin/env python3
"""Deep Report Orchestrator - Main entry point.

This script orchestrates multi-agent research reports using Claude.
It manages state, spawns research agents, and synthesizes findings.

Usage:
    python3 -m orchestrator.main "topic" [options]
    python3 -m orchestrator.main --resume /path/to/report

By default, an interactive interview runs to configure settings.
Use --quick to skip the interview and use sensible defaults.

Options:
    --quick             Skip interview, use defaults (10 agents, sonnet, intermediate)
    --interactive       Pause for approval before research and each iteration
    --model MODEL       Research model: sonnet (default) or opus
    --agents N          Number of research agents (default: 10, max: 30)
    --refs PATH         Seed references folder or comma-separated URLs
    --download-papers   Download cited open-access papers
    --audio             Generate audio-friendly version
    --expertise LEVEL   beginner, intermediate (default), expert
    --report-type TYPE  state-of-the-art, tutorial, comparison, survey
    --resume PATH       Resume from existing report directory
    --list, -l          List unfinished reports and resume one
    --delete, -d        Delete a report from the registry
    --update            Update to latest version from GitHub
    --setup-skill       Install Claude Code skill for /deep-report command
"""

import argparse
import sys
from pathlib import Path
from typing import Optional

try:
    import questionary
    from questionary import Style
    QUESTIONARY_AVAILABLE = True
except ImportError:
    QUESTIONARY_AVAILABLE = False

from .state import State
from .progress import ProgressWriter
from .approval import ApprovalGate
from .intervention import InterventionHandler
from .phases import run_setup, run_plan, run_research, run_synthesize, run_cleanup
from .ui import ui
from .utils.keyboard import VerboseToggle


# Custom style for questionary prompts
if QUESTIONARY_AVAILABLE:
    custom_style = Style([
        ('qmark', 'fg:cyan bold'),
        ('question', 'fg:white bold'),
        ('answer', 'fg:#87d787 bold'),       # Soft mint green (readable on dark/light)
        ('pointer', 'fg:cyan bold'),
        ('highlighted', 'fg:cyan bold'),
        ('selected', 'fg:#87d787'),          # Soft mint green
        ('instruction', 'fg:#888888'),
    ])
else:
    custom_style = None


def _prompt_choice(prompt: str, options: list[str], default: int = 0, allow_other: bool = False) -> str:
    """Prompt user to choose from a list of options with arrow key navigation.

    Args:
        prompt: Question to display
        options: List of predefined options
        default: Index of default option
        allow_other: If True, add "Other (specify)" option for freeform input

    Returns:
        Selected option, or "custom:<user input>" for freeform entries
    """
    if QUESTIONARY_AVAILABLE:
        choices = options.copy()
        if allow_other:
            choices.append("Other (specify)")

        result = questionary.select(
            prompt,
            choices=choices,
            default=choices[default],
            style=custom_style
        ).ask()

        if result is None:  # Ctrl+C or Ctrl+D
            return options[default]

        if allow_other and result == "Other (specify)":
            custom = questionary.text("Enter custom value:", style=custom_style).ask()
            return f"custom:{custom}" if custom else options[default]

        return result
    else:
        # Fallback to text-based selection
        print(f"\n{prompt}")
        for i, opt in enumerate(options):
            marker = "→" if i == default else " "
            print(f"  {marker} [{i + 1}] {opt}")

        if allow_other:
            print(f"    [{len(options) + 1}] Other (specify)")
            max_choice = len(options) + 1
        else:
            max_choice = len(options)

        while True:
            try:
                response = input(f"\nChoice [1-{max_choice}] (Enter for default): ").strip()
                if response == "":
                    return options[default]
                idx = int(response) - 1
                if 0 <= idx < len(options):
                    return options[idx]
                if allow_other and idx == len(options):
                    custom = input("Enter custom value: ").strip()
                    if custom:
                        return f"custom:{custom}"
                    print("Custom value cannot be empty")
                    continue
                print(f"Please enter 1-{max_choice}")
            except ValueError:
                print(f"Please enter 1-{max_choice}")
            except EOFError:
                return options[default]


def _prompt_int(prompt: str, default: int, min_val: int, max_val: int) -> int:
    """Prompt user for an integer in a range."""
    if QUESTIONARY_AVAILABLE:
        def validate(text):
            if not text:
                return True  # Allow empty for default
            try:
                val = int(text)
                return min_val <= val <= max_val
            except ValueError:
                return False

        result = questionary.text(
            f"{prompt} [{min_val}-{max_val}]",
            default=str(default),
            validate=validate,
            style=custom_style
        ).ask()

        if result is None or result == "":
            return default
        return int(result)
    else:
        print(f"\n{prompt}")
        while True:
            try:
                response = input(f"Number [{min_val}-{max_val}] (Enter for {default}): ").strip()
                if response == "":
                    return default
                val = int(response)
                if min_val <= val <= max_val:
                    return val
                print(f"Please enter {min_val}-{max_val}")
            except ValueError:
                print(f"Please enter a number {min_val}-{max_val}")
            except EOFError:
                return default


def _prompt_yes_no(prompt: str, default: bool = True) -> bool:
    """Prompt user for yes/no."""
    if QUESTIONARY_AVAILABLE:
        result = questionary.confirm(prompt, default=default, style=custom_style).ask()
        return result if result is not None else default
    else:
        default_str = "Y/n" if default else "y/N"
        try:
            response = input(f"{prompt} [{default_str}]: ").strip().lower()
            if response == "":
                return default
            return response in ("y", "yes")
        except EOFError:
            return default


def _truncate_display(text: str, max_len: int = 60) -> str:
    """Truncate text for display, replacing newlines with spaces."""
    text = text.replace('\n', ' ').strip()
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


def _find_seed_folder_candidates(cwd: Path) -> list[tuple[str, int]]:
    """Find directories in cwd that could contain seed references.

    Returns:
        List of (folder_path, file_count) tuples, sorted by relevance
    """
    candidates = []

    # Priority folder names (check these first)
    priority_names = {"seed-refs", "seeds", "references", "refs", "papers", "docs"}

    for item in cwd.iterdir():
        if not item.is_dir() or item.name.startswith('.'):
            continue

        # Count relevant files
        try:
            files = [f for f in item.iterdir()
                     if f.is_file() and f.suffix.lower() in {'.pdf', '.md', '.txt', '.html', '.json'}]
        except PermissionError:
            continue

        if files:
            # Boost priority folders to top
            priority = 0 if item.name.lower() in priority_names else 1
            candidates.append((str(item), len(files), priority))

    # Sort: priority first, then by file count descending
    candidates.sort(key=lambda x: (x[2], -x[1]))
    return [(path, count) for path, count, _ in candidates]


def _prompt_seed_folder(cwd: Path) -> Optional[str]:
    """Prompt user to select a seed folder from cwd."""
    candidates = _find_seed_folder_candidates(cwd)

    # Build choices
    choices = ["None (skip)"]
    for path, count in candidates[:8]:  # Limit to 8 folders
        folder_name = Path(path).name
        choices.append(f"./{folder_name} ({count} files)")
    choices.append("Other folder...")

    if QUESTIONARY_AVAILABLE:
        result = questionary.select(
            "📁 Seed reference folder?",
            choices=choices,
            style=custom_style
        ).ask()
    else:
        print("\n📁 Seed reference folder?")
        for i, choice in enumerate(choices):
            marker = "→" if i == 0 else " "
            print(f"  {marker} [{i + 1}] {choice}")
        try:
            response = input(f"\nChoice [1-{len(choices)}] (Enter for None): ").strip()
            if response == "" or response == "1":
                result = choices[0]
            else:
                idx = int(response) - 1
                if 0 <= idx < len(choices):
                    result = choices[idx]
                else:
                    result = choices[0]
        except (ValueError, EOFError):
            result = choices[0]

    if result is None or result == "None (skip)":
        return None

    if result == "Other folder...":
        # Use questionary.path for custom folder
        if QUESTIONARY_AVAILABLE:
            custom = questionary.path(
                "Folder path:",
                only_directories=True,
                style=custom_style
            ).ask()
        else:
            custom = input("Folder path: ").strip()
        return custom if custom and Path(custom).is_dir() else None

    # Extract folder name from choice like "./refs (5 files)"
    folder_name = result.split(" (")[0].lstrip("./")
    return str(cwd / folder_name)


def run_configure_interview(topic: str, cwd: str = None, existing_refs: str = None,
                            defaults: dict = None) -> dict:
    """Run interactive configuration interview.

    Args:
        topic: Research topic
        cwd: Original working directory
        existing_refs: Pre-existing refs from --refs flag
        defaults: Dict of default values from CLI flags (agents, model, etc.)

    Returns:
        Configuration dict ready for run_new_report()
    """
    defaults = defaults or {}

    # Show fancy header (clears screen)
    ui.interview_header()

    # Handle long topics: prompt for short name
    brief = ""
    if len(topic) > 100:
        print(f"\nTopic preview: {topic[:100]}...")
        if QUESTIONARY_AVAILABLE:
            short_name = questionary.text(
                "Short name for report (used for folder/headers):",
                style=custom_style
            ).ask() or ""
        else:
            short_name = input("Short name for report (used for folder/headers): ").strip()
        if short_name:
            brief = topic
            topic = short_name

    ui.step(f"Topic: {_truncate_display(topic, 60)}")
    print("\nAnswer the following questions to configure your report.")
    print("Press Enter to accept the default.")

    # ─── Report Settings ───
    ui.section_divider("Report Settings")

    # Report type - allow custom types like "technical deep-dive with code examples"
    report_type_options = ["state-of-the-art", "tutorial", "comparison", "survey"]
    report_type_default = report_type_options.index(defaults.get('report_type', 'state-of-the-art')) \
        if defaults.get('report_type') in report_type_options else 0
    report_type = _prompt_choice(
        "What type of report?",
        report_type_options,
        default=report_type_default,
        allow_other=True
    )

    # Expertise level - allow custom audience descriptions
    expertise_options = ["beginner", "intermediate", "expert"]
    expertise_default = expertise_options.index(defaults.get('expertise', 'intermediate')) \
        if defaults.get('expertise') in expertise_options else 1
    expertise = _prompt_choice(
        "Target expertise level?",
        expertise_options,
        default=expertise_default,
        allow_other=True
    )

    # ─── Research Settings ───
    ui.section_divider("Research Settings")

    # Agent count
    agent_count = _prompt_int(
        "How many research agents? (more = broader coverage, higher cost)",
        default=defaults.get('agents', 10), min_val=3, max_val=30
    )

    # Research model
    model_options = ["sonnet", "opus"]
    model_default = model_options.index(defaults.get('model', 'sonnet')) \
        if defaults.get('model') in model_options else 0
    model = _prompt_choice(
        "Research agent model? (opus = higher quality, ~5x cost)",
        model_options,
        default=model_default
    )

    # ─── Output Options ───
    ui.section_divider("Output Options")

    # Download papers
    download_papers = _prompt_yes_no("Download cited open-access papers?",
                                      default=defaults.get('download_papers', True))

    # Audio version
    audio = _prompt_yes_no("Generate audio-friendly version?",
                           default=defaults.get('audio', False))

    # ─── Seed References ───
    ui.section_divider("Seed References")

    seed_urls = []
    seed_folder = None

    if existing_refs:
        # Pre-existing refs from --refs flag
        print(f"Seed references (from --refs): {existing_refs}")
        if _prompt_yes_no("Use these?", default=True):
            if existing_refs.startswith("http"):
                seed_urls = [u.strip() for u in existing_refs.split(",")]
            elif Path(existing_refs).is_dir():
                seed_folder = existing_refs
        else:
            # Let them choose new refs
            interview_cwd = Path(cwd) if cwd else Path.cwd()
            seed_folder = _prompt_seed_folder(interview_cwd)
    else:
        # 1. File seeds (folder selector)
        interview_cwd = Path(cwd) if cwd else Path.cwd()
        seed_folder = _prompt_seed_folder(interview_cwd)

    # 2. URL seeds (text input)
    if QUESTIONARY_AVAILABLE:
        urls_input = questionary.text(
            "🔗 Seed URLs (comma-separated, Enter to skip):",
            style=custom_style
        ).ask() or ""
    else:
        urls_input = input("Seed URLs (comma-separated, Enter to skip): ").strip()

    if urls_input:
        seed_urls = [u.strip() for u in urls_input.split(",") if u.strip().startswith("http")]

    # ─── Advanced ───
    ui.section_divider("Advanced")

    # Interactive mode
    interactive = _prompt_yes_no("Enable approval gates? (pause before research/iterations)", default=False)

    # ─── Summary ───
    ui.section_divider()
    ui.header("CONFIGURATION SUMMARY")
    ui.config_summary({
        "Topic": _truncate_display(topic, 50),
        "Brief": _truncate_display(brief, 50) if brief else "(none)",
        "Report type": report_type,
        "Expertise": expertise,
        "Agents": agent_count,
        "Model": model,
        "Download papers": "Yes" if download_papers else "No",
        "Audio version": "Yes" if audio else "No",
        "Seed folder": seed_folder or "(none)",
        "Seed URLs": f"{len(seed_urls)} URLs" if seed_urls else "(none)",
        "Approval gates": "Yes" if interactive else "No",
    })

    if not _prompt_yes_no("\nProceed with this configuration?", default=True):
        print("Cancelled.")
        return None

    return {
        "topic": topic,
        "brief": brief,
        "model": model,
        "agent_count": agent_count,
        "seed_urls": seed_urls,
        "seed_refs_folder": seed_folder,
        "download_papers": download_papers,
        "generate_audio": audio,
        "expertise_level": expertise,
        "report_type": report_type,
        "report_dir": None,
        "cwd": cwd,
        "_interactive": interactive,
    }


# Global context for phases
class OrchestratorContext:
    """Shared context across all phases."""
    def __init__(self, interactive: bool = False, verbose: bool = False):
        self.interactive = interactive
        self.verbose = verbose
        self.progress: Optional[ProgressWriter] = None
        self.approval: Optional[ApprovalGate] = None
        self.intervention: Optional[InterventionHandler] = None

    def init_for_report(self, report_dir: Path):
        """Initialize context objects for a report."""
        self.progress = ProgressWriter(report_dir)
        self.approval = ApprovalGate(report_dir, self.interactive, self.progress)
        self.intervention = InterventionHandler(report_dir, self.progress)
        # Set verbose mode on global UI
        ui.set_verbose(self.verbose)


def main():
    parser = argparse.ArgumentParser(
        description="Deep Report Orchestrator - Multi-agent research synthesis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Default - runs interactive interview
  python3 -m orchestrator.main "Climate change mitigation"

  # Quick mode - skip interview, use sensible defaults
  python3 -m orchestrator.main "Climate change mitigation" --quick

  # Pre-fill interview with specific values
  python3 -m orchestrator.main "Quantum computing" --agents 15 --model opus

  # Resume interrupted report
  python3 -m orchestrator.main --resume ~/reports/quantum_20260207_1430
        """
    )
    parser.add_argument("topic", nargs="?", help="Research topic")
    parser.add_argument("--quick", action="store_true",
                        help="Skip interview, use sensible defaults")
    parser.add_argument("--interactive", action="store_true",
                        help="Pause for approval before research and each iteration")
    parser.add_argument("--model", default="sonnet", choices=["sonnet", "opus"],
                        help="Model for research agents (default: sonnet)")
    parser.add_argument("--agents", type=int, default=10,
                        help="Number of research agents (default: 10, max: 30)")
    parser.add_argument("--refs", help="Seed references folder or comma-separated URLs")
    parser.add_argument("--download-papers", action="store_true",
                        help="Download cited open-access papers")
    parser.add_argument("--audio", action="store_true",
                        help="Generate audio-friendly version")
    parser.add_argument("--expertise", default="intermediate",
                        choices=["beginner", "intermediate", "expert"])
    parser.add_argument("--report-type", default="state-of-the-art",
                        choices=["state-of-the-art", "tutorial", "comparison", "survey"])
    parser.add_argument("--resume", help="Resume from existing report directory")
    parser.add_argument("--list", "-l", action="store_true",
                        help="List unfinished reports and resume one")
    parser.add_argument("--delete", "-d", action="store_true",
                        help="Delete a report from the registry")
    parser.add_argument("--output", help="Output directory (default: cwd/<topic> or ~/Documents/deep-reports/<topic>)")
    parser.add_argument("--cwd", help="Original working directory (set by CLI wrapper)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show detailed agent activity (press 'v' during execution to toggle)")
    parser.add_argument("--update", action="store_true",
                        help="Update deep-report to latest version from GitHub")
    parser.add_argument("--setup-skill", action="store_true",
                        help="Install Claude Code skill for /deep-report command")

    args = parser.parse_args()

    # Handle --update flag first
    if args.update:
        return update_cli()

    # Handle --setup-skill flag
    if args.setup_skill:
        return setup_skill()

    # Create context
    ctx = OrchestratorContext(interactive=args.interactive, verbose=args.verbose)

    # Handle resume case
    if args.resume:
        return resume_report(Path(args.resume), ctx)

    # Handle --list flag
    if args.list:
        return list_and_resume(ctx)

    # Handle --delete flag
    if args.delete:
        return delete_report()

    # If no topic, check for unfinished reports to resume
    if not args.topic:
        from .registry import registry
        unfinished = registry.list_unfinished()

        if unfinished:
            # Sort by most recently updated
            unfinished.sort(key=lambda r: r["updated_at"], reverse=True)
            ui.info(f"Found {len(unfinished)} unfinished report(s)")
            selected = ui.report_picker(unfinished)
            if selected:
                return resume_report(selected, ctx)

        parser.error("Topic is required for new reports (or use --resume/--list)")

    # Quick mode: skip interview, use sensible defaults
    if args.quick:
        topic = args.topic
        brief = ""
        # Handle long topics by prompting for short name
        if len(topic) > 100:
            print(f"\nTopic preview: {topic[:100]}...")
            if QUESTIONARY_AVAILABLE:
                short_name = questionary.text(
                    "Short name for report (used for folder/headers):",
                    style=custom_style
                ).ask() or ""
            else:
                short_name = input("Short name for report (used for folder/headers): ").strip()
            if short_name:
                brief = topic
                topic = short_name

        # Parse seed refs
        seed_urls = []
        seed_folder = None
        if args.refs:
            if args.refs.startswith("http"):
                seed_urls = [u.strip() for u in args.refs.split(",")]
            elif Path(args.refs).is_dir():
                seed_folder = args.refs

        config = {
            "topic": topic,
            "brief": brief,
            "model": args.model,
            "agent_count": max(3, min(args.agents, 30)),
            "seed_urls": seed_urls,
            "seed_refs_folder": seed_folder,
            "download_papers": args.download_papers or True,  # Default to True in quick mode
            "generate_audio": args.audio,
            "expertise_level": args.expertise,
            "report_type": args.report_type,
            "report_dir": args.output,
            "cwd": args.cwd,
        }
        return run_new_report(config, ctx)

    # Default: run interactive interview with CLI flags as pre-filled defaults
    defaults = {
        'agents': args.agents,
        'model': args.model,
        'expertise': args.expertise,
        'report_type': args.report_type,
        'download_papers': args.download_papers,
        'audio': args.audio,
    }
    config = run_configure_interview(
        args.topic,
        cwd=args.cwd,
        existing_refs=args.refs,
        defaults=defaults
    )
    if config is None:
        return 1
    # Extract interactive flag from config
    interactive = config.pop("_interactive", False)
    ctx = OrchestratorContext(interactive=interactive, verbose=args.verbose)
    return run_new_report(config, ctx)


def run_new_report(config: dict, ctx: OrchestratorContext) -> int:
    """Run a new report from scratch."""

    # Display header with Rich UI
    mode_str = "INTERACTIVE" if ctx.interactive else ""
    ui.header(
        f"DEEP REPORT: {config['topic']}",
        mode_str
    )
    ui.config_summary({
        "Model": config['model'],
        "Agents": config['agent_count'],
        "Type": config['report_type'],
        "Expertise": config['expertise_level'],
    })

    # Initialize state (will be saved after setup)
    state = State()

    # Phase 1: Setup
    ui.phase_start(1, "Setup")
    if not run_setup(state, config):
        ui.error("Setup failed")
        return 1
    ui.phase_complete(1, "Setup")

    # Initialize context with report directory
    ctx.init_for_report(Path(state.report_dir))
    ctx.progress.phase_start(1, "Setup")
    ctx.progress.phase_complete(1, "Setup")

    # Continue with remaining phases
    return continue_from_phase(state, 2, ctx)


def _validate_resume_files(state: State, target_phase: int) -> tuple[bool, str]:
    """Validate that critical files exist for the phase we're resuming to.

    Returns:
        Tuple of (valid, error_message)
    """
    report_dir = Path(state.report_dir)

    # Phase 3+ needs plan
    if target_phase >= 3 and not (report_dir / "state" / "plan.md").exists():
        return False, "Missing plan.md - cannot resume to research phase"

    # Phase 4+ needs research outputs
    if target_phase >= 4:
        agent_files = list((report_dir / "full" / "agents").glob("*.md"))
        if not agent_files:
            return False, "No research outputs found - cannot resume to synthesis phase"

    return True, ""


def list_and_resume(ctx: OrchestratorContext) -> int:
    """List unfinished reports and optionally resume one."""
    from .registry import registry

    reports = registry.list_unfinished()

    if not reports:
        ui.info("No unfinished reports found")
        return 0

    # Sort by most recently updated
    reports.sort(key=lambda r: r["updated_at"], reverse=True)

    selected = ui.report_picker(reports)
    if selected:
        return resume_report(selected, ctx)

    return 0


def delete_report() -> int:
    """Show all reports and delete selected one from registry."""
    from .registry import registry

    reports = registry.list_all()

    if not reports:
        ui.info("No reports in registry")
        return 0

    # Sort by most recently updated
    reports.sort(key=lambda r: r["updated_at"], reverse=True)

    selected = ui.report_picker_for_delete(reports)
    if selected:
        if registry.delete(str(selected)):
            ui.success(f"Removed from registry: {selected.name}")
            ui.info("Note: Files on disk were not deleted")
        else:
            ui.warning("Report not found in registry")

    return 0


def update_cli() -> int:
    """Update deep-report to latest version from GitHub."""
    import subprocess

    ui.info("Updating deep-report from GitHub...")

    try:
        result = subprocess.run(
            ["pip", "install", "--upgrade", "git+https://github.com/lampadare/deep-report.git"],
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode == 0:
            ui.success("Updated successfully")
            # Show new version if we can parse it
            if "Successfully installed" in result.stdout:
                ui.info(result.stdout.split("Successfully installed")[-1].strip())
            return 0
        else:
            ui.error(f"Update failed: {result.stderr}")
            return 1

    except subprocess.TimeoutExpired:
        ui.error("Update timed out")
        return 1
    except Exception as e:
        ui.error(f"Update failed: {e}")
        return 1


def setup_skill() -> int:
    """Install Claude Code skill by symlinking to ~/.claude/skills/."""
    import deep_report

    # Find the skill directory in the installed package
    package_dir = Path(deep_report.__path__[0])
    skill_source = package_dir / "skill"

    if not skill_source.exists():
        ui.error(f"Skill not found at {skill_source}")
        return 1

    # Target: ~/.claude/skills/deep-report
    claude_skills_dir = Path.home() / ".claude" / "skills"
    skill_target = claude_skills_dir / "deep-report"

    # Create skills directory if needed
    claude_skills_dir.mkdir(parents=True, exist_ok=True)

    # Check if already exists
    if skill_target.exists() or skill_target.is_symlink():
        if skill_target.is_symlink():
            current = skill_target.resolve()
            if current == skill_source.resolve():
                ui.success("Skill already installed")
                ui.info(f"  {skill_target} → {skill_source}")
                return 0
            else:
                # Different symlink, remove and recreate
                skill_target.unlink()
        else:
            ui.warning(f"Removing existing {skill_target}")
            import shutil
            shutil.rmtree(skill_target)

    # Create symlink
    try:
        skill_target.symlink_to(skill_source)
        ui.success("Claude Code skill installed")
        ui.info(f"  {skill_target} → {skill_source}")
        ui.info("Use /deep-report in Claude Code to invoke")
        return 0
    except Exception as e:
        ui.error(f"Failed to create symlink: {e}")
        return 1


def resume_report(report_dir: Path, ctx: OrchestratorContext) -> int:
    """Resume an existing report."""

    state_file = report_dir / "state" / "orchestrator_state.json"

    if not state_file.exists():
        ui.error(f"No state file found at {state_file}")
        return 1

    ui.info(f"Resuming report from: {report_dir}")
    state = State.load(state_file)

    # Initialize context
    ctx.init_for_report(report_dir)

    # Determine where to resume
    current_phase = state.current_phase
    target_phase = current_phase + 1
    ui.info(f"Last completed phase: {current_phase}")
    ui.info(f"Last step: {state.current_step}")

    # Validate files exist for target phase
    valid, error = _validate_resume_files(state, target_phase)
    if not valid:
        ui.error(error)
        ui.info("You may need to restart from an earlier phase.")
        return 1

    # Resume from next phase
    return continue_from_phase(state, target_phase, ctx)


def continue_from_phase(state: State, start_phase: int, ctx: OrchestratorContext) -> int:
    """Continue execution from a specific phase."""

    # Ensure state file path is set
    if not state._state_file:
        state_file = Path(state.report_dir) / "state" / "orchestrator_state.json"
        state._state_file = str(state_file)
        state.save()

    # Set up verbose toggle (press 'v' during execution)
    def on_verbose_toggle(enabled: bool):
        ui.set_verbose(enabled)
        status = "ON" if enabled else "OFF"
        ui.info(f"Verbose mode: {status}")

    verbose_toggle = VerboseToggle(on_toggle=on_verbose_toggle)
    toggle_available = verbose_toggle.start()
    if toggle_available:
        ui.info("Press 'v' to toggle verbose output")
    elif ctx.verbose:
        ui.info("Verbose mode enabled via --verbose flag")

    phases = [
        (1, "Setup", lambda s: True),  # Already done if start_phase > 1
        (2, "Plan", run_plan),
        (3, "Research", lambda s: run_research(s, ctx.approval, ctx.progress)),
        (4, "Synthesize", run_synthesize),
        (5, "Cleanup", run_cleanup),
    ]

    try:
        for phase_num, phase_name, phase_func in phases:
            if phase_num < start_phase:
                continue

            ui.phase_start(phase_num, phase_name)
            if ctx.progress:
                ctx.progress.phase_start(phase_num, phase_name)

            try:
                success = phase_func(state)

                if not success:
                    ui.error(f"Phase {phase_num} ({phase_name}) failed")
                    if ctx.progress:
                        ctx.progress.error(phase_num, f"{phase_name} failed")
                    return 1

                ui.phase_complete(phase_num, phase_name)
                if ctx.progress:
                    ctx.progress.phase_complete(phase_num, phase_name)

            except KeyboardInterrupt:
                ui.warning(f"Interrupted during phase {phase_num}")
                ui.info(f"Resume with: deep-report --resume {state.report_dir}")
                return 130

            except Exception as e:
                ui.error(f"Error in phase {phase_num}: {e}")
                if ctx.progress:
                    ctx.progress.error(phase_num, str(e))
                import traceback
                traceback.print_exc()
                return 1
    finally:
        # Stop verbose toggle listener
        verbose_toggle.stop()

    # Final summary with Rich UI
    ui.final_summary(
        state.report_dir,
        {
            "Completed threads": len(state.completed_threads),
            "Failed threads": len(state.failed_threads),
            "Iterations": state.research_iteration,
        }
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
