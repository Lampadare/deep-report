#!/usr/bin/env python3
"""Phase 3: Research - Iterative research with decision agent evaluation."""

import json
import threading
from pathlib import Path
from typing import Optional

from ..state import State
from ..utils import (
    spawn_agent,
    spawn_agents_parallel,
    spawn_decision_agent,
    AgentResult,
    AGENT_TOOLS,
    DEFAULT_TIMEOUT,
)
from ..approval import ApprovalGate
from ..progress import ProgressWriter
from ..ui import ui


def run_research(
    state: State,
    approval: Optional[ApprovalGate] = None,
    progress: Optional[ProgressWriter] = None,
) -> bool:
    """Run the research phase with iterative deepening.

    Spawns research agents, summarizes outputs, then uses a decision agent
    to determine if research is sufficient or needs to go deeper.

    Args:
        state: Orchestrator state
        approval: Optional approval gate for interactive mode
        progress: Optional progress writer for monitoring

    Returns:
        True if research succeeded, False otherwise
    """
    state.checkpoint("research_started")

    report_dir = Path(state.report_dir)

    # Load scope and plan context
    scope_content = _read_file(report_dir / "state" / "scope.md")

    # Get seed context for prompts
    seed_context = _gather_seed_context(report_dir)

    iteration = 0
    max_iterations = state.max_iterations

    # APPROVAL GATE: Before first research run
    if approval and state.research_iteration == 0:
        if not approval.pre_research_gate(state):
            ui.warning("Research cancelled by user")
            return False

    while iteration < max_iterations:
        iteration += 1
        state.research_iteration = iteration
        state.checkpoint(f"research_iteration_{iteration}")

        ui.info(f"Research Iteration {iteration}/{max_iterations}")
        if progress:
            progress.update(3, f"Iteration {iteration}/{max_iterations}", "starting")

        # Determine what to research this iteration
        if iteration == 1:
            # First iteration: run all initial threads
            threads_to_run = state.get_pending_threads()
        else:
            # Subsequent iterations: run follow-ups from decision agent
            threads_to_run = state.get_pending_followups()

        if not threads_to_run:
            ui.info("No threads to run, skipping iteration")
            break

        # Run research agents in parallel
        ui.step(f"Spawning {len(threads_to_run)} research agents")
        if progress:
            progress.update(3, f"Spawning agents", f"{len(threads_to_run)} agents")

        results = _run_research_batch(
            state, threads_to_run, scope_content, seed_context, iteration, progress
        )

        # Update state with completed/failed
        for thread_id, result in results.items():
            if result.success:
                if thread_id.startswith("followup_"):
                    _mark_followup_complete(state, thread_id, result)
                    if thread_id not in state.completed_threads:
                        state.completed_threads.append(thread_id)
                else:
                    if thread_id not in state.completed_threads:
                        state.completed_threads.append(thread_id)
                    state.update_thread(thread_id, status="completed", output_file=result.output_file)
            else:
                if thread_id.startswith("followup_"):
                    _mark_followup_failed(state, thread_id, result.error)
                else:
                    state.failed_threads.append(thread_id)
                    state.update_thread(thread_id, status="failed")
                ui.warning(f"Thread {thread_id} failed: {result.error[:80]}")
                if progress:
                    progress.error(3, f"Thread {thread_id}: {result.error[:100]}")

        state.save()
        state.checkpoint(f"research_batch_{iteration}_complete")

        # Summarize all new outputs
        ui.step("Summarizing research outputs")
        if progress:
            progress.update(3, "Summarizing", f"{len(results)} outputs")
        _summarize_outputs(state, results)
        state.checkpoint(f"summaries_{iteration}_complete")

        # Decision agent: should we go deeper?
        if iteration < max_iterations:
            ui.step("Evaluating research coverage")
            if progress:
                progress.update(3, "Decision agent", "evaluating coverage")

            summaries = _gather_all_summaries(report_dir)
            # Use brief if available (detailed research instructions), otherwise topic
            decision_topic = state.brief or state.topic
            with ui.spinner_task("Decision agent evaluating coverage..."):
                decision = spawn_decision_agent(
                    summaries=summaries,
                    topic=decision_topic,
                    iteration=iteration,
                    max_iterations=max_iterations,
                )

            ui.decision(
                iteration,
                decision.get('sufficient', True),
                decision.get('reasoning', 'N/A')
            )

            if progress:
                progress.decision(
                    iteration,
                    decision.get("sufficient", True),
                    decision.get("reasoning", "N/A")
                )

            if decision.get("sufficient", True):
                ui.success("Research deemed sufficient")
                break

            # APPROVAL GATE: Before each follow-up iteration
            if approval:
                if not approval.iteration_gate(state, decision, iteration):
                    ui.info("User stopped iterations, proceeding to synthesis")
                    break

            # Create follow-up threads from decision
            _create_followups(state, decision, iteration)

    state.checkpoint("research_complete")
    state.mark_phase_complete(3)

    completed = len(state.completed_threads)
    failed = len(state.failed_threads)
    ui.info(f"{completed} threads succeeded, {failed} failed")

    if progress:
        progress.update(3, "Complete", f"{completed} succeeded, {failed} failed")

    return completed > 0


