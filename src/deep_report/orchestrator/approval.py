#!/usr/bin/env python3
"""User approval gates for deep-report orchestrator.

Handles interactive approval before research runs and iterations.
CRITICAL: Only shows metadata, NEVER reads research content.
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
from .utils import RoleEnforcer


class ApprovalGate:
    """Handles user approval with metadata-only display.

    STRICT ROLE ISOLATION:
    The orchestrator NEVER reads:
      - full/agents/*.md (research content)
      - full/seeds/*.md (seed content)
      - Any file > 1000 chars except state/plan.md

    The orchestrator ONLY reads:
      - state/*.json (metadata)
      - File existence checks (Path.exists())
      - Word counts (len(file.read_text().split()))
      - First 200 chars of decision reasoning
    """

    def __init__(self, report_dir: Path, interactive: bool = False,
                 progress: Optional[ProgressWriter] = None):
        self.report_dir = Path(report_dir)
        self.interactive = interactive
        self.approval_file = self.report_dir / "state" / "pending_approval.json"
        self.progress = progress

    def request_approval(self, gate_id: str, metadata: dict) -> bool:
        """Request user approval. Blocks until approved or rejected.

        Args:
            gate_id: Identifier for this approval gate
            metadata: Dict of metadata to show user (NEVER content)

        Returns:
            True if approved, False if rejected
        """
        if not self.interactive:
            return True  # Auto-approve in non-interactive mode

        # Write approval request to file
        request = {
            "gate_id": gate_id,
            "metadata": metadata,
            "status": "pending",
            "requested_at": datetime.now().isoformat(),
        }
        try:
            self.approval_file.parent.mkdir(parents=True, exist_ok=True)
            self.approval_file.write_text(json.dumps(request, indent=2))
        except Exception:
            pass

        if self.progress:
            self.progress.approval_waiting(gate_id)

        # Display to user with Rich if available
        if RICH_AVAILABLE:
            console = Console()
            console.print()

            # Build metadata table
            table = Table(show_header=False, box=None, padding=(0, 2))
            table.add_column("Key", style="dim cyan")
            table.add_column("Value", style="white")

            for key, value in metadata.items():
                if isinstance(value, list):
                    if value:
                        value = "\n".join(f"• {v}" for v in value)
                    else:
                        value = "(none)"
                elif isinstance(value, dict):
                    value = json.dumps(value, indent=2)
                key_display = key.replace("_", " ").title()
                table.add_row(key_display, str(value))

            # Create panel with table
            console.print(Panel(
                table,
                title=f"[bold yellow]⏸ APPROVAL REQUIRED[/]",
                subtitle=f"[dim]{gate_id}[/]",
                border_style="yellow",
                padding=(1, 2)
            ))

            # Options
            console.print()
            console.print("[bold]Options:[/]")
            console.print("  [green]y[/] / [green]Enter[/]  Approve and continue")
            console.print("  [yellow]n[/]          Reject (skip this step)")
            console.print("  [red]q[/]          Quit orchestrator")
            console.print()

            try:
                response = console.input("[bold]Approve?[/] (y): ").strip().lower()
            except EOFError:
                console.print("\n[yellow]No input available, defaulting to reject[/]")
                response = 'n'
        else:
            # Fallback to plain text
            print(f"\n{'='*60}")
            print(f"APPROVAL REQUIRED: {gate_id}")
            print(f"{'='*60}")
            for key, value in metadata.items():
                if isinstance(value, (list, dict)):
                    value = json.dumps(value, indent=2)
                print(f"  {key}: {value}")
            print(f"{'='*60}")
            print("\nOptions:")
            print("  [y/Enter] Approve and continue")
            print("  [n]       Reject (skip this step)")
            print("  [q]       Quit orchestrator")
            print()

            try:
                response = input("Approve? [y/n/q]: ").strip().lower()
            except EOFError:
                print("\nNo input available, defaulting to reject")
                response = 'n'

        # Explicit handling of responses to avoid double approval on empty input
        if response == 'q':
            approved = False
        elif response in ('n', 'no'):
            approved = False
        elif response in ('y', 'yes', ''):
            approved = True
        else:
            # Treat unexpected input as rejection
            approved = False

        if response == 'q':
            request["status"] = "quit"
            request["responded_at"] = datetime.now().isoformat()
            self.approval_file.write_text(json.dumps(request, indent=2))
            if self.progress:
                self.progress.approval_received(gate_id, False)
            raise KeyboardInterrupt("User quit at approval gate")

        request["status"] = "approved" if approved else "rejected"
        request["responded_at"] = datetime.now().isoformat()
        self.approval_file.write_text(json.dumps(request, indent=2))

        if self.progress:
            self.progress.approval_received(gate_id, approved)

        return approved

    def get_research_metadata(self, state) -> dict:
        """Extract research metadata WITHOUT reading content.

        This method counts files and words but does NOT store or return
        the actual content. This is critical for role isolation.
        """
        full_dir = self.report_dir / "full" / "agents"

        # Count files and words without storing content
        completed_count = 0
        total_words = 0

        if full_dir.exists():
            for f in full_dir.glob("*.md"):
                completed_count += 1
                # Stream word count without loading full content
                total_words += RoleEnforcer.count_words_streaming(f)

        # Defensive checks for state attributes
        failed_threads = getattr(state, 'failed_threads', [])
        research_iteration = getattr(state, 'research_iteration', 0)
        max_iterations = getattr(state, 'max_iterations', 1)

        return {
            "completed_agents": completed_count,
            "failed_agents": len(failed_threads),
            "total_research_words": f"{total_words:,}",
            "iteration": research_iteration,
            "max_iterations": max_iterations,
        }

    def pre_research_gate(self, state) -> bool:
        """Approval gate before starting research."""
        # Defensive checks for state attributes
        threads = getattr(state, 'threads', [])
        research_model = getattr(state, 'research_model', 'unknown')
        estimated_cost = getattr(state, 'estimated_cost', 0.0)
        max_iterations = getattr(state, 'max_iterations', 1)

        metadata = {
            "threads_to_run": len(threads),
            "model": research_model,
            "estimated_cost": f"${estimated_cost:.2f}",
            "max_iterations": max_iterations,
        }
        return self.request_approval("pre_research", metadata)

    def iteration_gate(self, state, decision: dict, iteration: int) -> bool:
        """Approval gate before starting a new iteration."""
        # Count proposed follow-ups
        followup_count = (
            len(decision.get("gaps", [])) +
            len(decision.get("conflicts", [])) +
            len(decision.get("deepen", []))
        )

        # Get current research stats (metadata only)
        research_meta = self.get_research_metadata(state)

        metadata = {
            "current_iteration": iteration,
            "proposed_followups": followup_count,
            "decision_reasoning": decision.get("reasoning", "")[:200],
            "gaps": decision.get("gaps", []),
            "conflicts": decision.get("conflicts", []),
            "deepen": decision.get("deepen", []),
            **research_meta,
        }

        return self.request_approval(f"iteration_{iteration + 1}", metadata)

    def pre_synthesis_gate(self, state) -> bool:
        """Approval gate before synthesis (optional)."""
        research_meta = self.get_research_metadata(state)

        metadata = {
            "synthesis_strategy": "multi-pass" if research_meta["completed_agents"] > 10 else "single-pass",
            **research_meta,
        }

        return self.request_approval("pre_synthesis", metadata)
