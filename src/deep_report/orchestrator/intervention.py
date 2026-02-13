#!/usr/bin/env python3
"""Async user intervention handler for deep-report orchestrator.

Handles structural failures that require user action (permissions, rate limits, etc).
"""

import json
from pathlib import Path
from datetime import datetime
from typing import Optional

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

from .progress import ProgressWriter


class InterventionHandler:
    """Handles async user intervention for structural failures."""

    def __init__(self, report_dir: Path, progress: Optional[ProgressWriter] = None):
        self.report_dir = Path(report_dir)
        self.intervention_file = self.report_dir / "state" / "intervention_needed.json"
        self.progress = progress

    def request_intervention(self, issue: str, details: dict,
                            suggested_fix: str = "") -> bool:
        """Request user intervention for a structural issue.

        Args:
            issue: Brief description of the issue
            details: Dict with error details
            suggested_fix: Suggested action for user to take

        Returns:
            True if user fixed and wants to retry
            False if user wants to skip/quit
        """
        request = {
            "issue": issue,
            "details": details,
            "suggested_fix": suggested_fix,
            "requested_at": datetime.now().isoformat(),
            "status": "pending",
        }
        self.intervention_file.parent.mkdir(parents=True, exist_ok=True)
        self.intervention_file.write_text(json.dumps(request, indent=2))

        if self.progress:
            self.progress.intervention_needed(issue)

        if RICH_AVAILABLE:
            console = Console()
            console.print()

            # Build details table
            table = Table(show_header=False, box=None, padding=(0, 2))
            table.add_column("Key", style="dim cyan")
            table.add_column("Value", style="white")

            table.add_row("Issue", f"[bold red]{issue}[/]")
            for key, value in details.items():
                key_display = key.replace("_", " ").title()
                table.add_row(key_display, str(value))

            if suggested_fix:
                table.add_row("Suggested Fix", f"[green]{suggested_fix}[/]")

            table.add_row("Intervention File", f"[dim]{self.intervention_file}[/]")

            console.print(Panel(
                table,
                title="[bold red]⚠ INTERVENTION REQUIRED[/]",
                border_style="red",
                padding=(1, 2)
            ))

            console.print()
            console.print("[bold]Options:[/]")
            console.print("  [green]r[/] / [green]Enter[/]  Retry after fixing the issue")
            console.print("  [yellow]s[/]          Skip this task and continue")
            console.print("  [red]q[/]          Quit orchestrator")
            console.print()

            try:
                response = console.input("[bold]Action?[/] (r): ").strip().lower()
            except EOFError:
                console.print("\n[yellow]No input available, defaulting to skip[/]")
                response = 's'
        else:
            print(f"\n{'!'*60}")
            print(f"INTERVENTION REQUIRED")
            print(f"{'!'*60}")
            print(f"\nIssue: {issue}")
            print(f"\nDetails:")
            for key, value in details.items():
                print(f"  {key}: {value}")

            if suggested_fix:
                print(f"\nSuggested fix: {suggested_fix}")

            print(f"\nIntervention file: {self.intervention_file}")
            print(f"\n{'!'*60}")
            print("\nOptions:")
            print("  [r/Enter] Retry after fixing the issue")
            print("  [s]       Skip this task and continue")
            print("  [q]       Quit orchestrator")
            print()

            try:
                response = input("Action? [r/s/q]: ").strip().lower()
            except EOFError:
                print("\nNo input available, defaulting to skip")
                response = 's'

        # KeyboardInterrupt is used for quit to halt execution cleanly
        if response == 'q':
            request["status"] = "quit"
            request["responded_at"] = datetime.now().isoformat()
            self.intervention_file.write_text(json.dumps(request, indent=2))
            raise KeyboardInterrupt("User quit at intervention")

        # Explicit fallthrough: skip takes precedence over retry
        if response == 's':
            request["status"] = "skipped"
            request["responded_at"] = datetime.now().isoformat()
            self.intervention_file.write_text(json.dumps(request, indent=2))
            return False

        # User wants to retry (r or Enter)
        # Remove file on retry to signal completion
        self.intervention_file.unlink(missing_ok=True)
        return True

    def check_rate_limit(self, error: str) -> bool:
        """Handle rate limit errors specifically."""
        return self.request_intervention(
            issue="API Rate Limit Reached",
            details={"error": error},
            suggested_fix="Wait a few minutes for rate limit to reset, then press Enter to retry"
        )

    def check_permission(self, error: str, path: str = "") -> bool:
        """Handle permission errors."""
        return self.request_intervention(
            issue="Permission Denied",
            details={"error": error, "path": path},
            suggested_fix=f"Check file/directory permissions for: {path}"
        )

    def check_api_key(self, error: str) -> bool:
        """Handle API key issues."""
        return self.request_intervention(
            issue="API Key Issue",
            details={"error": error},
            suggested_fix="Check your ANTHROPIC_API_KEY environment variable"
        )

    def check_billing(self, error: str) -> bool:
        """Handle billing/quota issues."""
        return self.request_intervention(
            issue="Billing or Quota Issue",
            details={"error": error},
            suggested_fix="Check your Anthropic account billing status"
        )

    def categorize_and_handle(self, error: str, context: dict = None) -> bool:
        """Categorize an error and request appropriate intervention.

        Returns:
            True if user wants to retry
            False if user wants to skip
        """
        error_lower = error.lower()
        context = context or {}

        if "rate limit" in error_lower:
            return self.check_rate_limit(error)

        if "permission denied" in error_lower:
            return self.check_permission(error, context.get("path", ""))

        if "api key" in error_lower or "unauthorized" in error_lower:
            return self.check_api_key(error)

        if "billing" in error_lower or "quota" in error_lower:
            return self.check_billing(error)

        # Generic intervention
        return self.request_intervention(
            issue="Unexpected Error",
            details={"error": error, **context},
            suggested_fix="Review the error and fix the underlying issue"
        )
