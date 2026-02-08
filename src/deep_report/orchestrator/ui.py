#!/usr/bin/env python3
"""Rich terminal interface for deep-report orchestrator.

Provides colored output, progress bars, and formatted panels.
Falls back gracefully if Rich is not installed.
"""

from typing import Optional
import sys

# Try to import Rich, fall back to plain text if not available
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
    from rich.table import Table
    from rich.live import Live
    from rich.text import Text
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


class DeepReportUI:
    """Rich terminal interface for deep-report.

    Provides styled output with colors, progress bars, and panels.
    Falls back to plain text if Rich is not installed.
    """

    def __init__(self):
        if RICH_AVAILABLE:
            self.console = Console()
        else:
            self.console = None
        self._progress = None
        self._live = None

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
        if RICH_AVAILABLE:
            self.console.print()
            self.console.print(Panel(
                f"[bold white]{title}[/]\n{subtitle}" if subtitle else f"[bold white]{title}[/]",
                border_style="blue",
                padding=(0, 2)
            ))
        else:
            print()
            print("=" * 60)
            print(title)
            if subtitle:
                print(subtitle)
            print("=" * 60)

    def phase_start(self, phase: int, name: str):
        """Announce phase start."""
        if RICH_AVAILABLE:
            self.console.print()
            self.console.print(f"[bold blue]{'━' * 3} PHASE {phase}: {name.upper()} {'━' * 3}[/]")
        else:
            print()
            print(f"=== PHASE {phase}: {name.upper()} ===")

    def phase_complete(self, phase: int, name: str):
        """Announce phase completion."""
        if RICH_AVAILABLE:
            self.console.print(f"[green]✓ Phase {phase} ({name}) complete[/]")
        else:
            print(f"[OK] Phase {phase} ({name}) complete")

    def step(self, message: str):
        """Print a step message."""
        if RICH_AVAILABLE:
            self.console.print(f"  [dim]→[/] {message}")
        else:
            print(f"  -> {message}")

    def success(self, message: str):
        """Print a success message."""
        if RICH_AVAILABLE:
            self.console.print(f"[green]✓[/] {message}")
        else:
            print(f"[OK] {message}")

    def warning(self, message: str):
        """Print a warning message."""
        if RICH_AVAILABLE:
            self.console.print(f"[yellow]⚠[/] {message}")
        else:
            print(f"[WARN] {message}")

    def error(self, message: str):
        """Print an error message."""
        if RICH_AVAILABLE:
            self.console.print(f"[red bold]ERROR:[/] {message}")
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
            table.add_column("Key", style="dim")
            table.add_column("Value", style="bold")
            for key, value in config.items():
                if not key.startswith("_"):
                    table.add_row(key.replace("_", " ").title(), str(value))
            self.console.print(table)
        else:
            for key, value in config.items():
                if not key.startswith("_"):
                    print(f"  {key}: {value}")

    def agent_progress_start(self, total: int, description: str = "Spawning agents"):
        """Start agent progress tracking."""
        if RICH_AVAILABLE:
            self._progress = Progress(
                SpinnerColumn(),
                TextColumn("[bold blue]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                TextColumn("[dim]{task.fields[status]}"),
                console=self.console
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
            status = "[green]SUFFICIENT[/]" if sufficient else "[yellow]NEEDS MORE[/]"
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


# Global UI instance
ui = DeepReportUI()
