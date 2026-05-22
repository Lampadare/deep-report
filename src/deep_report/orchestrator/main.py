#!/usr/bin/env python3
"""Deep Report Orchestrator - Main entry point.

This script orchestrates multi-agent research reports using Claude.
It manages state, spawns research agents, and synthesizes findings.

Usage:
    deep-report "topic" [options]
    deep-report --resume /path/to/report

By default, an interactive interview runs to configure settings.
Use --quick to skip the interview and use sensible defaults.

Options:
    --quick             Skip interview, use defaults (10 agents, sonnet, intermediate)
    --interactive       Pause for approval before research and each iteration
    --model MODEL       Research model: sonnet (default) or opus
    --agents N          Number of research agents (default: 10, range: 3-30)
    --refs PATH         Seed references folder or comma-separated URLs
    --download-papers   Download cited open-access papers
    --audio             Generate audio-friendly version
    --expertise LEVEL   beginner, intermediate (default), expert
    --report-type TYPE  deep-dive, tutorial, comparison, survey
    --resume PATH       Resume from existing report directory
    --list, -l          List unfinished reports and resume one
    --delete, -d        Delete a report from the registry
    --update            Update to latest version from GitHub
    --setup-skill       Install Claude Code skill for /deep-report command
    --intro             Show onboarding guide and example usage
"""

import argparse
import os
import re
import shutil
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import questionary
    from questionary import Style, Choice
    QUESTIONARY_AVAILABLE = True
except ImportError:
    QUESTIONARY_AVAILABLE = False

from .topic_analyzer import TopicAnalyzer, UserProfile

from .state import State
from .progress import ProgressWriter
from .approval import ApprovalGate
from .intervention import InterventionHandler
from .phases import run_setup, run_plan, run_research, run_synthesize, run_cleanup
from .ui import ui
from .utils.keyboard import VerboseToggle
from .utils.agents import process_tracker


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


# Double Ctrl+C exits interview cleanly with no metadata saved
import time as _time
_last_interview_ctrlc = [0.0]


def _check_interview_exit():
    """Call when Ctrl+C detected during interview. Second press within 3s raises."""
    now = _time.monotonic()
    if now - _last_interview_ctrlc[0] < 3.0:
        raise KeyboardInterrupt
    _last_interview_ctrlc[0] = now
    ui.dim("Press Ctrl+C again within 3s to exit")


OPTION_DESCRIPTIONS = {
    "report_type": {
        "deep-dive": "In-depth analysis of the latest developments and best practices",
        "tutorial": "Step-by-step learning guide with examples",
        "comparison": "Side-by-side analysis of approaches or technologies",
        "survey": "Broad landscape overview of a field",
    },
    "expertise": {
        "beginner": "Accessible language, explains fundamentals",
        "intermediate": "Assumes working knowledge, focuses on application",
        "expert": "Technical depth, latest research, assumes domain expertise",
    },
    "model": {
        "sonnet": "Fast and cost-effective (~$2-5 per report)",
        "opus": "Deeper analysis, higher quality (~$8-15 per report)",
    },
}


def _build_choices(options: list[str], category: str, recs: Optional[dict] = None,
                   rec_key: Optional[str] = None) -> list:
    """Build Choice objects with descriptions and AI recommendations.

    Args:
        options: List of option values
        category: Key in OPTION_DESCRIPTIONS
        recs: AI recommendations dict (or None)
        rec_key: Key in recs for the recommended value

    Returns:
        List of Choice objects if questionary available, else plain options
    """
    if not QUESTIONARY_AVAILABLE:
        return options

    descs = OPTION_DESCRIPTIONS.get(category, {})
    choices = []
    rec_value = recs.get(rec_key, "") if recs and rec_key else ""
    rec_reason = recs.get(f"{rec_key}_reason", "") if recs and rec_key else ""

    for opt in options:
        desc = descs.get(opt, "")
        if rec_value == opt and rec_reason:
            label = f"{opt} — {desc} — {rec_reason} (recommended)" if desc else f"{opt} — {rec_reason} (recommended)"
        else:
            label = f"{opt} — {desc}" if desc else opt
        choices.append(Choice(_truncate_choice_label(label), value=opt))
    return choices


def _truncate_choice_label(label: str) -> str:
    """Truncate a choice label to fit within terminal width."""
    max_w = max(10, shutil.get_terminal_size().columns - 6)  # questionary arrow prefix
    return label if len(label) <= max_w else label[:max_w - 1] + "…"