def _read_file(path: Path) -> str:
    """Read file content or return empty string."""
    if path.exists():
        return path.read_text()
    return ""


def _gather_seed_context(report_dir: Path) -> str:
    """Gather seed summaries for research agent prompts."""
    summaries_dir = report_dir / "summaries" / "seeds"
    if not summaries_dir.exists():
        return ""

    context = []
    for f in summaries_dir.glob("*.md"):
        try:
            content = f.read_text()[:1000]
            context.append(f"[{f.stem}]: {content[:500]}...")
        except (OSError, IOError) as e:
            ui.warning(f"Failed to read seed context {f.name}: {e}")

    if context:
        return "\n\n## Background Context\n" + "\n\n".join(context)
    return ""


def _run_research_batch(
    state: State,
    threads: list[dict],
    scope: str,
    seed_context: str,
    iteration: int,
    progress: Optional[ProgressWriter] = None,
) -> dict[str, AgentResult]:
    """Run a batch of research agents in parallel."""

    report_dir = Path(state.report_dir)
    tasks = []

    for thread in threads:
        thread_id = thread.get("id", thread.get("focus", "unknown"))
        title = thread.get("title", thread.get("focus", "Research"))
        objective = thread.get("objective", thread.get("focus", ""))
        questions = thread.get("questions", [])

        output_file = report_dir / "full" / "agents" / f"{thread_id}.md"

        # Use brief if available (detailed research instructions), otherwise topic
        research_instructions = state.brief or state.topic

        prompt = _build_research_prompt(
            topic=research_instructions,
            title=title,
            objective=objective,
            questions=questions,
            scope=scope,
            seed_context=seed_context,
            expertise=state.expertise_level,
            report_type=state.report_type,
            iteration=iteration,
            output_file=output_file
        )

        tasks.append({
            "id": thread_id,
            "prompt": prompt,
            "model": state.research_model,
            "output_file": str(output_file),
            "timeout_secs": DEFAULT_TIMEOUT,
            "max_retries": 3,
            "allowed_tools": AGENT_TOOLS["research"],
        })

    # Build thread info for live table display
    thread_info = [{"id": t["id"], "title": t.get("title", t["id"])[:30]} for t in tasks]
    ui.research_table_start(thread_info, title=f"RESEARCH AGENTS (Iteration {iteration})")

    # Mark first batch as running (up to max_workers)
    max_workers = 10
    for t in thread_info[:max_workers]:
        ui.research_table_update(t["id"], "running")

    # Progress callback with thread-safe counter
    completed = [0]
    total = len(tasks)
    completed_lock = threading.Lock()

    def on_complete(task_id: str, result: AgentResult):
        with completed_lock:
            completed[0] += 1
            current = completed[0]

        # Update table status
        status = "complete" if result.success else "failed"
        ui.research_table_update(task_id, status, result.duration_secs)

        # Mark next pending thread as running (thread-safe via UI method)
        ui.research_table_mark_next_running()

        # Verbose output
        status_sym = "✓" if result.success else "✗"
        retries = f" [{result.retries} retries]" if result.retries > 0 else ""
        ui.verbose(f"[{current}/{total}] {task_id}: {status_sym}{retries}")
        ui.verbose(f"  Duration: {result.duration_secs:.1f}s")
        if not result.success:
            ui.verbose(f"  Error: {result.error[:200]}")

        if progress:
            progress.agent_complete(
                task_id, result.success, total, current,
                result.duration_secs, result.retries
            )

    results = spawn_agents_parallel(tasks, max_workers=max_workers, on_complete=on_complete)
    ui.research_table_complete()
    return results


