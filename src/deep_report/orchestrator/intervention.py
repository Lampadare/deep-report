#!/usr/bin/env python3
"""Async user intervention handler for deep-report orchestrator.

Handles structural failures that require user action (permissions, rate limits, etc).
"""

import json
from pathlib import Path
from datetime import datetime
from typing import Optional

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
        self.intervention_file.write_text(json.dumps(request, indent=2))

        if self.progress:
            self.progress.intervention_needed(issue)

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

        if response == 'q':
            request["status"] = "quit"
            request["responded_at"] = datetime.now().isoformat()
            self.intervention_file.write_text(json.dumps(request, indent=2))
            raise KeyboardInterrupt("User quit at intervention")

        if response == 's':
            request["status"] = "skipped"
            request["responded_at"] = datetime.now().isoformat()
            self.intervention_file.write_text(json.dumps(request, indent=2))
            return False

        # User wants to retry
        request["status"] = "retrying"
        request["responded_at"] = datetime.now().isoformat()
        self.intervention_file.write_text(json.dumps(request, indent=2))

        # Clear the intervention file on successful retry intent
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
