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
from .ui import ui
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

    # Gate types control which options are shown
    GATE_PROCEED_OR_QUIT = "proceed_or_quit"      # Enter=proceed, q=quit
    GATE_PROCEED_STOP_QUIT = "proceed_stop_quit"   # Enter=proceed, s=stop & synthesize, q=quit

    def request_approval(self, gate_id: str, metadata: dict,
                         gate_type: str = "proceed_or_quit") -> bool:
        """Request user approval. Blocks until approved or rejected.

        Args:
            gate_id: Identifier for this approval gate
            metadata: Dict of metadata to show user (NEVER content)
            gate_type: Controls option set — "proceed_or_quit" or "proceed_stop_quit"

        Returns:
            True if approved (proceed), False if stopped early (synthesize now)
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
        except Exception as e:
            ui.warning(f"Approval state saving failed: {e}")

        if self.progress:
            self.progress.approval_waiting(gate_id)

        is_iteration = gate_type == self.GATE_PROCEED_STOP_QUIT

        # Display to user with Rich if available
        while True:
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

                console.print(Panel(
                    table,
                    title=f"[bold yellow]⏸ APPROVAL REQUIRED[/]",
                    subtitle=f"[dim]{gate_id}[/]",
                    border_style="yellow",
                    padding=(1, 2)
                ))

                console.print()
                console.print("  [green]Enter[/]  Proceed")
                if is_iteration:
                    console.print("  [yellow]s[/]      Stop researching, synthesize now")
                console.print("  [red]q[/]      Quit (progress saved, resume later)")
                console.print()

                try:
                    prompt = "[bold]Proceed?[/] " + ("[Enter/s/q]: " if is_iteration else "[Enter/q]: ")
                    response = console.input(prompt).strip().lower()
                except EOFError:
                    ui.warning("No interactive input available — rejecting for safety")
                    response = 'q'
            else:
                print(f"\n{'='*60}")
                print(f"APPROVAL REQUIRED: {gate_id}")
                print(f"{'='*60}")
                for key, value in metadata.items():
                    if isinstance(value, (list, dict)):
                        value = json.dumps(value, indent=2)
                    print(f"  {key}: {value}")
                print(f"{'='*60}")
                print()
                print("  [Enter]  Proceed")
                if is_iteration:
                    print("  [s]      Stop researching, synthesize now")
                print("  [q]      Quit (progress saved, resume later)")
                print()

                try:
                    prompt = "Proceed? " + ("[Enter/s/q]: " if is_iteration else "[Enter/q]: ")
                    response = input(prompt).strip().lower()
                except EOFError:
                    ui.warning("No interactive input available — rejecting for safety")
                    response = 'q'

            if response == 'q':
                break
            elif response in ('s',) and is_iteration:
                break
            elif response == '':
                break
            else:
                if is_iteration:
                    ui.warning(f"Unrecognized input '{response}'. Options: Enter (proceed), s (stop & synthesize), q (quit, progress saved)")
                else:
                    ui.warning(f"Unrecognized input '{response}'. Options: Enter (proceed), q (quit, progress saved)")
                continue

        if response == 'q':
            request["status"] = "quit"
            request["responded_at"] = datetime.now().isoformat()
            try:
                self.approval_file.write_text(json.dumps(request, indent=2))
            except Exception:
                pass
            if self.progress:
                self.progress.approval_received(gate_id, False)
            raise KeyboardInterrupt("User quit at approval gate")

        approved = response != 's'

        request["status"] = "approved" if approved else "stopped_early"
        request["responded_at"] = datetime.now().isoformat()
        try:
            self.approval_file.write_text(json.dumps(request, indent=2))
        except Exception:
            pass

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
        """Approval gate before starting a new iteration.

        Shows coverage scores, numbered suggestions, and lets the user:
        - Enter: approve all suggestions
        - Comma-separated numbers: select specific suggestions
        - +text: add custom direction (combinable with numbers)
        - s: stop & synthesize
        - q: quit

        Mutates decision dict in-place to filter gaps/conflicts/deepen
        based on user selection.
        """
        if not self.interactive:
            return True

        research_meta = self.get_research_metadata(state)

        # Build numbered suggestion list
        suggestions = []
        for gap in decision.get("gaps", []):
            suggestions.append(("gap", gap))
        for conflict in decision.get("conflicts", []):
            suggestions.append(("conflict", conflict))
        for area in decision.get("deepen", []):
            suggestions.append(("deepen", area))

        coverage = decision.get("coverage")
        gate_id = f"iteration_{iteration + 1}"
        requested_at = datetime.now().isoformat()

        if self.progress:
            self.progress.approval_waiting(gate_id)

        # Write pending status immediately (so crash during input leaves a trace)
        self._save_gate_status(gate_id, "pending", requested_at)

        while True:
            if RICH_AVAILABLE:
                console = Console()
                console.print()

                # Coverage table
                if coverage and isinstance(coverage, dict):
                    cov_table = Table(show_header=True, box=None, padding=(0, 1))
                    cov_table.add_column("Area", style="white", min_width=20)
                    cov_table.add_column("Score", justify="center", width=7)
                    cov_table.add_column("Status", style="dim")
                    for area, info in coverage.items():
                        if not isinstance(info, dict):
                            continue
                        score = info.get("score", 0)
                        note = info.get("note", "")
                        if score >= 80:
                            ss = "bold green"
                        elif score >= 50:
                            ss = "bold yellow"
                        else:
                            ss = "bold red"
                        cov_table.add_row(area, f"[{ss}]{score}%[/]", str(note))
                    console.print(cov_table)
                    console.print()

                # Research stats
                stats = Table(show_header=False, box=None, padding=(0, 2))
                stats.add_column("Key", style="dim cyan")
                stats.add_column("Value", style="white")
                stats.add_row("Iteration", str(iteration))
                stats.add_row("Completed Agents", str(research_meta.get("completed_agents", 0)))
                stats.add_row("Total Words", str(research_meta.get("total_research_words", 0)))
                if decision.get("estimated_additional_cost"):
                    stats.add_row("Est. Additional Cost", decision["estimated_additional_cost"])
                console.print(stats)
                console.print()

                # Reasoning
                console.print(f"[bold]Reasoning:[/] {decision.get('reasoning', 'N/A')[:200]}")
                console.print()

                # Numbered suggestions
                if suggestions:
                    console.print("[bold]Proposed follow-ups:[/]")
                    type_colors = {"gap": "red", "conflict": "yellow", "deepen": "cyan"}
                    for i, (stype, text) in enumerate(suggestions, 1):
                        color = type_colors.get(stype, "white")
                        label = stype.upper()
                        console.print(f"  [bold]{i}.[/] [{color}][{label}][/] {text}")
                    console.print()

                # Options
                options_lines = ["[green]Enter[/]      Approve all follow-ups"]
                if suggestions:
                    options_lines.append("[cyan]1,3,5[/]     Select specific follow-ups")
                options_lines.append("[magenta]+text[/]     Add custom direction (e.g. 1,3,+my topic)")
                options_lines.append("[yellow]s[/]         Stop researching, synthesize now")
                options_lines.append("[red]q[/]         Quit (progress saved)")
                console.print(Panel(
                    "\n".join(options_lines),
                    title="[bold yellow]ITERATION GATE[/]",
                    subtitle=f"[dim]{gate_id}[/]",
                    border_style="yellow",
                    padding=(0, 2),
                ))

                try:
                    response = console.input("[bold]Choice:[/] ").strip()
                except EOFError:
                    ui.warning("No interactive input available — rejecting for safety")
                    response = "q"
            else:
                # Plain-text fallback
                print(f"\n{'='*60}")
                print(f"ITERATION GATE — iteration {iteration}")
                print(f"{'='*60}")

                if coverage and isinstance(coverage, dict):
                    print("\nCoverage:")
                    for area, info in coverage.items():
                        if isinstance(info, dict):
                            print(f"  {area}: {info.get('score', 0)}% — {info.get('note', '')}")

                print(f"\nIteration: {iteration}")
                print(f"Completed: {research_meta.get('completed_agents', 0)} agents, {research_meta.get('total_research_words', 0)} words")
                if decision.get("estimated_additional_cost"):
                    print(f"Est. cost: {decision['estimated_additional_cost']}")
                print(f"\nReasoning: {decision.get('reasoning', 'N/A')[:200]}")

                if suggestions:
                    print("\nProposed follow-ups:")
                    for i, (stype, text) in enumerate(suggestions, 1):
                        print(f"  {i}. [{stype.upper()}] {text}")

                if suggestions:
                    print(f"\n  Enter = approve all | 1,3,5 = select | +text = add custom")
                else:
                    print(f"\n  Enter = approve all | +text = add custom direction")
                print(f"  s = stop & synthesize | q = quit")

                try:
                    response = input("Choice: ").strip()
                except EOFError:
                    ui.warning("No interactive input available — rejecting for safety")
                    response = "q"

            # --- Handle response ---
            r = response.lower()
            if r == "q":
                self._save_gate_status(gate_id, "quit", requested_at)
                if self.progress:
                    self.progress.approval_received(gate_id, False)
                raise KeyboardInterrupt("User quit at approval gate")

            if r == "s":
                self._save_gate_status(gate_id, "stopped_early", requested_at)
                if self.progress:
                    self.progress.approval_received(gate_id, False)
                return False

            if r == "":
                # Approve all — no filtering
                self._save_gate_status(gate_id, "approved", requested_at)
                if self.progress:
                    self.progress.approval_received(gate_id, True)
                return True

            # Parse selection: numbers and +custom entries
            selected_indices = set()
            custom_directions = []
            valid = True

            for part in response.split(","):
                part = part.strip()
                if part.startswith("+"):
                    custom_text = part[1:].strip()
                    if custom_text:
                        custom_directions.append(custom_text)
                    else:
                        ui.warning("Empty custom direction ignored — use +your topic")
                        valid = False
                elif part:
                    try:
                        idx = int(part)
                        if not suggestions:
                            ui.warning("No suggestions available to select")
                            valid = False
                        elif 1 <= idx <= len(suggestions):
                            selected_indices.add(idx - 1)
                        else:
                            ui.warning(f"'{idx}' out of range (1-{len(suggestions)})")
                            valid = False
                    except ValueError:
                        ui.warning(f"Unrecognized input '{part}'. Use numbers, +text, s, or q")
                        valid = False

            if not valid:
                continue  # Re-prompt on any parse error

            # Filter decision dict in-place
            if selected_indices or custom_directions:
                new_gaps = []
                new_conflicts = []
                new_deepen = []

                for i, (stype, text) in enumerate(suggestions):
                    if i in selected_indices:
                        if stype == "gap":
                            new_gaps.append(text)
                        elif stype == "conflict":
                            new_conflicts.append(text)
                        elif stype == "deepen":
                            new_deepen.append(text)

                for custom in custom_directions:
                    new_deepen.append(custom)

                decision["gaps"] = new_gaps
                decision["conflicts"] = new_conflicts
                decision["deepen"] = new_deepen

            self._save_gate_status(gate_id, "approved", requested_at)
            if self.progress:
                self.progress.approval_received(gate_id, True)
            return True

    def _save_gate_status(self, gate_id: str, status: str,
                          requested_at: str = None):
        """Persist gate status to approval file."""
        request = {
            "gate_id": gate_id,
            "status": status,
        }
        if requested_at:
            request["requested_at"] = requested_at
        if status != "pending":
            request["responded_at"] = datetime.now().isoformat()
        try:
            self.approval_file.parent.mkdir(parents=True, exist_ok=True)
            self.approval_file.write_text(json.dumps(request, indent=2))
        except Exception:
            pass

    def pre_synthesis_gate(self, state) -> bool:
        """Approval gate before synthesis (optional)."""
        research_meta = self.get_research_metadata(state)

        metadata = {
            "synthesis_strategy": "multi-pass" if research_meta["completed_agents"] > 10 else "single-pass",
            **research_meta,
        }

        return self.request_approval("pre_synthesis", metadata)
