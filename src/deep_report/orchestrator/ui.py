#!/usr/bin/env python3
"""Rich terminal interface for deep-report orchestrator.

Provides colored output, progress bars, spinners, and formatted panels.
Falls back gracefully if Rich is not installed.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import logging
import sys
import time

# Try to import Rich, fall back to plain text if not available
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
    from rich.table import Table
    from rich.box import ROUNDED
    from rich.live import Live
    from rich.text import Text
    from rich.spinner import Spinner
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False
    ROUNDED = None


@dataclass(frozen=True)
class Theme:
    accent: str = "cyan"
    success: str = "green"
    warning: str = "yellow"
    error: str = "red"
    dim: str = "dim"
    info: str = "cyan"
    heading: str = "bold white"
    phase_colors: tuple = ("cyan", "blue", "magenta", "green", "yellow")
    border: str = "cyan"

theme = Theme()


SPINNER_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
PHASE_ICONS = {
    1: "🔧",  # Setup
    2: "📋",  # Plan
    3: "🔬",  # Research
    4: "📝",  # Synthesize
    5: "✅",  # Cleanup
}


class _SpinnerTable:
    """Wrapper that rebuilds the research table on each Live refresh for spinner animation."""

    def __init__(self, builder):
        self._builder = builder

    def __rich_console__(self, console, options):
        yield self._builder()


class DeepReportUI:
    """Rich terminal interface for deep-report.

    Provides styled output with colors, progress bars, spinners, and panels.
    Falls back to plain text if Rich is not installed.
    """

    def __init__(self):
        if RICH_AVAILABLE:
            self.console = Console(force_terminal=None)
        else:
            self.console = None
        self._progress = None
        self._live = None
        self._task_id = None
        self._verbose = False
        # Plain mode progress tracking
        self._plain_total = 0
        self._plain_completed = 0
        # Research table state
        self._threads = []
        self._thread_status = {}
        self._thread_times = {}
        self._research_title = ""
        self._research_live = None
        self._spinner_frame = 0
        # Thread safety for _thread_status (RLock for reentrant access from Live refresh)
        import threading
        self._status_lock = threading.RLock()
        # Phase timing
        self._phase_start_time = None
        self._session_start_time = None
        # ETA tracking
        self._agent_durations = []

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
            if self._research_live and getattr(self._research_live, 'is_started', False):
                self._research_live.console.print(f"[{theme.dim}]{message}[/]")
            else:
                self.console.print(f"[{theme.dim}]{message}[/]")
        else:
            print(f"  [V] {message}")

    def _truncate(self, text: str, max_len: int = 60) -> str:
        """Truncate text to max_len, replacing newlines with spaces."""
        text = text.replace('\n', ' ').strip()
        if len(text) <= max_len:
            return text
        return text[:max_len - 3] + "..."

    @staticmethod
    def _picker_style(accent: str = "cyan"):
        """Return a questionary Style for picker dialogs."""
        from questionary import Style
        return Style([
            ('qmark', f'fg:{accent} bold'),
            ('question', 'fg:white bold'),
            ('answer', f'fg:#87d787 bold' if accent == 'cyan' else f'fg:{accent} bold'),
            ('pointer', f'fg:{accent} bold'),
            ('highlighted', f'fg:{accent} bold'),
            ('selected', f'fg:#87d787' if accent == 'cyan' else f'fg:{accent}'),
        ])

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

    def _update_title(self, text: str):
        """Update terminal window title."""
        if sys.stdout.isatty():
            sys.stdout.write(f"\033]0;{text}\007")
            sys.stdout.flush()

    def header(self, title: str, subtitle: str = ""):
        """Print a styled header."""
        display_title = self._truncate(title, 80)
        if RICH_AVAILABLE:
            self.console.print()
            self.console.print(Panel(
                f"[{theme.heading}]{display_title}[/]\n{subtitle}" if subtitle else f"[{theme.heading}]{display_title}[/]",
                border_style=theme.border,
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
            if sys.stdout.isatty():
                self.console.clear()
            self.console.print()
            # Minimal branding
            self.console.print(f"[bold {theme.accent}]  ╔═══════════════════╗[/]")
            self.console.print(f"[bold {theme.accent}]  ║   DEEP  REPORT    ║[/]")
            self.console.print(f"[bold {theme.accent}]  ╚═══════════════════╝[/]")
            self.console.print()
            title = Text("🔬 DEEP REPORT CONFIGURATION", style=theme.heading)
            self.console.print(Panel(
                title,
                border_style=theme.border,
                padding=(0, 2),
                subtitle=f"[{theme.dim}]Use ↑↓ arrows to navigate, Enter to select[/]"
            ))
        else:
            print("=" * 60)
            print("DEEP REPORT CONFIGURATION")
            print("Use Up/Down arrows to navigate, Enter to select")
            print("=" * 60)

    def section_divider(self, label: str = ""):
        """Print a subtle section divider."""
        if RICH_AVAILABLE:
            if label:
                self.console.print(f"\n[{theme.dim}]─── {label} ───[/]\n")
            else:
                self.console.print()
        else:
            if label:
                print(f"\n--- {label} ---\n")
            else:
                print()

    def phase_start(self, phase: int, name: str):
        """Announce phase start with colorful styling."""
        if self._session_start_time is None:
            self._session_start_time = time.time()
        self._phase_start_time = time.time()

        icon = PHASE_ICONS.get(phase, "▶")
        self._update_title(f"deep-report: Phase {phase}/5 — {name}")

        if RICH_AVAILABLE:
            color = theme.phase_colors[(phase - 1) % len(theme.phase_colors)]
            self.console.print()
            self.console.print(f"[bold {color}]{'━' * 3} {icon} PHASE {phase}: {name.upper()} {'━' * 3}[/]")
        else:
            print()
            print(f"=== {icon} PHASE {phase}: {name.upper()} ===")

    def phase_complete(self, phase: int, name: str):
        """Announce phase completion."""
        elapsed = ""
        if self._phase_start_time:
            secs = time.time() - self._phase_start_time
            if secs >= 60:
                elapsed = f" ({int(secs // 60)}m {int(secs % 60)}s)"
            else:
                elapsed = f" ({int(secs)}s)"

        if RICH_AVAILABLE:
            self.console.rule(style=theme.dim)
            self.console.print(f"[bold {theme.success}]✓[/] Phase {phase} ({name}) complete{elapsed}")
        else:
            print(f"--- Phase {phase} ({name}) complete{elapsed} ---")

        if phase == 5:
            self._update_title("deep-report: Complete")

    def phase_bar(self, completed_phase: int, total: int = 5):
        """Show persistent phase progress bar."""
        PHASE_NAMES = ["Setup", "Plan", "Research", "Synthesize", "Cleanup"]

        if RICH_AVAILABLE:
            parts = []
            for i, name in enumerate(PHASE_NAMES[:total], 1):
                if i <= completed_phase:
                    parts.append(f"[bold {theme.success}]✓ {name}[/]")
                elif i == completed_phase + 1:
                    parts.append(f"[bold {theme.accent}]▶ {name}[/]")
                else:
                    parts.append(f"[{theme.dim}]○ {name}[/]")
            bar = " → ".join(parts)
            self.console.print(f"\n{bar}\n")
        else:
            parts = []
            for i, name in enumerate(PHASE_NAMES[:total], 1):
                if i <= completed_phase:
                    parts.append(f"[✓ {name}]")
                elif i == completed_phase + 1:
                    parts.append(f"[> {name}]")
                else:
                    parts.append(f"[  {name}]")
            print(" → ".join(parts))

    def step(self, message: str):
        """Print a step message."""
        if RICH_AVAILABLE:
            self.console.print(f"  [{theme.accent}]→[/] {message}")
        else:
            print(f"  -> {message}")

    def success(self, message: str):
        """Print a success message."""
        if RICH_AVAILABLE:
            self.console.print(f"[bold {theme.success}]✓[/] {message}")
        else:
            print(f"[OK] {message}")

    def warning(self, message: str):
        """Print a warning message."""
        if RICH_AVAILABLE:
            self.console.print(f"[bold {theme.warning}]⚠[/] {message}")
        else:
            print(f"[WARN] {message}")

    def error(self, message: str):
        """Print an error message."""
        if RICH_AVAILABLE:
            self.console.print(f"[bold {theme.error}]✗ ERROR:[/] {message}")
        else:
            print(f"ERROR: {message}")

    def info(self, message: str):
        """Print an info message."""
        if RICH_AVAILABLE:
            self.console.print(f"[{theme.info}]ℹ[/] {message}")
        else:
            print(f"[INFO] {message}")

    def intervention(self, issue: str, details: dict):
        """Show an intervention required panel."""
        if RICH_AVAILABLE:
            content = f"[{theme.error} bold]{issue}[/]\n\n"
            content += "\n".join(f"  {k}: {v}" for k, v in details.items())
            self.console.print(Panel(
                content,
                title="⚠️  INTERVENTION REQUIRED",
                border_style=theme.error
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
            table.add_column("Key", style=f"{theme.dim} {theme.accent}")
            table.add_column("Value", style=theme.heading)
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
            spinner = Spinner("dots", text=f"[{theme.accent}]{message}[/]")
            with Live(spinner, console=self.console, refresh_per_second=10, transient=True):
                yield
        else:
            print(f"{message}...")
            yield

    def agent_progress_start(self, total: int, description: str = "Spawning agents"):
        """Start agent progress tracking with enhanced visuals."""
        if RICH_AVAILABLE:
            try:
                self._progress = Progress(
                    SpinnerColumn("dots", style=theme.accent),
                    TextColumn(f"[bold {theme.accent}]{{task.description}}[/]"),
                    BarColumn(complete_style=theme.success, finished_style=f"bold {theme.success}"),
                    TaskProgressColumn(),
                    TextColumn(f"[{theme.dim} {theme.accent}]{{task.fields[status]}}[/]"),
                    console=self.console,
                    transient=False,
                )
                self._live = Live(self._progress, console=self.console, refresh_per_second=4)
                self._live.start()
                self._task_id = self._progress.add_task(description, total=total, status="starting...")
            except Exception:
                print(f"{description} (0/{total})...")
                self._plain_total = total
                self._plain_completed = 0
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
        """Complete agent progress tracking. Safe to call multiple times."""
        if RICH_AVAILABLE and self._live:
            try:
                self._live.stop()
            except Exception:
                pass
            finally:
                self._progress = None
                self._live = None
                self._task_id = None
            self.success(message)

    @contextmanager
    def spinner_task(self, message: str):
        """Show spinner during a single long task."""
        if RICH_AVAILABLE:
            with self.console.status(f"[{theme.accent}]{message}[/]", spinner="dots"):
                yield
        else:
            print(f"  {message}...")
            yield

    def research_table_start(self, threads: list[dict], title: str = "RESEARCH AGENTS"):
        """Start live research thread table.

        Args:
            threads: List of {"id": "thread_1", "title": "Short description"}
            title: Table title
        """
        self._threads = threads
        self._thread_status = {t["id"]: "pending" for t in threads}
        self._thread_times = {}
        self._research_title = title

        if not RICH_AVAILABLE:
            print(f"\n{title}")
            return

        try:
            self._research_live = Live(
                _SpinnerTable(self._build_research_table),
                console=self.console,
                refresh_per_second=4
            )
            self._research_live.start()
        except Exception:
            print(f"\n{title}")

    def _build_research_table(self):
        """Build the research status table."""
        with self._status_lock:
            table = Table(show_header=True, box=ROUNDED)
            table.add_column("#", width=4, justify="right")
            table.add_column("Agent", width=32)
            table.add_column("Status", width=20)

            char = SPINNER_CHARS[self._spinner_frame % len(SPINNER_CHARS)]
            self._spinner_frame += 1

            for i, thread in enumerate(self._threads, 1):
                tid = thread["id"]
                title = self._truncate(thread.get("title", tid), 30)
                status = self._thread_status.get(tid, "pending")

                if status == "pending":
                    status_text = f"[{theme.dim}]○ Pending[/]"
                elif status == "running":
                    status_text = f"[{theme.accent}]{char} Running...[/]"
                elif status == "complete":
                    time_str = f" ({self._thread_times.get(tid, 0):.0f}s)" if tid in self._thread_times else ""
                    status_text = f"[{theme.success}]✓ Complete{time_str}[/]"
                elif status == "failed":
                    status_text = f"[{theme.error}]✗ Failed[/]"
                else:
                    status_text = f"[{theme.dim}]{status}[/]"

                table.add_row(str(i), title, status_text)

            complete = sum(1 for s in self._thread_status.values() if s == "complete")
            failed = sum(1 for s in self._thread_status.values() if s == "failed")
            running = sum(1 for s in self._thread_status.values() if s == "running")
            total = len(self._threads)
            pct = int(complete / total * 100) if total else 0
            durations_snapshot = list(self._agent_durations)
            research_title = self._research_title

        summary = f"Progress: {complete}/{total} ({pct}%)"

        # ETA from rolling average
        if durations_snapshot and complete < total:
            avg = sum(durations_snapshot) / len(durations_snapshot)
            remaining = total - complete - failed
            parallel = max(running, 1)
            eta_secs = (remaining / parallel) * avg
            if eta_secs >= 60:
                summary += f" | ETA: ~{int(eta_secs // 60)}m {int(eta_secs % 60)}s"
            else:
                summary += f" | ETA: ~{int(eta_secs)}s"

        if failed:
            summary += f" | [{theme.error}]✗ {failed} failed[/]"

        return Panel(table, title=f"[bold]🔬 {research_title}[/]",
                     subtitle=summary, border_style=theme.border)

    def research_table_update(self, thread_id: str, status: str, duration: float = 0):
        """Update a thread's status."""
        with self._status_lock:
            self._thread_status[thread_id] = status
            if duration:
                self._thread_times[thread_id] = duration
            if status == "complete" and duration:
                self._agent_durations.append(duration)

        # Live auto-refreshes via _SpinnerTable, but force an update for immediate feedback
        if RICH_AVAILABLE and self._research_live:
            try:
                self._research_live.update(_SpinnerTable(self._build_research_table))
            except Exception:
                logging.debug("research_table_update failed", exc_info=True)
        elif not RICH_AVAILABLE:
            symbol = "✓" if status == "complete" else "✗" if status == "failed" else "⠋"
            print(f"  [{symbol}] {thread_id}: {status}")

    def research_table_mark_next_running(self):
        """Mark the next pending thread as running (thread-safe)."""
        with self._status_lock:
            for tid, s in self._thread_status.items():
                if s == "pending":
                    self._thread_status[tid] = "running"
                    break
            else:
                return

    def cleanup_thread_metadata(self):
        """Clear thread metadata to free memory after a research phase."""
        with self._status_lock:
            self._threads = []
            self._thread_status = {}
            self._thread_times = {}
            self._research_title = ""
            self._spinner_frame = 0
            self._agent_durations = []

    def research_table_complete(self):
        """Finalize and close the live table. Safe to call multiple times."""
        if RICH_AVAILABLE and self._research_live:
            try:
                self._research_live.stop()
            except Exception:
                pass
            finally:
                self._research_live = None

            with self._status_lock:
                complete = sum(1 for s in self._thread_status.values() if s == "complete")
                failed = sum(1 for s in self._thread_status.values() if s == "failed")
                total = len(self._thread_status)

            if failed:
                self.warning(f"Research: {complete}/{total} succeeded, {failed} failed")
            else:
                self.success(f"Research: {complete}/{total} succeeded")

        self.cleanup_thread_metadata()

    def ensure_live_stopped(self):
        """Stop all live displays if running. Call from exception handlers."""
        if self._live:
            try:
                self._live.stop()
            except Exception:
                pass
            finally:
                self._progress = None
                self._live = None
                self._task_id = None
        if self._research_live:
            try:
                self._research_live.stop()
            except Exception:
                pass
            finally:
                self._research_live = None
        self.cleanup_thread_metadata()

    def decision(self, iteration: int, sufficient: bool, reasoning: str):
        """Display decision agent result."""
        if RICH_AVAILABLE:
            status = f"[bold {theme.success}]SUFFICIENT[/]" if sufficient else f"[bold {theme.warning}]NEEDS MORE[/]"
            self.console.print(f"\n[bold]Decision (iteration {iteration}):[/] {status}")
            self.console.print(f"  [{theme.dim}]{reasoning}[/]")
        else:
            status = "SUFFICIENT" if sufficient else "NEEDS MORE"
            print(f"\nDecision (iteration {iteration}): {status}")
            print(f"  {reasoning}")

    def plan_summary(self, threads: list[dict]):
        """Display research plan threads in a table."""
        if RICH_AVAILABLE:
            table = Table(show_header=True, box=ROUNDED)
            table.add_column("#", width=4, justify="right", style=theme.dim)
            table.add_column("Title", width=30)
            table.add_column("Objective", width=44)

            for i, thread in enumerate(threads, 1):
                title = self._truncate(thread.get("title", thread.get("id", "")), 28)
                objective = self._truncate(thread.get("objective", ""), 42)
                table.add_row(str(i), title, objective)

            self.console.print()
            self.console.print(Panel(
                table,
                title="[bold]📋 RESEARCH PLAN[/]",
                border_style=theme.border
            ))
        else:
            print("\n=== RESEARCH PLAN ===")
            for i, thread in enumerate(threads, 1):
                title = self._truncate(thread.get("title", thread.get("id", "")), 40)
                objective = self._truncate(thread.get("objective", ""), 50)
                print(f"  {i:2d}. {title}")
                print(f"      {objective}")
            print()

    def final_summary(self, report_dir: str, stats: dict):
        """Display final report summary."""
        elapsed = ""
        if self._session_start_time:
            secs = time.time() - self._session_start_time
            if secs >= 3600:
                elapsed = f"{int(secs // 3600)}h {int((secs % 3600) // 60)}m"
            elif secs >= 60:
                elapsed = f"{int(secs // 60)}m {int(secs % 60)}s"
            else:
                elapsed = f"{int(secs)}s"

        if RICH_AVAILABLE:
            self.console.print()
            content = f"[bold {theme.success}]Report generation complete![/]\n\n"
            content += f"[bold]📄 Report:[/] {report_dir}/report.md\n"
            content += f"[bold]📁 Directory:[/] {report_dir}\n"
            if elapsed:
                content += f"[bold]⏱  Total time:[/] {elapsed}\n"
            content += "\n"
            content += "\n".join(f"[{theme.dim}]{k}:[/] {v}" for k, v in stats.items())
            self.console.print(Panel(
                content,
                border_style=theme.success,
                title="✨ Success",
                padding=(1, 2)
            ))
            self._update_title("deep-report: Complete ✓")
        else:
            print()
            print("=" * 60)
            print("REPORT COMPLETE")
            if elapsed:
                print(f"Total time: {elapsed}")
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

            table = Table(show_header=True, box=ROUNDED)
            table.add_column("#", style=theme.dim, width=3)
            table.add_column("Topic", style=theme.accent)
            table.add_column("Phase", justify="center")
            table.add_column("Step", style=theme.dim)
            table.add_column("Last Updated", style=theme.dim)

            for i, report in enumerate(reports, 1):
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
            self.console.print(Panel(table, title="[bold]📋 Unfinished Reports[/]", border_style=theme.border))
        else:
            print()
            print("=== Unfinished Reports ===")
            for i, report in enumerate(reports, 1):
                updated = datetime.fromisoformat(report["updated_at"])
                time_ago = _format_time_ago(updated)
                print(f"  [{i}] {report['topic'][:40]} - Phase {report['phase']}/5 - {time_ago}")
            print()

        try:
            import questionary

            choices = [f"{r['topic'][:40]} ({r['phase']}/5)" for r in reports]
            choices.append("Cancel")

            result = questionary.select(
                "Select report to resume:",
                choices=choices,
                style=self._picker_style(theme.accent)
            ).ask()

            if result == "Cancel" or result is None:
                return None

            idx = choices.index(result)
            return Path(reports[idx]["path"])

        except ImportError:
            while True:
                try:
                    choice = input(f"Select report (1-{len(reports)}, or 'q' to cancel): ").strip()
                    if choice.lower() == 'q':
                        return None
                    idx = int(choice) - 1
                    if 0 <= idx < len(reports):
                        return Path(reports[idx]["path"])
                    print(f"Please enter 1-{len(reports)}")
                except ValueError:
                    print(f"Please enter a number 1-{len(reports)}")
                except EOFError:
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

            table = Table(show_header=True, box=ROUNDED)
            table.add_column("#", style=theme.dim, width=3)
            table.add_column("Topic", style=theme.accent)
            table.add_column("Phase", justify="center")
            table.add_column("Status", style=theme.dim)
            table.add_column("Last Updated", style=theme.dim)

            for i, report in enumerate(reports, 1):
                updated = datetime.fromisoformat(report["updated_at"])
                time_ago = _format_time_ago(updated)
                status = f"[{theme.success}]Complete[/]" if report.get("complete") else f"[{theme.warning}]In Progress[/]"

                table.add_row(
                    str(i),
                    self._truncate(report["topic"], 40),
                    f"{report['phase']}/5",
                    status,
                    time_ago
                )

            self.console.print()
            self.console.print(Panel(table, title="[bold]🗑️  Delete Report from Registry[/]", border_style=theme.error))
        else:
            print()
            print("=== Delete Report from Registry ===")
            for i, report in enumerate(reports, 1):
                updated = datetime.fromisoformat(report["updated_at"])
                time_ago = _format_time_ago(updated)
                status = "Complete" if report.get("complete") else "In Progress"
                print(f"  [{i}] {report['topic'][:40]} - Phase {report['phase']}/5 - {status} - {time_ago}")
            print()

        try:
            import questionary

            choices = [f"{r['topic'][:40]} ({r['phase']}/5)" for r in reports]
            choices.append("Cancel")

            result = questionary.select(
                "Select report to DELETE from registry:",
                choices=choices,
                style=self._picker_style(theme.error)
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
                except ValueError:
                    print(f"Please enter a number 1-{len(reports)}")
                except EOFError:
                    return None


def _format_time_ago(dt) -> str:
    """Format datetime as '2 hours ago' style string."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc) if dt.tzinfo else datetime.now()
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
