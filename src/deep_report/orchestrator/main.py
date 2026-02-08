#!/usr/bin/env python3
"""Deep Report Orchestrator - Main entry point.

This script orchestrates multi-agent research reports using Claude.
It manages state, spawns research agents, and synthesizes findings.

Usage:
    python3 -m orchestrator.main "topic" [options]
    python3 -m orchestrator.main --resume /path/to/report

Configure mode (interactive interview):
    python3 -m orchestrator.main "topic" --configure

Quick mode (no questions, sensible defaults):
    python3 -m orchestrator.main "topic" --quick

Interactive mode (approval gates before research):
    python3 -m orchestrator.main "topic" --interactive

Options:
    --configure         Interactive interview for all settings
    --quick             Use defaults: 10 agents, sonnet, intermediate, state-of-the-art
    --interactive       Pause for approval before research and each iteration
    --model MODEL       Research model: sonnet (default) or opus
    --agents N          Number of research agents (default: 10, max: 30)
    --refs PATH         Seed references folder or comma-separated URLs
    --download-papers   Download cited open-access papers
    --audio             Generate audio-friendly version
    --expertise LEVEL   beginner, intermediate (default), expert
    --report-type TYPE  state-of-the-art, tutorial, comparison, survey
    --resume PATH       Resume from existing report directory
"""

import argparse
import sys
from pathlib import Path
from typing import Optional

from .state import State
from .progress import ProgressWriter
from .approval import ApprovalGate
from .intervention import InterventionHandler
from .phases import run_setup, run_plan, run_research, run_synthesize, run_cleanup
from .ui import ui


def _prompt_choice(prompt: str, options: list[str], default: int = 0, allow_other: bool = False) -> str:
    """Prompt user to choose from a list of options.

    Args:
        prompt: Question to display
        options: List of predefined options
        default: Index of default option
        allow_other: If True, add "Other (specify)" option for freeform input

    Returns:
        Selected option, or "custom:<user input>" for freeform entries
    """
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
    default_str = "Y/n" if default else "y/N"
    try:
        response = input(f"{prompt} [{default_str}]: ").strip().lower()
        if response == "":
            return default
        return response in ("y", "yes")
    except EOFError:
        return default


