#!/usr/bin/env python3
"""User approval gates for deep-report orchestrator.

Handles interactive approval before research runs and iterations.
CRITICAL: Only shows metadata, NEVER reads research content.
"""

import json
import os
import queue
import time
from itertools import count
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional


def _utcnow_iso() -> str:
    """Timezone-aware ISO timestamp for cross-host driver correlation."""
    return datetime.now(timezone.utc).isoformat()


class _GateRejected(Exception):
    """Caller asked to reject the current request (not approve, not stop_early).
    Distinct from the False return value so iteration_gate can react differently
    from a user-issued stop_early."""

try:
    from rich.box import ROUNDED
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False
    ROUNDED = None

from .progress import ProgressWriter
from .ui import ui
from .utils import RoleEnforcer
from .utils.keyboard import KeyboardListener


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
                 progress: Optional[ProgressWriter] = None,
                 approval_mode: str = "stdin"):
        """
        Args:
            approval_mode: How to gather the user's decision.
                "stdin" — block on console.input() (default, current behavior)
                "file"  — write request to pending_approval.json and poll for a `response`
                          block. Used by --machine --interactive.
                "auto"  — short-circuit to approved without writing or asking. Used by
                          --machine alone and any non-interactive run.
        """
        self.report_dir = Path(report_dir)
        self.interactive = interactive
        self.approval_file = self.report_dir / "state" / "pending_approval.json"
        self.progress = progress
        self.approval_mode = approval_mode
        # Monotonic counter so the poll loop only accepts responses tagged to
        # the current request (prevents stale-response replay across iterations).
        self._request_seq = count(1)

    # Gate types control which options are shown
    GATE_PROCEED_OR_QUIT = "proceed_or_quit"      # Enter=proceed, q=quit
    GATE_PROCEED_STOP_QUIT = "proceed_stop_quit"   # Enter=proceed, s=stop & synthesize, q=quit

    def request_approval(self, gate_id: str, metadata: dict,
                         gate_type: str = "proceed_or_quit",
                         allow_feedback: bool = False) -> bool | str:
        """Request user approval. Blocks until approved or rejected.

        Args:
            gate_id: Identifier for this approval gate
            metadata: Dict of metadata to show user (NEVER content)
            gate_type: Controls option set — "proceed_or_quit" or "proceed_stop_quit"
            allow_feedback: If True, add 'f' option to provide feedback (returns str)

        Returns:
            True if approved, False if stopped early, or str with feedback text
        """
        if self.approval_mode == "auto" or not self.interactive:
            return True  # Auto-approve in non-interactive mode

        if self.approval_mode == "file":
            return self._wait_for_file_response(
                gate_id, metadata, gate_type, allow_feedback
            )

        # Write approval request to file
        request = {
            "gate_id": gate_id,
            "metadata": metadata,
            "status": "pending",
            "requested_at": _utcnow_iso(),
        }
        try:
            self.approval_file.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write_json(request)
        except Exception as e:
            ui.warning(f"Approval state saving failed: {e}")

        if self.progress:
            self.progress.approval_waiting(gate_id)

        is_iteration = gate_type == self.GATE_PROCEED_STOP_QUIT

        # Display to user with Rich if available. input_mode() stops the
        # persistent footer Live + verbose keyboard listener so console.input()
        # actually receives the user's keystrokes.
        with ui.input_mode():
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
                    if allow_feedback:
                        console.print("  [cyan]f[/]      Provide feedback to revise the plan")
                    if is_iteration:
                        console.print("  [yellow]s[/]      Stop researching, synthesize now")
                    console.print("  [red]q[/]      Quit (progress saved, resume later)")
                    console.print()

                    try:
                        opts = "[Enter"
                        if allow_feedback:
                            opts += "/f"
                        if is_iteration:
                            opts += "/s"
                        opts += "/q]: "
                        prompt = f"[bold]Proceed?[/] {opts}"
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
                    if allow_feedback:
                        print("  [f]      Provide feedback to revise the plan")
                    if is_iteration:
                        print("  [s]      Stop researching, synthesize now")
                    print("  [q]      Quit (progress saved, resume later)")
                    print()

                    try:
                        opts = "[Enter"
                        if allow_feedback:
                            opts += "/f"
                        if is_iteration:
                            opts += "/s"
                        opts += "/q]: "
                        prompt = f"Proceed? {opts}"
                        response = input(prompt).strip().lower()
                    except EOFError:
                        ui.warning("No interactive input available — rejecting for safety")
                        response = 'q'

                if response == 'q':
                    break
                elif response == 'f' and allow_feedback:
                    # Prompt for feedback text
                    try:
                        if RICH_AVAILABLE:
                            feedback = Console().input("[bold]Feedback:[/] ").strip()
                        else:
                            feedback = input("Feedback: ").strip()
                    except EOFError:
                        feedback = ""
                    if feedback:
                        request["status"] = "feedback"
                        request["feedback"] = feedback
                        request["responded_at"] = _utcnow_iso()
                        try:
                            self._atomic_write_json(request)
                        except Exception as e:
                            ui.warning(f"Approval state save failed: {e}")
                        return feedback
                    ui.warning("Empty feedback, try again")
                    continue
                elif response in ('s',) and is_iteration:
                    break
                elif response == '':
                    break
                else:
                    opts = "Enter (proceed)"
                    if allow_feedback:
                        opts += ", f (feedback)"
                    if is_iteration:
                        opts += ", s (stop & synthesize)"
                    opts += ", q (quit)"
                    ui.warning(f"Unrecognized input '{response}'. Options: {opts}")
                    continue

        if response == 'q':
            request["status"] = "quit"
            request["responded_at"] = _utcnow_iso()
            try:
                self._atomic_write_json(request)
            except Exception as e:
                ui.warning(f"Approval state save failed: {e}")
            if self.progress:
                self.progress.approval_received(gate_id, False)
            raise KeyboardInterrupt("User quit at approval gate")

        approved = response != 's'

        request["status"] = "approved" if approved else "stopped_early"
        request["responded_at"] = _utcnow_iso()
        try:
            self._atomic_write_json(request)
        except Exception as e:
            ui.warning(f"Approval state save failed: {e}")

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

    def pre_research_gate(self, state) -> bool | str:
        """Approval gate before starting research.

        Returns:
            True to proceed, str with feedback for re-planning.
            Raises KeyboardInterrupt on quit.
        """
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
        return self.request_approval("pre_research", metadata,
                                      allow_feedback=True)

    def iteration_gate(self, state, decision: dict, iteration: int) -> bool:
        """Approval gate before starting a new iteration.

        Renders a navigable table TUI of proposed follow-ups. Per row the user
        can toggle approve/deny and pick sonnet/opus. Confirming mutates the
        `decision` dict to keep only approved suggestions and adds parallel
        `*_with_model` lists that `_create_followups` uses.

        Returns True to proceed (with or without follow-ups), False to stop
        researching and proceed straight to synthesis. Raises KeyboardInterrupt
        on quit.
        """
        research_meta = self.get_research_metadata(state)
        sufficient = decision.pop("_sufficient", False)

        # Build initial row state
        default_model = getattr(state, "research_model", "sonnet") or "sonnet"
        rows = []
        for gap in decision.get("gaps", []):
            rows.append({"type": "gap", "text": gap, "approved": True, "model": default_model})
        for conflict in decision.get("conflicts", []):
            rows.append({"type": "conflict", "text": conflict, "approved": True, "model": default_model})
        for area in decision.get("deepen", []):
            rows.append({"type": "deepen", "text": area, "approved": True, "model": default_model})

        coverage = decision.get("coverage")
        gate_id = f"iteration_{iteration + 1}"
        requested_at = _utcnow_iso()

        # Machine-mode driven approval: skip the keyboard TUI, poll the file instead.
        # Approve = take all suggestions with default model. Stop_early = no follow-ups.
        if self.approval_mode == "file":
            metadata = {
                "iteration": iteration + 1,
                "sufficient": sufficient,
                "research_meta": research_meta,
                "suggestions": [
                    {"type": r["type"], "text": r["text"]} for r in rows
                ],
                "coverage": coverage,
            }
            result = self._wait_for_file_response(
                gate_id, metadata,
                gate_type=self.GATE_PROCEED_STOP_QUIT,
                allow_feedback=False,
            )
            if result is False:
                return False  # stop_early
            # Approve all suggestions with default model
            decision["gaps_with_model"] = [(r["text"], r["model"]) for r in rows if r["type"] == "gap"]
            decision["conflicts_with_model"] = [(r["text"], r["model"]) for r in rows if r["type"] == "conflict"]
            decision["deepen_with_model"] = [(r["text"], r["model"]) for r in rows if r["type"] == "deepen"]
            return True

        if self.progress:
            self.progress.approval_waiting(gate_id)

        # Write pending status immediately (so crash during input leaves a trace)
        self._save_gate_status(gate_id, "pending", requested_at)

        # Drive the TUI inside input_mode so the footer Live + verbose listener
        # don't fight us for the terminal.
        with ui.input_mode():
            action = self._run_iteration_tui(
                rows=rows,
                coverage=coverage,
                decision=decision,
                research_meta=research_meta,
                iteration=iteration,
                sufficient=sufficient,
                gate_id=gate_id,
                default_model=default_model,
            )

        if action == "quit":
            self._save_gate_status(gate_id, "quit", requested_at)
            if self.progress:
                self.progress.approval_received(gate_id, False)
            raise KeyboardInterrupt("User quit at approval gate")

        if action == "stop":
            self._save_gate_status(gate_id, "stopped_early", requested_at)
            if self.progress:
                self.progress.approval_received(gate_id, False)
            return False

        # action == "approve": mutate decision to keep only approved rows.
        new_gaps, new_conflicts, new_deepen = [], [], []
        gaps_wm, conflicts_wm, deepen_wm = [], [], []
        for row in rows:
            if not row["approved"]:
                continue
            entry = {"focus": row["text"], "model": row["model"]}
            if row["type"] == "gap":
                new_gaps.append(row["text"])
                gaps_wm.append(entry)
            elif row["type"] == "conflict":
                new_conflicts.append(row["text"])
                conflicts_wm.append(entry)
            else:  # deepen + custom both go in the deepen bucket
                new_deepen.append(row["text"])
                deepen_wm.append(entry)

        decision["gaps"] = new_gaps
        decision["conflicts"] = new_conflicts
        decision["deepen"] = new_deepen
        decision["gaps_with_model"] = gaps_wm
        decision["conflicts_with_model"] = conflicts_wm
        decision["deepen_with_model"] = deepen_wm

        self._save_gate_status(gate_id, "approved", requested_at)
        if self.progress:
            self.progress.approval_received(gate_id, True)
        return True

    def _run_iteration_tui(self, rows, coverage, decision, research_meta,
                            iteration, sufficient, gate_id, default_model):
        """Drive the navigable iteration gate.

        Returns one of: "approve", "stop", "quit". On "approve" the `rows`
        list reflects the user's final approve/model choices (in-place).
        """
        # Detect whether we can run the rich TUI; otherwise fall back.
        can_keyboard = False
        if RICH_AVAILABLE:
            probe = KeyboardListener(lambda c: None)
            can_keyboard = probe.available
        if not (RICH_AVAILABLE and can_keyboard):
            return self._iteration_tui_plain(
                rows, coverage, decision, research_meta,
                iteration, sufficient, gate_id, default_model,
            )

        console = Console()
        cursor = [0]
        key_q: "queue.Queue[str]" = queue.Queue()
        esc = {"state": 0}  # 0=none, 1=ESC, 2=ESC[

        def on_key(ch: str):
            if ch == "\x1b":
                esc["state"] = 1
                return
            if esc["state"] == 1:
                if ch == "[":
                    esc["state"] = 2
                    return
                esc["state"] = 0
                return
            if esc["state"] == 2:
                esc["state"] = 0
                if ch == "A":
                    key_q.put("UP")
                    return
                if ch == "B":
                    key_q.put("DOWN")
                    return
                return  # other CSI sequence, ignore
            key_q.put(ch)

        listener = KeyboardListener(on_key)

        type_colors = {"gap": "red", "conflict": "yellow", "deepen": "cyan", "custom": "magenta"}

        def build_display():
            items = []

            # Header: coverage + stats
            if coverage and isinstance(coverage, dict):
                cov = Table(show_header=True, box=None, padding=(0, 1))
                cov.add_column("Area", style="white", min_width=20)
                cov.add_column("Score", justify="center", width=7)
                cov.add_column("Status", style="dim")
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
                    cov.add_row(area, f"[{ss}]{score}%[/]", str(note))
                items.append(cov)

            approved = sum(1 for r in rows if r["approved"])
            stats = Text()
            stats.append(f"Iteration {iteration}", "white")
            stats.append("  ·  ", "dim")
            stats.append(f"{research_meta.get('completed_agents', 0)} agents", "white")
            stats.append("  ·  ", "dim")
            stats.append(f"{research_meta.get('total_research_words', 0)} words", "white")
            stats.append("  ·  ", "dim")
            stats.append(f"Approved {approved}/{len(rows)}", "cyan")
            if decision.get("estimated_additional_cost"):
                stats.append("  ·  ", "dim")
                stats.append(f"Est. (all): {decision['estimated_additional_cost']}", "dim")
            if sufficient:
                stats.append("  ·  ", "dim")
                stats.append("decision agent says sufficient", "dim yellow")
            items.append(stats)

            reasoning = decision.get("reasoning") or "N/A"
            items.append(Text(f"Reasoning: {reasoning[:300]}", style="white"))

            # Suggestion table
            table = Table(show_header=True, box=ROUNDED, padding=(0, 1), expand=True)
            table.add_column("#", width=3, justify="right", no_wrap=True)
            table.add_column("Type", width=10, no_wrap=True)
            table.add_column("OK", width=4, justify="center", no_wrap=True)
            table.add_column("Model", width=8, no_wrap=True)
            table.add_column("Suggestion", overflow="fold")
            for i, row in enumerate(rows):
                stype = row["type"]
                color = type_colors.get(stype, "white")
                label = f"[{color}]{stype.upper()}[/]"
                ok = "[green]✓[/]" if row["approved"] else "[red]✗[/]"
                mc = "magenta" if row["model"] == "opus" else "cyan"
                mdl = f"[{mc}]{row['model']}[/]"
                num = str(i + 1)
                text = row["text"]
                if i == cursor[0]:
                    items_style = "bold on grey23"
                    table.add_row(
                        f"[{items_style}]{num}[/]",
                        f"[{items_style}]{stype.upper()}[/]",
                        f"[{items_style}]{'✓' if row['approved'] else '✗'}[/]",
                        f"[{items_style}]{row['model']}[/]",
                        f"[{items_style}]{text}[/]",
                    )
                else:
                    table.add_row(num, label, ok, mdl, text)
            items.append(table)

            # Key hints
            hint = Text()
            hint.append("↑/↓ navigate  ", "dim cyan")
            hint.append("space toggle  ", "dim cyan")
            hint.append("s/o model  ", "dim cyan")
            hint.append("a/d all  ", "dim cyan")
            hint.append("S/O bulk model  ", "dim cyan")
            hint.append("+ custom  ", "dim magenta")
            if sufficient:
                # When the decision agent reports sufficient coverage, surface
                # the stop-and-synthesize path prominently.
                hint.append("Enter synthesize  ", "bold green")
                hint.append("c continue research  ", "dim yellow")
            else:
                hint.append("Enter confirm  ", "dim green")
                hint.append("x stop+synthesize  ", "dim yellow")
            hint.append("q quit", "dim red")
            items.append(Panel(
                hint,
                title="[bold yellow]ITERATION GATE[/]",
                subtitle=f"[dim]{gate_id}[/]",
                border_style="yellow",
                padding=(0, 1),
            ))

            return Group(*items)

        action = None
        listener.start()
        live = None
        try:
            live = Live(
                build_display(),
                console=console,
                refresh_per_second=10,
                screen=False,
                auto_refresh=False,
            )
            # refresh=True is required for the initial frame to render when
            # auto_refresh=False — without it Rich stores the renderable but
            # never draws it, so the first visible frame is whatever the user's
            # first keypress produces (looks like every action is one ahead).
            live.start(refresh=True)
            while action is None:
                try:
                    ch = key_q.get(timeout=0.1)
                except queue.Empty:
                    continue

                if ch in ("UP", "k"):
                    if rows:
                        cursor[0] = (cursor[0] - 1) % len(rows)
                elif ch in ("DOWN", "j"):
                    if rows:
                        cursor[0] = (cursor[0] + 1) % len(rows)
                elif ch == " ":
                    if rows:
                        rows[cursor[0]]["approved"] = not rows[cursor[0]]["approved"]
                elif ch == "s":
                    if rows:
                        rows[cursor[0]]["model"] = "sonnet"
                elif ch == "o":
                    if rows:
                        rows[cursor[0]]["model"] = "opus"
                elif ch == "a":
                    for r in rows:
                        r["approved"] = True
                elif ch == "d":
                    for r in rows:
                        r["approved"] = False
                elif ch == "S":
                    for r in rows:
                        if r["approved"]:
                            r["model"] = "sonnet"
                elif ch == "O":
                    for r in rows:
                        if r["approved"]:
                            r["model"] = "opus"
                elif ch in ("\r", "\n"):
                    # When the decision agent has already reported sufficient
                    # coverage, Enter defaults to "stop and synthesize" so the
                    # user doesn't accidentally pay for another iteration of
                    # research. The "c" key remains available to continue.
                    action = "stop" if sufficient else "approve"
                    break
                elif ch == "x":
                    # Explicit stop-and-synthesize on the not-yet-sufficient
                    # path. Mirrors the plain-fallback's "s" key.
                    if not sufficient:
                        action = "stop"
                        break
                elif ch == "c":
                    # Explicit "continue research" override on the sufficient
                    # path — the user wants more research despite the agent
                    # saying enough.
                    if sufficient:
                        action = "approve"
                        break
                elif ch == "q":
                    action = "quit"
                    break
                elif ch == "+":
                    # Stop Live + listener, prompt for custom direction, restart.
                    listener.stop()
                    live.stop()
                    try:
                        text = console.input("[bold magenta]Custom direction (Enter to cancel):[/] ").strip()
                    except EOFError:
                        text = ""
                    if text:
                        rows.append({
                            "type": "custom",
                            "text": text,
                            "approved": True,
                            "model": default_model,
                        })
                        cursor[0] = len(rows) - 1
                    live = Live(
                        build_display(),
                        console=console,
                        refresh_per_second=10,
                        screen=False,
                        auto_refresh=False,
                    )
                    live.start(refresh=True)
                    listener.start()
                    continue

                live.update(build_display(), refresh=True)
        finally:
            try:
                listener.stop()
            except Exception:
                pass
            if live is not None:
                try:
                    live.stop()
                except Exception:
                    pass

        return action or "quit"

    def _iteration_tui_plain(self, rows, coverage, decision, research_meta,
                              iteration, sufficient, gate_id, default_model):
        """Plain-text fallback when Rich or /dev/tty isn't available.

        Uses the original comma-list selection flow.
        """
        suggestions = [(r["type"], r["text"]) for r in rows]
        while True:
            print(f"\n{'='*60}")
            print(f"ITERATION GATE — iteration {iteration}")
            print(f"{'='*60}")

            if coverage and isinstance(coverage, dict):
                print("\nCoverage:")
                for area, info in coverage.items():
                    if isinstance(info, dict):
                        print(f"  {area}: {info.get('score', 0)}% — {info.get('note', '')}")

            print(f"\nIteration: {iteration}")
            print(f"Completed: {research_meta.get('completed_agents', 0)} agents, "
                  f"{research_meta.get('total_research_words', 0)} words")
            if decision.get("estimated_additional_cost"):
                print(f"Est. cost: {decision['estimated_additional_cost']}")
            print(f"\nReasoning: {decision.get('reasoning', 'N/A')[:200]}")

            if suggestions:
                print("\nProposed follow-ups:")
                for i, (stype, text) in enumerate(suggestions, 1):
                    print(f"  {i}. [{stype.upper()}] {text}")

            enter_label = "proceed to synthesis" if sufficient else "approve all"
            if suggestions:
                print(f"\n  Enter = {enter_label} | 1,3,5 = select | +text = add custom")
            else:
                print(f"\n  Enter = {enter_label} | +text = add custom direction")
            print(f"  s = stop & synthesize | q = quit")

            try:
                response = input("Choice: ").strip()
            except EOFError:
                ui.warning("No interactive input available — rejecting for safety")
                response = "q"

            r = response.lower()
            if r == "q":
                return "quit"
            if r == "s":
                return "stop"
            if r == "":
                if sufficient:
                    # Accept assessment: deny all and stop
                    for row in rows:
                        row["approved"] = False
                    return "stop"
                # Approve all
                for row in rows:
                    row["approved"] = True
                return "approve"

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
                continue

            # Apply selection to rows
            for i, row in enumerate(rows):
                row["approved"] = (i in selected_indices)
            for custom in custom_directions:
                rows.append({
                    "type": "custom",
                    "text": custom,
                    "approved": True,
                    "model": default_model,
                })
            return "approve"

    def _atomic_write_json(self, payload: dict):
        """Write JSON atomically: temp file + os.replace.

        Prevents readers from seeing a partial file mid-write. Raises if the
        write fails; callers MUST handle that — silently swallowing a failed
        approval write turns into a silent auto-approve of a paid gate.

        Tmp file name includes PID + a monotonic suffix so multiple writers
        sharing the same state directory (e.g., crash + resume, or two gates
        firing back-to-back) cannot clobber each other's tmp file.
        """
        self.approval_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.approval_file.with_suffix(
            f".tmp.{os.getpid()}.{time.monotonic_ns()}"
        )
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, self.approval_file)

    def _wait_for_file_response(self, gate_id: str, metadata: dict,
                                gate_type: str, allow_feedback: bool,
                                poll_secs: float = 2.0,
                                timeout_secs: float = 3600.0) -> bool | str:
        """Write a pending approval request and poll for a response block.

        Protocol:
          - Worker writes a request with `gate_id`, `request_seq` (monotonic),
            `status="pending"`, and an empty/absent `response`. Atomic publish.
          - Driver (skill or `deep-report --approve`) writes a `response` block
            into the same file with matching `gate_id` and `request_seq`.
          - Worker's poll only accepts responses whose `gate_id` AND `request_seq`
            match the current request AND whose `status != "resolved"`.

        Returns:
          True = approved
          False = stopped early (driver says "synthesize what we have")
          str = feedback text (only when allow_feedback)
          On timeout: emits a distinct approval_timeout event and returns False.

        Raises:
          KeyboardInterrupt — driver decision == "reject". Distinct from
            stop_early: reject means "abort, do not synthesize either",
            matching the interactive "q" key semantics.
        """
        seq = next(self._request_seq)
        request = {
            "gate_id": gate_id,
            "request_seq": seq,
            "metadata": metadata,
            "gate_type": gate_type,
            "allow_feedback": allow_feedback,
            "status": "pending",
            "requested_at": _utcnow_iso(),
            "response": None,  # explicit null — never inherit a prior gate's response
        }
        try:
            self._atomic_write_json(request)
        except Exception as e:
            # Could not serialize the request — do NOT auto-approve. An interactive
            # gate that cannot publish its request must fail closed.
            ui.error(f"Could not write approval request for gate '{gate_id}': {e}")
            if self.progress:
                self.progress.error(0, f"approval gate '{gate_id}' could not be written: {e}")
                self.progress.approval_received(gate_id, False)
            return False

        if self.progress:
            self.progress.approval_waiting(gate_id)

        try:
            return self._poll_for_response(
                gate_id, seq, allow_feedback, poll_secs, timeout_secs
            )
        except KeyboardInterrupt:
            # Persist a clear terminal state so the file isn't left "pending" forever.
            # If even this save fails (disk full, path deleted), surface it — a silent
            # failure here means the driver never learns the gate was interrupted.
            try:
                current = json.loads(self.approval_file.read_text())
                current["status"] = "interrupted"
                current["responded_at"] = _utcnow_iso()
                self._atomic_write_json(current)
            except Exception as e:
                ui.warning(f"Could not mark approval '{gate_id}' as interrupted: {e}")
                if self.progress:
                    self.progress.error(0, f"approval gate '{gate_id}' interrupted state save failed: {e}")
            if self.progress:
                self.progress.approval_received(gate_id, False)
            raise

    def _poll_for_response(self, gate_id: str, request_seq: int,
                           allow_feedback: bool, poll_secs: float,
                           timeout_secs: float) -> bool | str:
        """Inner poll loop. Extracted so KeyboardInterrupt handling is clean."""

        deadline = time.monotonic() + timeout_secs
        consecutive_missing = 0
        while time.monotonic() < deadline:
            try:
                current = json.loads(self.approval_file.read_text())
            except FileNotFoundError:
                # File was deleted (e.g., user wiped state). Surface clearly
                # after a few consecutive misses to avoid log spam on transient
                # races; recreating the request is the driver's responsibility.
                consecutive_missing += 1
                if consecutive_missing == 3:
                    ui.warning(
                        f"Approval file missing for gate '{gate_id}'; waiting…"
                    )
                time.sleep(poll_secs)
                continue
            except json.JSONDecodeError:
                # Mid-write race or tampered file — wait for the next tick.
                time.sleep(poll_secs)
                continue
            consecutive_missing = 0

            # Defensive checks: only accept responses bound to THIS request.
            if current.get("gate_id") != gate_id:
                time.sleep(poll_secs)
                continue
            if current.get("request_seq") != request_seq:
                time.sleep(poll_secs)
                continue
            if current.get("status") == "resolved":
                time.sleep(poll_secs)
                continue

            response = current.get("response")
            if response:
                decision = response.get("decision", "approve")
                feedback = response.get("feedback", "")
                approved = decision == "approve"
                if self.progress:
                    self.progress.approval_received(gate_id, approved)

                # Mark resolved so a stale read can't re-trigger us.
                current["status"] = "resolved"
                try:
                    self._atomic_write_json(current)
                except Exception as e:
                    ui.warning(f"Approval state save failed: {e}")

                if allow_feedback and feedback:
                    return feedback
                if decision == "stop_early":
                    return False
                if decision == "reject":
                    # Distinct from stop_early: reject = "abort, do not
                    # synthesize either". Mirrors the interactive 'q' key.
                    raise KeyboardInterrupt(
                        f"Driver rejected approval gate '{gate_id}'"
                    )
                return approved

            time.sleep(poll_secs)

        # Timeout — distinct from rejection. Emit a dedicated event so the
        # driver can tell "user walked away" from "user said no".
        ui.error(f"Approval gate '{gate_id}' timed out after {timeout_secs:.0f}s")
        if self.progress:
            self.progress.approval_timeout(gate_id, timeout_secs)
            self.progress.approval_received(gate_id, False)
        return False

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
            request["responded_at"] = _utcnow_iso()
        try:
            self.approval_file.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write_json(request)
        except Exception as e:
            ui.warning(f"Approval state save failed: {e}")

    def pre_synthesis_gate(self, state) -> bool:
        """Approval gate before synthesis (optional)."""
        research_meta = self.get_research_metadata(state)

        metadata = {
            "synthesis_strategy": "multi-pass" if research_meta["completed_agents"] > 10 else "single-pass",
            **research_meta,
        }

        return self.request_approval("pre_synthesis", metadata)