def _build_research_prompt(
    topic: str,
    title: str,
    objective: str,
    questions: list[str],
    scope: str,
    seed_context: str,
    expertise: str,
    report_type: str,
    iteration: int,
    output_file: Path
) -> str:
    """Build the prompt for a research agent."""

    questions_text = "\n".join(f"- {q}" for q in questions) if questions else "- Explore the topic thoroughly"

    return f"""You are a research agent. Investigate this aspect of the larger topic.

## Main Topic / Research Brief
{topic}

## Your Assignment
**Title:** {title}
**Objective:** {objective}

## Key Questions to Answer
{questions_text}

## Scope and Context
{scope[:2000] if scope else "No scope document provided."}
{seed_context}

## Research Parameters
- Expertise level: {expertise}
- Report type: {report_type}
- This is iteration {iteration}

## Output Requirements
Write your findings to: {output_file}

Your output should be:
1. **Comprehensive**: 3,000-6,000 words of substantive content
2. **Evidence-based**: Cite specific studies, papers, data points
3. **Structured**: Use clear sections and headings
4. **Quantitative**: Include numbers, statistics, effect sizes where available
5. **Critical**: Note limitations, conflicts in the literature, open questions

## Format
```markdown
# {title}

## Executive Summary
[2-3 sentences]

## Key Findings
[Main content organized in sections]

## Data and Evidence
[Specific numbers, studies, citations]

## Limitations and Open Questions
[What we don't know, conflicts in literature]

## Sources
[List of key sources with URLs where available]
```

Use WebSearch and WebFetch to find authoritative sources. Prioritize:
- Peer-reviewed papers (PubMed, arXiv, Google Scholar)
- Official reports and documentation
- Expert analyses from reputable institutions
- Recent data (prefer last 3-5 years unless foundational)

CRITICAL: You MUST call Write tool with file_path="{output_file}" to save your research.
"""