def run_configure_interview(topic: str, cwd: str = None) -> dict:
    """Run interactive configuration interview.

    Returns:
        Configuration dict ready for run_new_report()
    """
    print("\n" + "=" * 60)
    print("DEEP REPORT CONFIGURATION")
    print("=" * 60)
    print(f"\nTopic: {topic}")
    print("\nAnswer the following questions to configure your report.")
    print("Press Enter to accept the default (marked with →).")

    # Report type - allow custom types like "technical deep-dive with code examples"
    report_type = _prompt_choice(
        "What type of report?",
        ["state-of-the-art", "tutorial", "comparison", "survey"],
        default=0,
        allow_other=True
    )

    # Expertise level - allow custom audience descriptions
    expertise = _prompt_choice(
        "Target expertise level?",
        ["beginner", "intermediate", "expert"],
        default=1,
        allow_other=True
    )

    # Agent count
    agent_count = _prompt_int(
        "How many research agents? (more = broader coverage, higher cost)",
        default=10, min_val=3, max_val=30
    )

    # Research model
    model = _prompt_choice(
        "Research agent model? (opus = higher quality, ~5x cost)",
        ["sonnet", "opus"],
        default=0
    )

    # Download papers
    print()
    download_papers = _prompt_yes_no("Download cited open-access papers?", default=True)

    # Audio version
    audio = _prompt_yes_no("Generate audio-friendly version?", default=False)

    # Seed references
    print()
    refs_input = input("Seed references (folder path or URLs, Enter to skip): ").strip()
    seed_urls = []
    seed_folder = None
    if refs_input:
        if refs_input.startswith("http"):
            seed_urls = [u.strip() for u in refs_input.split(",")]
        elif Path(refs_input).is_dir():
            seed_folder = refs_input
        else:
            print(f"Warning: path not found: {refs_input}")

    # Interactive mode
    interactive = _prompt_yes_no("Enable approval gates? (pause before research/iterations)", default=False)

    # Summary
    print("\n" + "=" * 60)
    print("CONFIGURATION SUMMARY")
    print("=" * 60)
    print(f"  Topic:           {topic}")
    print(f"  Report type:     {report_type}")
    print(f"  Expertise:       {expertise}")
    print(f"  Agents:          {agent_count}")
    print(f"  Model:           {model}")
    print(f"  Download papers: {'Yes' if download_papers else 'No'}")
    print(f"  Audio version:   {'Yes' if audio else 'No'}")
    if seed_folder:
        print(f"  Seed folder:     {seed_folder}")
    elif seed_urls:
        print(f"  Seed URLs:       {len(seed_urls)} URLs")
    print(f"  Approval gates:  {'Yes' if interactive else 'No'}")
    print("=" * 60)

    if not _prompt_yes_no("\nProceed with this configuration?", default=True):
        print("Cancelled.")
        return None

    return {
        "topic": topic,
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
    def __init__(self, interactive: bool = False):
        self.interactive = interactive
        self.progress: Optional[ProgressWriter] = None
        self.approval: Optional[ApprovalGate] = None
        self.intervention: Optional[InterventionHandler] = None

    def init_for_report(self, report_dir: Path):
        """Initialize context objects for a report."""
        self.progress = ProgressWriter(report_dir)
        self.approval = ApprovalGate(report_dir, self.interactive, self.progress)
        self.intervention = InterventionHandler(report_dir, self.progress)


def main():
    parser = argparse.ArgumentParser(
        description="Deep Report Orchestrator - Multi-agent research synthesis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Configure mode - interactive interview for all settings
  python3 -m orchestrator.main "Climate change mitigation" --configure

  # Quick mode - sensible defaults, no questions
  python3 -m orchestrator.main "Climate change mitigation" --quick

  # Interactive - pauses for approval at key points
  python3 -m orchestrator.main "Machine learning in healthcare" --interactive

  # Full control via flags
  python3 -m orchestrator.main "Quantum computing" --agents 15 --model opus --audio

  # Resume interrupted report
  python3 -m orchestrator.main --resume ~/reports/quantum_20260207_1430
        """
    )
    parser.add_argument("topic", nargs="?", help="Research topic")
    parser.add_argument("--configure", action="store_true",
                        help="Interactive interview for all settings before running")
    parser.add_argument("--quick", action="store_true",
                        help="Use defaults: 10 agents, sonnet, intermediate, state-of-the-art, download papers")
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
    parser.add_argument("--output", help="Output directory (default: cwd/<topic> or ~/Documents/deep-reports/<topic>)")
    parser.add_argument("--cwd", help="Original working directory (set by CLI wrapper)")

    args = parser.parse_args()

    # Create context
    ctx = OrchestratorContext(interactive=args.interactive)

    # Handle resume case
    if args.resume:
        return resume_report(Path(args.resume), ctx)

    # Require topic for new reports
    if not args.topic:
        parser.error("Topic is required for new reports (or use --resume)")

    # Configure mode: interactive interview
    if args.configure:
        config = run_configure_interview(args.topic, cwd=args.cwd)
        if config is None:
            return 1
        # Extract interactive flag from config
        interactive = config.pop("_interactive", False)
        ctx = OrchestratorContext(interactive=interactive)
        return run_new_report(config, ctx)

    # Quick mode: sensible defaults
    if args.quick:
        config = {
            "topic": args.topic,
            "model": args.model,  # Can still override with --model opus
            "agent_count": args.agents if args.agents != 10 else 10,
            "seed_urls": [],
            "seed_refs_folder": None,
            "download_papers": True,
            "generate_audio": False,
            "expertise_level": "intermediate",
            "report_type": "state-of-the-art",
            "report_dir": args.output,
            "cwd": args.cwd,
        }
        # Still allow overrides
        if args.refs:
            if args.refs.startswith("http"):
                config["seed_urls"] = [u.strip() for u in args.refs.split(",")]
            elif Path(args.refs).is_dir():
                config["seed_refs_folder"] = args.refs
        if args.audio:
            config["generate_audio"] = True
    else:
        # Parse seed refs
        seed_urls = []
        seed_folder = None
        if args.refs:
            if args.refs.startswith("http"):
                seed_urls = [u.strip() for u in args.refs.split(",")]
            elif Path(args.refs).is_dir():
                seed_folder = args.refs
            else:
                print(f"Warning: refs path not found: {args.refs}")

        # Build config dict
        config = {
            "topic": args.topic,
            "model": args.model,
            "agent_count": max(1, min(args.agents, 30)),
            "seed_urls": seed_urls,
            "seed_refs_folder": seed_folder,
            "download_papers": args.download_papers,
            "generate_audio": args.audio,
            "expertise_level": args.expertise,
            "report_type": args.report_type,
            "report_dir": args.output,
            "cwd": args.cwd,
        }

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
    print("\n[PHASE 1] Setup")
    if not run_setup(state, config):
        print("Setup failed")
        return 1

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


def resume_report(report_dir: Path, ctx: OrchestratorContext) -> int:
    """Resume an existing report."""

    state_file = report_dir / "state" / "orchestrator_state.json"

    if not state_file.exists():
        print(f"Error: No state file found at {state_file}")
        return 1

    print(f"Resuming report from: {report_dir}")
    state = State.load(state_file)

    # Initialize context
    ctx.init_for_report(report_dir)

    # Determine where to resume
    current_phase = state.current_phase
    target_phase = current_phase + 1
    print(f"Last completed phase: {current_phase}")
    print(f"Last step: {state.current_step}")

    # Validate files exist for target phase
    valid, error = _validate_resume_files(state, target_phase)
    if not valid:
        print(f"Error: {error}")
        print("You may need to restart from an earlier phase.")
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

    phases = [
        (1, "Setup", lambda s: True),  # Already done if start_phase > 1
        (2, "Plan", run_plan),
        (3, "Research", lambda s: run_research(s, ctx.approval, ctx.progress)),
        (4, "Synthesize", run_synthesize),
        (5, "Cleanup", run_cleanup),
    ]

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
