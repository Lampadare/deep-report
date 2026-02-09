#!/usr/bin/env python3
"""Rich terminal interface for deep-report orchestrator.

Provides colored output, progress bars, spinners, and formatted panels.
Falls back gracefully if Rich is not installed.
"""

from typing import Optional
from contextlib import contextmanager
import sys

# Try to import Rich, fall back to plain text if not available
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
    from rich.table import Table
    from rich.live import Live
    from rich.text import Text
    from rich.spinner import Spinner
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


# Phase colors and icons for visual distinction
PHASE_COLORS = ["cyan", "blue", "magenta", "green", "yellow"]
PHASE_ICONS = {
    1: "🔧",  # Setup
    2: "📋",  # Plan
    3: "🔬",  # Research
    4: "📝",  # Synthesize
    5: "✅",  # Cleanup
}


class DeepReportUI:
    """Rich terminal interface for deep-report.

    Provides styled output with colors, progress bars, spinners, and panels.
    Falls back to plain text if Rich is not installed.
    """

    def __init__(self):
        if RICH_AVAILABLE:
            self.console = Console()
        else:
            self.console = None
        self._progress = None
        self._live = None
        self._task_id = None
        self._verbose = False

    def set_verbose(self, enabled: bool):
        """Enable or disable verbose mode."""
        self._verbose = enabled

    @property
    def verbose_enabled(self) -> bool:
        """Check if verbose mode is enabled."""
        return self._verbose

    def verbose(self, message: str):
        """Print message only if verbose mode is enabled."""
        if not self._verbose:
            return
        if RICH_AVAILABLE:
            self.console.print(f"[dim]{message}[/]")
        else:
            print(f"  [V] {message}")

    def _truncate(self, text: str, max_len: int = 60) -> str:
        """Truncate text to max_len, replacing newlines with spaces."""
        text = text.replace('\n', ' ').strip()
        if len(text) <= max_len:
            return text
        return text[:max_len - 3] + "..."

    def _print(self, message: str, style: str = None):
        """Print with optional styling."""
        if self.console and style:
            self.console.print(message, style=style)
        elif self.console:
            self.console.print(message)
        else:
            # Strip Rich markup for plain output
            import re
            plain = re.sub(r'\[/?[^\]]+\]', '', message)
            print(plain)

    def header(self, title: str, subtitle: str = ""):
        """Print a styled header."""
        display_title = self._truncate(title, 80)
        if RICH_AVAILABLE:
            self.console.print()
            self.console.print(Panel(
                f"[bold white]{display_title}[/]\n{subtitle}" if subtitle else f"[bold white]{display_title}[/]",
                border_style="cyan",
                padding=(0, 2)
            ))
        else:
            print()
            print("=" * 60)
            print(display_title)
            if subtitle:
                print(subtitle)
            print("=" * 60)

    def interview_header(self):
        """Show colorful banner for configure mode."""
        if RICH_AVAILABLE:
            self.console.clear()  # Start with clean slate
            self.console.print()  # Top padding
            title = Text("🔬 DEEP REPORT CONFIGURATION", style="bold white")
            self.console.print(Panel(
                title,
                border_style="cyan",
                padding=(0, 2),
                subtitle="[dim]Use ↑↓ arrows to navigate, Enter to select[/]"
            ))
        else:
            # Clear screen for non-Rich terminals
            print("\033[2J\033[H", end="")
            print()
            print("=" * 60)
            print("DEEP REPORT CONFIGURATION")
            print("Use Up/Down arrows to navigate, Enter to select")
            print("=" * 60)

    def section_divider(self, label: str = ""):
        """Print a subtle section divider."""
        if RICH_AVAILABLE:
            if label:
                self.console.print(f"\n[dim]─── {label} ───[/]\n")
            else:
                self.console.print()  # Just spacing
        else:
            if label:
                print(f"\n--- {label} ---\n")
            else:
                print()

    def phase_start(self, phase: int, name: str):
        """Announce phase start with colorful styling."""
        icon = PHASE_ICONS.get(phase, "▶")
        if RICH_AVAILABLE:
            color = PHASE_COLORS[(phase - 1) % len(PHASE_COLORS)]
            self.console.print()
            self.console.print(f"[bold {color}]{'━' * 3} {icon} PHASE {phase}: {name.upper()} {'━' * 3}[/]")
        else:
            print()
            print(f"=== {icon} PHASE {phase}: {name.upper()} ===")

    def phase_complete(self, phase: int, name: str):
        """Announce phase completion."""
        if RICH_AVAILABLE:
            self.console.print(f"[bold green]✓[/] Phase {phase} ({name}) complete")
        else:
            print(f"[OK] Phase {phase} ({name}) complete")

    def step(self, message: str):
        """Print a step message."""
        if RICH_AVAILABLE:
            self.console.print(f"  [cyan]→[/] {message}")
        else:
            print(f"  -> {message}")

    def success(self, message: str):
        """Print a success message."""
        if RICH_AVAILABLE:
            self.console.print(f"[bold green]✓[/] {message}")
        else:
            print(f"[OK] {message}")

    def warning(self, message: str):
        """Print a warning message."""
        if RICH_AVAILABLE:
            self.console.print(f"[bold yellow]⚠[/] {message}")
        else:
            print(f"[WARN] {message}")

    def error(self, message: str):
        """Print an error message."""
        if RICH_AVAILABLE:
            self.console.print(f"[bold red]✗ ERROR:[/] {message}")
        else:
            print(f"ERROR: {message}")

    def info(self, message: str):
        """Print an info message."""
        if RICH_AVAILABLE:
            self.console.print(f"[cyan]ℹ[/] {message}")
        else:
            print(f"[INFO] {message}")

    def intervention(self, issue: str, details: dict):
        """Show an intervention required panel."""
        if RICH_AVAILABLE:
            content = f"[red bold]{issue}[/]\n\n"
            content += "\n".join(f"  {k}: {v}" for k, v in details.items())
            self.console.print(Panel(
                content,
                title="⚠️  INTERVENTION REQUIRED",
                border_style="red"
            ))
        else:
            print()
            print("!!!! INTERVENTION REQUIRED !!!!")
            print(issue)
            for k, v in details.items():
                print(f"  {k}: {v}")
            print()

    def config_summary(self, config: dict):
        """Display configuration summary as a table."""
        if RICH_AVAILABLE:
            table = Table(show_header=False, box=None, padding=(0, 2))
            table.add_column("Key", style="dim cyan")
            table.add_column("Value", style="bold white")
            for key, value in config.items():
                if not key.startswith("_"):
                    display_value = self._truncate(str(value), 60)
                    table.add_row(key.replace("_", " ").title(), display_value)
            self.console.print(table)
        else:
            for key, value in config.items():
                if not key.startswith("_"):
                    display_value = self._truncate(str(value), 60)
                    print(f"  {key}: {display_value}")

    @contextmanager
    def show_loading(self, message: str):
        """Show animated spinner during loading."""
        if RICH_AVAILABLE:
            spinner = Spinner("dots", text=f"[cyan]{message}[/]")
            with Live(spinner, console=self.console, refresh_per_second=10, transient=True):
                yield
        else:
            print(f"{message}...")
            yield

    def agent_progress_start(self, total: int, description: str = "Spawning agents"):
        """Start agent progress tracking with enhanced visuals."""
        if RICH_AVAILABLE:
            self._progress = Progress(
                SpinnerColumn("dots", style="cyan"),
                TextColumn("[bold blue]{task.description}[/]"),
                BarColumn(complete_style="green", finished_style="bold green"),
                TaskProgressColumn(),
                TextColumn("[dim cyan]{task.fields[status]}[/]"),
                console=self.console,
                transient=False,
            )
            self._live = Live(self._progress, console=self.console, refresh_per_second=4)
            self._live.start()
            self._task_id = self._progress.add_task(description, total=total, status="starting...")
        else:
            print(f"{description} (0/{total})...")
            self._plain_total = total
            self._plain_completed = 0

    def agent_progress_update(self, completed: int, current: str = ""):
        """Update agent progress."""
        if RICH_AVAILABLE and self._progress and self._task_id is not None:
            self._progress.update(self._task_id, completed=completed, status=current[:40])
        else:
            self._plain_completed = completed
            if current:
                print(f"  [{completed}/{self._plain_total}] {current}")

    def agent_progress_complete(self, message: str = "Complete"):
        """Complete agent progress tracking."""
        if RICH_AVAILABLE and self._live:
            self._live.stop()
            self._progress = None
            self._live = None
            self._task_id = None
        self.success(message)

    def decision(self, iteration: int, sufficient: bool, reasoning: str):
        """Display decision agent result."""
        if RICH_AVAILABLE:
            status = "[bold green]SUFFICIENT[/]" if sufficient else "[bold yellow]NEEDS MORE[/]"
            self.console.print(f"\n[bold]Decision (iteration {iteration}):[/] {status}")
            self.console.print(f"  [dim]{reasoning}[/]")
        else:
            status = "SUFFICIENT" if sufficient else "NEEDS MORE"
            print(f"\nDecision (iteration {iteration}): {status}")
            print(f"  {reasoning}")

    def final_summary(self, report_dir: str, stats: dict):
        """Display final report summary."""
        if RICH_AVAILABLE:
            self.console.print()
            self.console.print(Panel(
                f"[bold green]REPORT COMPLETE[/]\n\n"
                f"[bold]Location:[/] {report_dir}\n"
                f"[bold]Report:[/] {report_dir}/report.md\n\n"
                + "\n".join(f"[dim]{k}:[/] {v}" for k, v in stats.items()),
                border_style="green",
                title="✨ Success"
            ))
        else:
            print()
            print("=" * 60)
            print("REPORT COMPLETE")
            print("=" * 60)
            print(f"Location: {report_dir}")
            print(f"Report: {report_dir}/report.md")
            for k, v in stats.items():
                print(f"  {k}: {v}")
            print("=" * 60)

    def report_picker(self, reports: list[dict]):
        """Display unfinished reports and let user pick one.

        Returns:
            Path to selected report, or None if cancelled
        """
        from pathlib import Path
        from datetime import datetime

        if not reports:
            self.info("No unfinished reports found")
            return None

        if RICH_AVAILABLE:
            from rich.box import ROUNDED

            # Build table
            table = Table(show_header=True, box=ROUNDED)
            table.add_column("#", style="dim", width=3)
            table.add_column("Topic", style="cyan")
            table.add_column("Phase", justify="center")
            table.add_column("Step", style="dim")
            table.add_column("Last Updated", style="dim")

            for i, report in enumerate(reports, 1):
                # Parse ISO timestamp
                updated = datetime.fromisoformat(report["updated_at"])
                time_ago = _format_time_ago(updated)

                table.add_row(
                    str(i),
                    self._truncate(report["topic"], 40),
                    f"{report['phase']}/5",
                    self._truncate(report["step"], 20),
                    time_ago
                )

            self.console.print()
            self.console.print(Panel(table, title="[bold]📋 Unfinished Reports[/]", border_style="blue"))
        else:
            print()
            print("=== Unfinished Reports ===")
            for i, report in enumerate(reports, 1):
                updated = datetime.fromisoformat(report["updated_at"])
                time_ago = _format_time_ago(updated)
                print(f"  [{i}] {report['topic'][:40]} - Phase {report['phase']}/5 - {time_ago}")
            print()

        # Use questionary to pick if available
        try:
            import questionary
            from questionary import Style

            custom_style = Style([
                ('qmark', 'fg:cyan bold'),
                ('question', 'fg:white bold'),
                ('answer', 'fg:#87d787 bold'),
                ('pointer', 'fg:cyan bold'),
                ('highlighted', 'fg:cyan bold'),
                ('selected', 'fg:#87d787'),
            ])

            choices = [f"{r['topic'][:40]} ({r['phase']}/5)" for r in reports]
            choices.append("Cancel")

            result = questionary.select(
                "Select report to resume:",
                choices=choices,
                style=custom_style
            ).ask()

            if result == "Cancel" or result is None:
                return None

            idx = choices.index(result)
            return Path(reports[idx]["path"])

        except ImportError:
            # Fallback to simple input
            while True:
                try:
                    choice = input(f"Select report (1-{len(reports)}, or 'q' to cancel): ").strip()
                    if choice.lower() == 'q':
                        return None
                    idx = int(choice) - 1
                    if 0 <= idx < len(reports):
                        return Path(reports[idx]["path"])
                    print(f"Please enter 1-{len(reports)}")
                except (ValueError, EOFError):
                    return None


    def report_picker_for_delete(self, reports: list[dict]):
        """Display all reports and let user pick one to delete.

        Returns:
            Path to selected report, or None if cancelled
        """
        from pathlib import Path
        from datetime import datetime

        if not reports:
            self.info("No reports in registry")
            return None

        if RICH_AVAILABLE:
            from rich.box import ROUNDED

            # Build table
            table = Table(show_header=True, box=ROUNDED)
            table.add_column("#", style="dim", width=3)
            table.add_column("Topic", style="cyan")
            table.add_column("Phase", justify="center")
            table.add_column("Status", style="dim")
            table.add_column("Last Updated", style="dim")

            for i, report in enumerate(reports, 1):
                updated = datetime.fromisoformat(report["updated_at"])
                time_ago = _format_time_ago(updated)
                status = "[green]Complete[/]" if report.get("complete") else "[yellow]In Progress[/]"

                table.add_row(
                    str(i),
                    self._truncate(report["topic"], 40),
                    f"{report['phase']}/5",
                    status,
                    time_ago
                )

            self.console.print()
            self.console.print(Panel(table, title="[bold]🗑️  Delete Report from Registry[/]", border_style="red"))
        else:
            print()
            print("=== Delete Report from Registry ===")
            for i, report in enumerate(reports, 1):
                updated = datetime.fromisoformat(report["updated_at"])
                time_ago = _format_time_ago(updated)
                status = "Complete" if report.get("complete") else "In Progress"
                print(f"  [{i}] {report['topic'][:40]} - Phase {report['phase']}/5 - {status} - {time_ago}")
            print()

        # Use questionary to pick
        try:
            import questionary
            from questionary import Style

            custom_style = Style([
                ('qmark', 'fg:red bold'),
                ('question', 'fg:white bold'),
                ('answer', 'fg:red bold'),
                ('pointer', 'fg:red bold'),
                ('highlighted', 'fg:red bold'),
                ('selected', 'fg:red'),
            ])

            choices = [f"{r['topic'][:40]} ({r['phase']}/5)" for r in reports]
            choices.append("Cancel")

            result = questionary.select(
                "Select report to DELETE from registry:",
                choices=choices,
                style=custom_style
            ).ask()

            if result == "Cancel" or result is None:
                return None

            idx = choices.index(result)
            return Path(reports[idx]["path"])

        except ImportError:
            while True:
                try:
                    choice = input(f"Select report to DELETE (1-{len(reports)}, or 'q' to cancel): ").strip()
                    if choice.lower() == 'q':
                        return None
                    idx = int(choice) - 1
                    if 0 <= idx < len(reports):
                        return Path(reports[idx]["path"])
                    print(f"Please enter 1-{len(reports)}")
                except (ValueError, EOFError):
                    return None


def _format_time_ago(dt) -> str:
    """Format datetime as '2 hours ago' style string."""
    from datetime import datetime

    now = datetime.now()
    diff = now - dt
    seconds = diff.total_seconds()

    if seconds < 60:
        return "just now"
    elif seconds < 3600:
        mins = int(seconds / 60)
        return f"{mins} min{'s' if mins > 1 else ''} ago"
    elif seconds < 86400:
        hours = int(seconds / 3600)
        return f"{hours} hour{'s' if hours > 1 else ''} ago"
    else:
        days = int(seconds / 86400)
        return f"{days} day{'s' if days > 1 else ''} ago"


# Global UI instance
ui = DeepReportUI()