def _prompt_choice(prompt: str, options: list[str], default: int = 0,
                   allow_other: bool = False, rich_choices: list = None) -> str:
    """Prompt user to choose from a list of options with arrow key navigation.

    Args:
        prompt: Question to display
        options: List of predefined options (plain strings, used for fallback + default lookup)
        default: Index of default option
        allow_other: If True, add "Other (specify)" option for freeform input
        rich_choices: Optional list of Choice objects with descriptions (questionary only)

    Returns:
        Selected option, or "custom:<user input>" for freeform entries
    """
    if QUESTIONARY_AVAILABLE:
        if rich_choices:
            choices = list(rich_choices)
            # Find default by value
            default_val = options[default] if default < len(options) else None
            default_choice = None
            for c in choices:
                if getattr(c, 'value', c) == default_val:
                    default_choice = c
                    break
        else:
            choices = options.copy()
            default_choice = choices[default] if default < len(choices) else None

        if allow_other:
            if rich_choices:
                choices.append(Choice("Other (specify)", value="__other__"))
            else:
                choices.append("Other (specify)")

        result = questionary.select(
            prompt,
            choices=choices,
            default=default_choice,
            style=custom_style
        ).ask()

        if result is None:  # Ctrl+C or Ctrl+D
            _check_interview_exit()
            ui.info(f"Using default: {options[default]}")
            return options[default]

        if allow_other and result in ("Other (specify)", "__other__"):
            custom = questionary.text(
                "Enter custom value (e.g., 'technical deep-dive with code examples'):",
                style=custom_style
            ).ask()
            if custom is None:
                _check_interview_exit()
                return options[default]
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
                    custom = input("Enter custom value (e.g., 'technical deep-dive with code examples'): ").strip()
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
                if val < min_val:
                    return f"Value must be at least {min_val}"
                if val > max_val:
                    return f"Value must be at most {max_val}"
                return True
            except ValueError:
                return "Please enter a valid number"

        result = questionary.text(
            f"{prompt} [{min_val}-{max_val}]",
            default=str(default),
            validate=validate,
            style=custom_style
        ).ask()

        if result is None:
            _check_interview_exit()
            ui.info(f"Using default: {default}")
            return default
        if result == "":
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
                ui.info("Using default settings")
                return default


def _prompt_yes_no(prompt: str, default: bool = True) -> bool:
    """Prompt user for yes/no."""
    if QUESTIONARY_AVAILABLE:
        result = questionary.confirm(prompt, default=default, style=custom_style).ask()
        if result is None:
            _check_interview_exit()
            return default
        return result
    else:
        default_str = "Y/n" if default else "y/N"
        try:
            response = input(f"{prompt} [{default_str}]: ").strip().lower()
            if response == "":
                return default
            return response in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            ui.info("Using default settings")
            return default


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
                     if f.is_file() and not f.name.startswith('.')
                     and f.suffix.lower() in {'.pdf', '.md', '.txt', '.html', '.json'}]
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

    if result is None:
        _check_interview_exit()
        return None
    if result == "None (skip)":
        return None

    if result == "Other folder...":
        # Use questionary.path for custom folder
        if QUESTIONARY_AVAILABLE:
            custom = questionary.path(
                "Folder path:",
                only_directories=True,
                style=custom_style
            ).ask()
            if custom is None:
                _check_interview_exit()
                return None
        else:
            custom = input("Folder path: ").strip()
        if custom and not Path(custom).is_dir():
            ui.warning(f"Path not found: {custom}")
            return None
        return custom if custom else None

    # Extract folder name from choice like "./refs (5 files)"
    folder_name = result.split(" (")[0].lstrip("./")
    return str(cwd / folder_name)


def _word_boundary_match(signal: str, text: str) -> bool:
    """Check if signal appears in text at word boundaries (avoids false positives)."""
    return bool(re.search(r'\b' + re.escape(signal) + r'\b', text))