def _summarize_outputs(state: State, results: dict[str, AgentResult]):
    """Summarize all successful research outputs in parallel.

    Reads content directly and passes to summarizer agents to avoid Read tool failures.
    """
    report_dir = Path(state.report_dir)
    summaries_dir = report_dir / "summaries" / "agents"

    tasks = []
    for thread_id, result in results.items():
        if not result.success:
            continue

        output_file = Path(result.output_file) if result.output_file else None
        if not output_file or not output_file.exists():
            continue

        # Read content here and pass directly to summarizer
        try:
            content = output_file.read_text()
        except Exception as e:
            ui.warning(f"Failed to read {output_file}: {e}")
            continue

        summary_file = summaries_dir / f"{thread_id}_summary.md"

        # Use brief if available (detailed research instructions), otherwise topic
        summary_topic = state.brief or state.topic

        prompt = f"""TASK: Summarize research output.

OUTPUT FILE: {summary_file}

<content>
{content}
</content>

Format (under 500 words):
- 5-10 bullet points of key findings
- Use [FINDING-HIGH/MEDIUM/LOW] confidence prefixes
- Include source citations
- Preserve quantitative data and specific claims

CRITICAL: You MUST call Write tool with file_path="{summary_file}" to save your summary.
"""

        tasks.append({
            "id": thread_id,
            "prompt": prompt,
            "model": "sonnet",
            "output_file": str(summary_file),
            "timeout_secs": 540,
            "allowed_tools": ["Write"],
        })

    if not tasks:
        return

    # Progress tracking for summarization
    ui.agent_progress_start(len(tasks), "Summarizing research")
    completed_count = [0]
    completed_lock = threading.Lock()

    def on_complete(task_id: str, result):
        with completed_lock:
            completed_count[0] += 1
            current = completed_count[0]
        status = "✓" if result.success else "✗"
        ui.agent_progress_update(current, f"{task_id}: {status}")
        ui.verbose(f"Summary {task_id}: {status}")

    summary_results = spawn_agents_parallel(tasks, max_workers=10, on_complete=on_complete)
    ui.agent_progress_complete(f"Summarized {len(tasks)} outputs")

    failed = [tid for tid, r in summary_results.items() if not r.success]
    if failed:
        ui.warning(f"Failed to summarize: {failed}")


def _gather_all_summaries(report_dir: Path) -> list[str]:
    """Gather all agent summaries for decision agent."""
    summaries_dir = report_dir / "summaries" / "agents"
    if not summaries_dir.exists():
        return []

    summaries = []
    for f in sorted(summaries_dir.glob("*_summary.md")):
        try:
            content = f.read_text()
            summaries.append(f"### {f.stem}\n{content}")
        except (OSError, IOError) as e:
            ui.warning(f"Failed to read summary {f.name}: {e}")

    return summaries


def _create_followups(state: State, decision: dict, iteration: int):
    """Create follow-up research threads from decision agent output."""

    followups = []

    # Gaps need new research
    for i, gap in enumerate(decision.get("gaps", [])):
        followups.append({
            "id": f"followup_{iteration}_gap_{i+1}",
            "reason": "gap",
            "focus": gap,
            "title": f"Gap: {gap[:50]}",
            "objective": f"Research gap: {gap}",
            "questions": [f"What do we know about {gap}?"],
            "parent_threads": [],
            "iteration": iteration + 1,
            "status": "pending",
        })

    # Conflicts need resolution
    for i, conflict in enumerate(decision.get("conflicts", [])):
        followups.append({
            "id": f"followup_{iteration}_conflict_{i+1}",
            "reason": "conflict",
            "focus": conflict,
            "title": f"Conflict resolution: {conflict[:40]}",
            "objective": f"Resolve conflicting findings: {conflict}",
            "questions": [f"What explains the conflict: {conflict}?"],
            "parent_threads": [],
            "iteration": iteration + 1,
            "status": "pending",
        })

    # Areas to deepen
    for i, area in enumerate(decision.get("deepen", [])):
        followups.append({
            "id": f"followup_{iteration}_deepen_{i+1}",
            "reason": "deepen",
            "focus": area,
            "title": f"Deep dive: {area[:50]}",
            "objective": f"Explore in more detail: {area}",
            "questions": [f"What are the details of {area}?"],
            "parent_threads": [],
            "iteration": iteration + 1,
            "status": "pending",
        })

    for fu in followups:
        state.add_followup(fu)

    if followups:
        ui.info(f"Created {len(followups)} follow-up threads for iteration {iteration + 1}")


def _mark_followup_complete(state: State, followup_id: str, result: AgentResult):
    """Mark a followup as completed."""
    for fu in state.followups:
        if fu.get("id") == followup_id:
            fu["status"] = "completed"
            fu["output_file"] = result.output_file
            state.save()
            return


def _mark_followup_failed(state: State, followup_id: str, error: str):
    """Mark a followup as failed."""
    for fu in state.followups:
        if fu.get("id") == followup_id:
            fu["status"] = "failed"
            fu["error"] = error
            state.save()
            return