def _analyze_topic_defaults(topic: str) -> dict:
    """Analyze topic to suggest smart defaults for report type and expertise."""
    t = topic.lower()

    # Expertise hints
    expert_signals = [
        "advanced", "novel", "optimization", "theorem", "proof",
        "phd", "doctoral", "state-of-the-art", "sota", "frontier",
        "architecture", "mechanism", "formal", "signal chain",
        "impedance", "spectroscopy", "pharmacokinetic", "nanoscale",
    ]
    beginner_signals = [
        "introduction", "beginner", "basics", "getting started",
        "what is", "overview", "101", "primer", "guide for",
    ]

    if any(_word_boundary_match(s, t) for s in expert_signals):
        expertise_default = 2  # expert
    elif any(_word_boundary_match(s, t) for s in beginner_signals):
        expertise_default = 0  # beginner
    else:
        expertise_default = 1  # intermediate

    # Report type hints
    comparison_signals = [" vs ", "versus", "comparison", "compare", "differences between"]
    tutorial_signals = ["how to", "tutorial", "guide", "learn", "step by step"]
    survey_signals = ["survey", "landscape", "overview of", "review of"]

    if any(_word_boundary_match(s, t) for s in comparison_signals):
        report_type_default = 2  # comparison
    elif any(_word_boundary_match(s, t) for s in tutorial_signals):
        report_type_default = 1  # tutorial
    elif any(_word_boundary_match(s, t) for s in survey_signals):
        report_type_default = 3  # survey
    else:
        report_type_default = 0  # deep-dive

    return {
        "expertise_default": expertise_default,
        "report_type_default": report_type_default,
    }


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

    # Hint for first-time users
    profile_path = Path.home() / ".deep-report" / "profile.json"
    if not profile_path.exists():
        ui.dim("First time? Run 'deep-report --intro' for a guided introduction")

    # Handle long topics: prompt for short name
    brief = ""
    if len(topic) > 100:
        ui.info(f"Topic preview: {ui._truncate(topic, 100)}")
        if QUESTIONARY_AVAILABLE:
            short_name = questionary.text(
                "Short name for report (used for folder/headers):",
                style=custom_style
            ).ask() or ""
        else:
            try:
                short_name = input("Short name for report (used for folder/headers): ").strip()
            except (EOFError, KeyboardInterrupt):
                short_name = ""
        if short_name:
            brief = topic
            topic = short_name

    ui.step(f"Topic: {ui._truncate(topic, 60)}")
    ui.info("Answer the following questions to configure your report.")
    ui.info("Press Enter to accept the default.")

    # Start AI analysis in background
    analyzer = TopicAnalyzer(brief or topic, seed_refs=existing_refs)
    analyzer.analyze_async()

    # ─── Step 1: Report Settings ───
    ui.step("Step 1/4 — Report Settings")

    # Smart defaults based on topic analysis (keyword fallback)
    topic_hints = _analyze_topic_defaults(brief or topic)

    # Get AI recommendations (wait for background analysis to finish)
    ui.dim("Analyzing topic for smart defaults...")
    recs = analyzer.get_recommendations(timeout=60.0)

    # Report type — use AI-suggested types if available, else hardcoded four
    ai_report_types = recs.get("report_types") if recs else None
    rec_report_type = recs.get("report_type", "") if recs else ""

    if ai_report_types and len(ai_report_types) >= 3:
        report_type_options = [e["value"] for e in ai_report_types]
        # Build Choice objects with description in the title (questionary truncates description=)
        if QUESTIONARY_AVAILABLE:
            rec_reason = recs.get("report_type_reason", "") if recs else ""
            report_type_choices = []
            for entry in ai_report_types:
                val = entry["value"]
                desc = entry.get("description", "")
                if val == rec_report_type and rec_reason:
                    label = f"{val} — {desc} (recommended)" if desc else f"{val} (recommended)"
                else:
                    label = f"{val} — {desc}" if desc else val
                report_type_choices.append(Choice(_truncate_choice_label(label), value=val))
        else:
            report_type_choices = None
        # Default to recommended
        if rec_report_type in report_type_options:
            report_type_default = report_type_options.index(rec_report_type)
        else:
            report_type_default = 0
    else:
        report_type_options = ["deep-dive", "tutorial", "comparison", "survey"]
        if rec_report_type in report_type_options:
            report_type_default = report_type_options.index(rec_report_type)
        else:
            report_type_default = topic_hints["report_type_default"]
        report_type_choices = _build_choices(
            report_type_options, "report_type", recs, "report_type")

    report_type = _prompt_choice(
        "What type of report?",
        report_type_options,
        default=report_type_default,
        allow_other=True,
        rich_choices=report_type_choices
    )

    # Expertise level - allow custom audience descriptions
    expertise_options = ["beginner", "intermediate", "expert"]
    if recs and recs.get("expertise") in expertise_options:
        expertise_default = expertise_options.index(recs["expertise"])
    else:
        expertise_default = topic_hints["expertise_default"]
    expertise_choices = _build_choices(
        expertise_options, "expertise", recs, "expertise")
    expertise = _prompt_choice(
        "Target expertise level?",
        expertise_options,
        default=expertise_default,
        allow_other=True,
        rich_choices=expertise_choices
    )

    # ─── Step 2: Research Settings ───
    ui.step("Step 2/4 — Research Configuration")

    # Agent count - use AI recommendation if available
    agent_default = defaults.get('agents', 10)
    if recs and isinstance(recs.get("agent_count"), int):
        rec_agents = max(3, min(recs["agent_count"], 30))
        agent_default = rec_agents
    agent_count = _prompt_int(
        "How many research agents? (~$0.50-2.00/agent for opus, ~$0.15-0.50 for sonnet)",
        default=agent_default, min_val=3, max_val=30
    )

    # Research model
    model_options = ["sonnet", "opus"]
    if recs and recs.get("model") in model_options:
        model_default = model_options.index(recs["model"])
    elif defaults.get('model') in model_options:
        model_default = model_options.index(defaults['model'])
    else:
        model_default = 0
    model_choices = _build_choices(model_options, "model", recs, "model")
    model = _prompt_choice(
        "Research agent model? (opus = higher quality, ~5x cost)",
        model_options,
        default=model_default,
        rich_choices=model_choices
    )

    # ─── Step 3: Output Options ───
    ui.step("Step 3/4 — Output Options")

    # Download papers
    download_papers = _prompt_yes_no("Download cited open-access papers?",
                                      default=defaults.get('download_papers', True))

    # Audio version
    audio = _prompt_yes_no("Generate audio-friendly version?",
                           default=defaults.get('audio', False))

    # ─── Step 4: Seed References ───
    ui.step("Step 4/4 — Seed References")
    ui.info("Provide existing documents (PDFs, URLs, .md files) as starting material. Agents use these to ground their research. (Optional)")

    seed_urls = []
    seed_folder = None

    if existing_refs:
        # Pre-existing refs from --refs flag
        ui.info(f"Seed references (from --refs): {existing_refs}")
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
        try:
            urls_input = input("Seed URLs (comma-separated, Enter to skip): ").strip()
        except (EOFError, KeyboardInterrupt):
            urls_input = ""

    if urls_input:
        for u in urls_input.split(","):
            u = u.strip()
            if not u:
                continue
            if u.startswith("http"):
                seed_urls.append(u)
            else:
                ui.warning(f"Skipping invalid URL: {u}")

    # ─── Advanced ───
    ui.section_divider("Advanced")

    # Interactive mode
    interactive = _prompt_yes_no("Enable approval gates? (pause before research/iterations)", default=False)

    # ─── Summary ───
    ui.section_divider()
    ui.header("CONFIGURATION SUMMARY")
    ui.config_summary({
        "Topic": ui._truncate(topic, 50),
        "Brief": ui._truncate(brief, 50) if brief else "(none)",
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
        ui.info("Cancelled")
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
    def __init__(self, interactive: bool = False, verbose: bool = False,
                 machine_mode: bool = False):
        self.interactive = interactive
        self.verbose = verbose
        self.machine_mode = machine_mode
        self.progress: Optional[ProgressWriter] = None
        self.approval: Optional[ApprovalGate] = None
        self.intervention: Optional[InterventionHandler] = None

    def init_for_report(self, report_dir: Path):
        """Initialize context objects for a report."""
        self.progress = ProgressWriter(report_dir)
        # Approval mode:
        #   --machine + --interactive → "file" (poll pending_approval.json)
        #   --machine alone            → "auto" (skip gates, like non-interactive)
        #   default                    → "stdin" if interactive else "auto"
        if self.machine_mode and self.interactive:
            approval_mode = "file"
        elif self.interactive:
            approval_mode = "stdin"
        else:
            approval_mode = "auto"
        self.approval = ApprovalGate(
            report_dir, self.interactive, self.progress, approval_mode=approval_mode
        )
        self.intervention = InterventionHandler(report_dir, self.progress, self.interactive)
        # Set verbose mode on global UI
        ui.set_verbose(self.verbose)


def main():
    parser = argparse.ArgumentParser(
        description="Deep Report Orchestrator - Multi-agent research synthesis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Default - runs interactive interview
  deep-report "Climate change mitigation"

  # Quick mode - skip interview, use sensible defaults
  deep-report "Climate change mitigation" --quick

  # Pre-fill interview with specific values
  deep-report "Quantum computing" --agents 15 --model opus

  # Machine mode - silent worker for skills/agents, state in progress.jsonl
  deep-report "Quantum computing" --machine --name quantum-computing

  # Resume interrupted report
  deep-report --resume ~/reports/quantum_20260207_1430
        """
    )
    parser.add_argument("topic", nargs="?", help="Research topic")
    parser.add_argument("--quick", action="store_true",
                        help="Skip interview, use sensible defaults")
    parser.add_argument("--interactive", action="store_true",
                        help="Ask for your OK before spending money on research and before each follow-up round (shows per-area coverage scores and suggested directions)")
    parser.add_argument("--model", default=None, choices=["sonnet", "opus"],
                        help="Model for research agents (default: sonnet)")
    def _validate_agents(value):
        val = int(value)
        if val < 3 or val > 30:
            raise argparse.ArgumentTypeError(f"agents must be between 3 and 30, got {val}")
        return val

    parser.add_argument("--agents", type=_validate_agents, default=None,
                        help="Number of research agents to run (3-30, default: 10)")
    parser.add_argument("--refs", help="Seed references folder or comma-separated URLs")
    parser.add_argument("--download-papers", action="store_true",
                        help="Download freely available research PDFs cited in the report (saved to papers/ folder)")
    parser.add_argument("--audio", action="store_true",
                        help="Generate audio-friendly version")
    parser.add_argument("--expertise", default=None,
                        choices=["beginner", "intermediate", "expert"],
                        help="Expertise level (beginner/intermediate/expert)")
    parser.add_argument("--report-type", default=None,
                        help="Report type (e.g. deep-dive, tutorial, comparison, survey — AI may suggest additional types based on your topic)")
    parser.add_argument("--resume", help="Resume from existing report directory")
    parser.add_argument("--list", "-l", action="store_true",
                        help="List unfinished reports and resume one")
    parser.add_argument("--delete", "-d", action="store_true",
                        help="Delete a report from the registry")
    parser.add_argument("--output", help="Output directory (default: cwd/<topic> or ~/Documents/deep-reports/<topic>)")
    parser.add_argument("--cwd", help=argparse.SUPPRESS)
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show per-agent progress, timing, retries, and error details")
    parser.add_argument("--update", action="store_true",
                        help="Update deep-report to latest version from GitHub")
    parser.add_argument("--setup-skill", action="store_true",
                        help="Install Claude Code skill for /deep-report command")
    parser.add_argument("--intro", action="store_true",
                        help="Show onboarding guide and example usage")
    parser.add_argument("--machine", action="store_true",
                        help="Run as silent file-coordinated worker for skills/agents. "
                             "No Rich Live, no questionary, no input(). State flows through "
                             "state/progress.jsonl and state/pending_approval.json.")
    parser.add_argument("--name", default=None,
                        help="Short name for report folder/headers. "
                             "Required in --machine mode when topic > 100 chars.")
    # Approval subcommand: deep-report --approve --report-dir <dir> --gate <id>
    parser.add_argument("--approve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--report-dir", help=argparse.SUPPRESS)
    parser.add_argument("--gate", help=argparse.SUPPRESS)
    parser.add_argument("--decision", choices=["approve", "reject", "stop_early"],
                        default="approve", help=argparse.SUPPRESS)
    parser.add_argument("--feedback", default=None, help=argparse.SUPPRESS)

    args = parser.parse_args()

    # Handle --update flag first
    if args.update:
        return update_cli()

    # Handle --setup-skill flag
    if args.setup_skill:
        return setup_skill()

    # Handle --intro flag
    if args.intro:
        return show_intro()

    # Handle --approve subcommand: write approval response and exit
    if args.approve:
        return _handle_approve_subcommand(args)

    # Machine mode: silent worker. Activate before any UI calls.
    if args.machine:
        ui.set_machine_mode(True)

    # Create context
    ctx = OrchestratorContext(
        interactive=args.interactive,
        verbose=args.verbose,
        machine_mode=args.machine,
    )

    # Handle resume case
    if args.resume:
        return resume_report(Path(args.resume), ctx)

    # Handle --list flag
    if args.list:
        return list_and_resume(ctx)

    # Handle --delete flag
    if args.delete:
        return delete_report()

    # Machine mode: strict bypass — no questionary, no AI recommendations, no prompts.
    # Validate inputs and run with flag-derived config.
    if args.machine:
        if not args.topic:
            print("error: --machine requires a topic", file=sys.stderr)
            return 2
        if len(args.topic) > 100 and not args.name:
            print("error: --machine requires --name when topic is over 100 chars",
                  file=sys.stderr)
            return 2
        config = _build_machine_config(args)
        return run_new_report(config, ctx)

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

        ui.info("Tip: Run 'deep-report --intro' for a getting-started guide")
        parser.error("Topic is required for new reports (or use --resume/--list)")

    # Quick mode: skip interview, use sensible defaults
    if args.quick:
        topic = args.topic
        brief = ""
        # Handle long topics by prompting for short name
        if len(topic) > 100:
            ui.info(f"Topic preview: {ui._truncate(topic, 100)}")
            if QUESTIONARY_AVAILABLE:
                short_name = questionary.text(
                    "Short name for report (used for folder/headers):",
                    style=custom_style
                ).ask() or ""
            else:
                try:
                    short_name = input("Short name for report (used for folder/headers): ").strip()
                except (EOFError, KeyboardInterrupt):
                    short_name = ""
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

        # Use AI recommendations for defaults not explicitly set via CLI
        analyzer = TopicAnalyzer(brief or topic, seed_refs=args.refs)
        analyzer.analyze_async()
        with ui.spinner_task("Analyzing topic..."):
            recs = analyzer.get_recommendations(timeout=3.0)

        # Determine effective values: CLI flag (non-None) > AI recommendation > hardcoded default
        effective_model = args.model or "sonnet"
        effective_expertise = args.expertise or "intermediate"
        effective_report_type = args.report_type or "deep-dive"
        effective_agents = args.agents if args.agents is not None else 10

        # Only apply AI recs when user didn't explicitly set CLI flags (None = unset)
        if recs:
            if args.model is None and recs.get("model") in ("sonnet", "opus"):
                effective_model = recs["model"]
                if effective_model != "sonnet":
                    ui.info(f"AI recommended: {effective_model} model ({recs.get('model_reason', '')})")
            if args.expertise is None and recs.get("expertise") in ("beginner", "intermediate", "expert"):
                effective_expertise = recs["expertise"]
                if effective_expertise != "intermediate":
                    ui.info(f"AI recommended: {effective_expertise} expertise ({recs.get('expertise_reason', '')})")
            if args.report_type is None and recs.get("report_type"):
                effective_report_type = recs["report_type"]
                if effective_report_type != "deep-dive":
                    ui.info(f"AI recommended: {effective_report_type} report type ({recs.get('report_type_reason', '')})")
            if args.agents is None and isinstance(recs.get("agent_count"), int):
                effective_agents = max(3, min(recs["agent_count"], 30))
                if effective_agents != 10:
                    ui.info(f"AI recommended: {effective_agents} agents ({recs.get('agent_count_reason', '')})")

        clamped_agents = max(3, min(effective_agents, 30))
        if clamped_agents != effective_agents:
            ui.warning(f"Agent count adjusted from {effective_agents} to {clamped_agents} (valid range: 3-30)")

        # Show quick mode config summary
        ui.config_summary({
            "Model": effective_model,
            "Agents": clamped_agents,
            "Type": effective_report_type,
            "Expertise": effective_expertise,
        })

        config = {
            "topic": topic,
            "brief": brief,
            "model": effective_model,
            "agent_count": clamped_agents,
            "seed_urls": seed_urls,
            "seed_refs_folder": seed_folder,
            "download_papers": args.download_papers,
            "generate_audio": args.audio,
            "expertise_level": effective_expertise,
            "report_type": effective_report_type,
            "report_dir": args.output,
            "cwd": args.cwd,
            "interactive": args.interactive,
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
    try:
        config = run_configure_interview(
            args.topic,
            cwd=args.cwd,
            existing_refs=args.refs,
            defaults=defaults
        )
    except KeyboardInterrupt:
        print()
        ui.info("Cancelled — no report created")
        return 130
    if config is None:
        return 1
    # Extract interactive flag from config and persist it for resume
    interactive = config.pop("_interactive", False)
    config["interactive"] = interactive
    ctx = OrchestratorContext(interactive=interactive, verbose=args.verbose)
    return run_new_report(config, ctx)




def _build_machine_config(args) -> dict:
    """Build a run config from flags only — no prompts, no AI recommendations.

    Used by --machine to skip questionary, the AI topic analyzer, and any input() fallback.
    Defaults mirror --quick's hardcoded values; the AI recommendation layer is skipped on
    purpose to keep the run deterministic and fast for agent drivers.
    """
    topic = args.topic
    brief = ""
    # Long-topic handling: --name supplies the short form, full topic becomes the brief.
    if args.name:
        brief = topic
        topic = args.name

    # Parse seed refs (same logic as --quick path)
    seed_urls = []
    seed_folder = None
    if args.refs:
        if args.refs.startswith("http"):
            seed_urls = [u.strip() for u in args.refs.split(",")]
        elif Path(args.refs).is_dir():
            seed_folder = args.refs

    return {
        "topic": topic,
        "brief": brief,
        "model": args.model or "sonnet",
        "agent_count": args.agents if args.agents is not None else 10,
        "seed_urls": seed_urls,
        "seed_refs_folder": seed_folder,
        "download_papers": args.download_papers,
        "generate_audio": args.audio,
        "expertise_level": args.expertise or "intermediate",
        "report_type": args.report_type or "deep-dive",
        "report_dir": args.output,
        "cwd": args.cwd,
        "interactive": args.interactive,
    }


def _handle_approve_subcommand(args) -> int:
    """Write an approval response to state/pending_approval.json.

    The running --machine CLI polls this file; writing a `response` block releases the gate.

    Hardened against:
      - empty/whitespace gate or report_dir
      - path traversal (--report-dir resolved + sanity-checked for state/manifest.json)
      - racing --approve invocations (fcntl.flock around read+write)
      - re-approval of an already-resolved gate (status checked under the lock)
      - partial reads (atomic publish via os.replace)
    """
    import fcntl
    from json import loads, dumps, JSONDecodeError

    if not args.report_dir or not args.gate or not args.gate.strip():
        print("error: --approve requires --report-dir and --gate", file=sys.stderr)
        return 2

    # Resolve and sanity-check the report dir. We require manifest.json — it's
    # written by setup.py for every report, so its presence is a cheap
    # "yes this is actually a deep-report report dir" check that blocks
    # path-traversal targets like /etc/passwd.
    report_dir = Path(args.report_dir).expanduser().resolve()
    manifest = report_dir / "state" / "manifest.json"
    if not manifest.exists():
        print(f"error: not a deep-report directory (no manifest.json at {manifest})",
              file=sys.stderr)
        return 2

    approval_file = report_dir / "state" / "pending_approval.json"
    if not approval_file.exists():
        print(f"error: no pending approval at {approval_file}", file=sys.stderr)
        return 2

    # Lock + read + validate + write atomically. Two racing --approve calls will
    # serialize on the lock; the second sees status="responded" and exits.
    try:
        with open(approval_file, "r+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.seek(0)
                try:
                    request = loads(f.read())
                except JSONDecodeError as e:
                    print(f"error: could not parse approval file: {e}",
                          file=sys.stderr)
                    return 2

                if request.get("gate_id") != args.gate:
                    print(f"error: gate mismatch — file has "
                          f"'{request.get('gate_id')}', got '{args.gate}'",
                          file=sys.stderr)
                    return 2

                if request.get("status") in ("responded", "resolved"):
                    print(f"error: gate '{args.gate}' is no longer pending "
                          f"(status={request.get('status')})", file=sys.stderr)
                    return 2

                request["response"] = {
                    "decision": args.decision,
                    "feedback": args.feedback or "",
                    "responded_at": datetime.now().isoformat(),
                }
                request["status"] = "responded"

                # Atomic publish so the polling worker never reads a partial line.
                tmp = approval_file.with_suffix(".tmp")
                tmp.write_text(dumps(request, indent=2))
                os.replace(tmp, approval_file)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError as e:
        print(f"error: could not write approval response: {e}", file=sys.stderr)
        return 1

    print(f"approval {args.decision} written for gate '{args.gate}'")
    return 0


def _check_auth():
    """Probe Claude CLI auth. Warns on failure, never blocks."""
    from ..cli import check_claude_auth
    check_claude_auth()


def run_new_report(config: dict, ctx: OrchestratorContext) -> int:
    """Run a new report from scratch."""
    _check_auth()

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

    # Phase 1: Setup. (REPORT_DIR= is emitted inside run_setup, immediately after
    # the path is determined, so machine-mode drivers see it as the first
    # parseable stdout line.)
    ui.phase_start(1, "Setup")
    if not run_setup(state, config):
        ui.error("Setup failed. Check the error messages above for details.")
        return 1
    ui.phase_complete(1, "Setup")

    # Initialize context with report directory
    ctx.init_for_report(Path(state.report_dir))
    ctx.progress.phase_start(1, "Setup")
    ctx.progress.phase_complete(1, "Setup")

    # Transition message
    ui.info("Setup complete — decomposing into research threads...")

    # Continue with remaining phases
    result = continue_from_phase(state, 2, ctx)

    # Update user profile after successful report
    if result == 0:
        try:
            profile = UserProfile()
            profile.update(config)
        except Exception:
            pass

    return result


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
        if not _prompt_yes_no(f"Delete '{selected.name}'?", default=False):
            return 0
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


def show_intro() -> int:
    """Show onboarding guide with flow explanation and examples."""
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.markdown import Markdown
    except ImportError:
        print("Install 'rich' for the best onboarding experience.")
        print("Run: pip install rich")
        return 0

    console = Console()

    intro = """
# Welcome to Deep Report

Deep Report generates **comprehensive research reports** by spawning multiple
Claude agents in parallel, each investigating a different aspect of your topic.

---

## Getting Started

The simplest way to start:

    deep-report "your research topic"

That's it! An **interactive interview** will walk you through all the options:
- How many research agents to use
- Which model (Sonnet or Opus)
- Target expertise level
- Report type (AI suggests formats tailored to your topic)
- Whether to add seed references

**All flags are optional** - the interview covers everything. Use `--quick` only
if you want to skip the interview and use sensible defaults.

---

## How It Works (5 Phases)

### Phase 1: Setup
- Creates report directory structure
- Processes any seed references (PDFs, URLs, Excel files) you provide
- Writes a scope document defining research boundaries

### Phase 2: Planning
- Decomposes your topic into 10-30 research threads
- Each thread gets a specific angle to investigate
- Shows estimated cost before proceeding

### Phase 3: Research ← *This is where the magic happens*
- Spawns parallel Claude agents with web search, academic databases, and news tools
- Agents write 3,000-6,000 word research files
- A decision agent scores coverage per area and suggests follow-up directions
- In `--interactive` mode, you see coverage scores and pick which directions to pursue
- Iterates until research is deemed sufficient

### Phase 4: Synthesis
- Synthesizes all research into a cohesive report
- Writes executive summary, sections, conclusion
- Compiles references and optionally generates audio version

### Phase 5: Cleanup
- Writes final summary with metrics
- Report is ready at `<output-dir>/report.md`

---

## Where You Provide Input

| When | What |
|------|------|
| **Start** | Topic (required) |
| **Interview** | The interview guides you through all options |
| **Pre-research** | Approve plan and cost estimate (with `--interactive`) |
| **Iterations** | Review coverage scores, select follow-up directions (with `--interactive`) |

---

## Examples

```bash
# Interactive mode (recommended for first time)
deep-report "quantum computing advances in 2025"

# Skip interview, use defaults
deep-report "CRISPR gene therapy" --quick

# Provide seed references (the interview will also ask about this)
deep-report "neural interfaces" --refs ./papers/

# Resume an interrupted report
deep-report --list
```

---

## Output Structure

```
your-topic_20260209_1430/
├── report.md          ← Final synthesized report (15-30k words)
├── refs.md            ← Compiled references
├── SUMMARY.md         ← Metrics and stats
├── full/agents/       ← Raw research from each agent
├── summaries/agents/  ← Condensed summaries
└── state/             ← Checkpoints for resume
```

---

## Tips

- **Seed references** dramatically improve quality - the interview will ask
- Reports auto-save and can be resumed with `--list`
- Press **'v'** during execution to toggle verbose mode
- Typical report takes 15-45 minutes depending on settings

---

Ready? Just run:

    deep-report "your topic here"

"""

    console.print(Panel(Markdown(intro), title="[bold cyan]Deep Report Onboarding[/]", border_style="cyan"))

    # Offer to run example
    if QUESTIONARY_AVAILABLE:
        result = questionary.confirm(
            "Would you like to run a quick example report now?",
            default=False,
            style=custom_style
        ).ask()

        if result:
            console.print("\n[dim]Starting: deep-report \"Brief history of neural networks\" --quick --agents 3[/]\n")
            import subprocess
            subprocess.run([
                "deep-report",
                "Brief history of neural networks",
                "--quick",
                "--agents", "3"
            ])

    return 0


def resume_report(report_dir: Path, ctx: OrchestratorContext) -> int:
    """Resume an existing report."""
    _check_auth()

    state_file = report_dir / "state" / "orchestrator_state.json"

    if not state_file.exists():
        ui.error("No saved session found. Start a new report with: deep-report '<topic>'")
        return 1

    # Skill/agent contract: drivers using --machine --resume need to know which
    # dir we're tailing, just like a fresh run. Emitted before any other output.
    if ctx.machine_mode:
        print(f"REPORT_DIR={report_dir}", flush=True)

    ui.info(f"Resuming report from: {report_dir}")
    try:
        state = State.load(state_file)
    except RuntimeError as e:
        ui.error(f"Could not load saved state: {e}")
        ui.info("Check the backup file or start fresh with: deep-report --delete")
        return 1
    except OSError as e:
        ui.error(f"Could not read state file: {e}")
        return 1

    # Restore interactive mode from persisted state (CLI --interactive also works)
    ctx.interactive = ctx.interactive or getattr(state, 'interactive', False)

    # Initialize context
    ctx.init_for_report(report_dir)

    # Determine where to resume
    _step_display_names = {
        "phase_1_complete": "Setup",
        "phase_2_complete": "Planning",
        "phase_3_complete": "Research",
        "phase_4_complete": "Synthesis",
        "phase_5_complete": "Cleanup",
    }
    current_phase = state.current_phase
    step = state.current_step
    # If the current step indicates the phase completed, resume at the next phase.
    # Otherwise the phase was interrupted mid-flight — resume AT it.
    if step.endswith("_complete") and step.startswith("phase_"):
        target_phase = current_phase + 1
    else:
        target_phase = current_phase
    step_display = _step_display_names.get(step, step)

    # Detailed resume recap (defensive for legacy state files)
    completed = getattr(state, 'completed_threads', [])
    failed = getattr(state, 'failed_threads', [])
    iteration = getattr(state, 'research_iteration', 0)
    max_iter = getattr(state, 'max_iterations', 1)
    est_cost = getattr(state, 'estimated_cost', 0.0)

    ui.step(f"Resuming report: {ui._truncate(state.topic, 60)}")
    ui.info(f"  Completed: {len(completed)} agents, Failed: {len(failed)}")
    ui.info(f"  Iterations: {iteration}/{max_iter}")
    ui.info(f"  Estimated cost so far: ${est_cost:.2f}")
    ui.info(f"  Resuming from Phase {target_phase}: {step_display}")

    # Check if report is already complete
    if current_phase >= 5:
        ui.warning("This report is already fully completed (all 5 phases done)")
        ui.info(f"Report location: {report_dir}")
        return 0

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

    # Ensure state file path is set (only set and save if not already set)
    state_file = Path(state.report_dir) / "state" / "orchestrator_state.json"
    if not state._state_file:
        state._state_file = str(state_file)
        state.save()

    # Set up verbose toggle (press 'v' during execution)
    # Footer shows verbose indicator, so just toggle the flag
    def on_verbose_toggle(enabled: bool):
        ui.set_verbose(enabled)

    verbose_toggle = VerboseToggle(on_toggle=on_verbose_toggle)
    verbose_toggle.start()
    ui.attach_verbose_toggle(verbose_toggle)

    phases = [
        (1, "Setup", lambda s: True),  # Already done if start_phase > 1
        (2, "Planning", run_plan),
        (3, "Research", lambda s: run_research(s, ctx.approval, ctx.progress, ctx.intervention)),
        (4, "Synthesis", run_synthesize),
        (5, "Cleanup", run_cleanup),
    ]

    # 3-tier Ctrl+C: 1st = warning only, 2nd within 3s = graceful shutdown, 3rd = force kill
    _last_ctrlc = [0.0]
    _shutting_down = [False]

    def _handle_sigint(signum, frame):
        now = _time.monotonic()
        elapsed = now - _last_ctrlc[0]

        if _shutting_down[0]:
            # Already shutting down — force kill
            ui.warning("Force killing all agents...")
            process_tracker.shutdown(timeout=0)
            sys.exit(130)

        if elapsed > 3.0:
            # First press (or expired) — just warn
            _last_ctrlc[0] = now
            ui.dim("Press Ctrl+C again within 3s to quit (progress is auto-saved)")
            return

        # Second press within 3s — graceful shutdown
        # Set flag only; avoid calling Live.stop() from signal handler
        # (Rich holds a threading lock that can deadlock if signal fires mid-render)
        _shutting_down[0] = True
        try:
            state.save()
        except Exception:
            pass
        process_tracker.shutdown(timeout=10)
        raise KeyboardInterrupt

    original_handler = signal.signal(signal.SIGINT, _handle_sigint)

    # Start persistent session footer (phase bar + elapsed + cost + verbose)
    # Seed footer cost from persisted state (non-zero on resume)
    if state.total_cost > 0:
        ui.update_session_cost(state.total_cost)
    ui.start_session()

    # Enable verbose if requested
    if ctx.verbose:
        ui.set_verbose(True)

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
                    ui.info("Resume with:")
                    ui.dim(f"  deep-report --resume {state.report_dir}")
                    if not ctx.verbose:
                        ui.info("Re-run with verbose mode (press 'v') for full details")
                    if ctx.progress:
                        ctx.progress.error(phase_num, f"{phase_name} failed")
                    print()
                    return 1

                ui.phase_complete(phase_num, phase_name)
                if ctx.progress:
                    ctx.progress.phase_complete(phase_num, phase_name)

                # Transition messages between phases
                if phase_num == 1:
                    ui.info("Setup complete — decomposing into research threads...")
                elif phase_num == 2:
                    thread_count = len(state.threads)
                    ui.info(f"Plan ready — {thread_count} agents will research in parallel")
                    ui.dim("Research phase typically takes 10-30 minutes depending on agent count and model")
                elif phase_num == 3:
                    completed = len(state.completed_threads)
                    ui.info(f"Research complete — synthesizing findings from {completed} agents...")
                    ui.dim("Synthesis typically takes 5-15 minutes")
                elif phase_num == 4:
                    ui.info("Report assembled — running final cleanup...")

                # Phase bar updates automatically via session footer

            except KeyboardInterrupt:
                ui.warning(f"Interrupted during phase {phase_num}")
                ui.info("Resume with:")
                ui.dim(f"  deep-report --resume {state.report_dir}")
                print()
                return 130

            except Exception as e:
                ui.error(f"Unexpected error: {e}")
                ui.info("Resume with:")
                ui.dim(f"  deep-report --resume {state.report_dir}")
                if ctx.verbose:
                    import traceback
                    traceback.print_exc()
                if ctx.progress:
                    ctx.progress.error(phase_num, str(e))
                print()
                return 1
    finally:
        ui.stop_session()
        signal.signal(signal.SIGINT, original_handler)
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

    # Single "we're done" event for any agent tailing progress.jsonl
    if ctx.progress:
        report_path = str(Path(state.report_dir) / "report.md")
        summary_path = str(Path(state.report_dir) / "SUMMARY.md")
        ctx.progress.report_ready(report_path, summary_path, exit_code=0)

    return 0


if __name__ == "__main__":
    sys.exit(main())
