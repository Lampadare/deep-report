#!/usr/bin/env python3
"""Phase 2: Plan - Topic decomposition and research thread planning."""

import json
from pathlib import Path

from ..state import State
from ..utils import spawn_agent, extract_json
from ..ui import ui


# Cost estimates per 1K tokens (approximate)
COST_PER_1K = {
    "opus": {"input": 0.015, "output": 0.075},
    "sonnet": {"input": 0.003, "output": 0.015},
    "haiku": {"input": 0.00025, "output": 0.00125},
}


def run_plan(state: State) -> bool:
    """Run the planning phase.

    Decomposes the topic into research threads and estimates cost.

    Returns:
        True if planning succeeded, False otherwise
    """
    state.checkpoint("plan_started")

    report_dir = Path(state.report_dir)

    # Read scope document
    scope_file = report_dir / "state" / "scope.md"
    scope_content = ""
    if scope_file.exists():
        scope_content = scope_file.read_text()

    # Read seed summaries for context
    seed_context = _gather_seed_summaries(report_dir)

    # Generate research plan
    ui.step("Generating research plan")
    plan = _generate_plan(state, scope_content, seed_context)

    if not plan:
        ui.error("Failed to generate research plan")
        return False

    state.threads = plan["threads"]
    state.estimated_cost = plan["estimated_cost"]
    state.plan_written = True
    state.save()

    ui.plan_summary(state.threads)

    # Write plan to file
    plan_file = report_dir / "state" / "plan.md"
    try:
        _write_plan_file(state, plan, plan_file)
    except (OSError, PermissionError) as e:
        ui.error(f"Failed to write plan file: {e}")
        return False

    state.checkpoint("plan_written")
    state.mark_phase_complete(2)

    ui.info(f"{len(state.threads)} research threads planned")
    ui.info(f"Estimated cost: ${state.estimated_cost:.2f}")
    return True


def _gather_seed_summaries(report_dir: Path) -> str:
    """Gather all seed summaries into a single string."""
    summaries_dir = report_dir / "summaries" / "seeds"
    if not summaries_dir.exists():
        return ""

    context = []
    for f in summaries_dir.glob("*.md"):
        try:
            content = f.read_text()[:1500]
            context.append(f"### {f.stem}\n{content}")
        except (OSError, IOError) as e:
            ui.warning(f"Failed to read seed file {f.name}: {e}")

    return "\n\n".join(context)


def _generate_plan(state: State, scope: str, seed_context: str) -> dict | None:
    """Use an agent to generate the research plan."""

    # Use brief if available (detailed research instructions), otherwise topic
    research_instructions = state.brief or state.topic

    prompt = f"""You are a research planning agent. Create a research plan for this topic.

## Topic
{research_instructions}

## Report Type
{state.report_type}

## Expertise Level
{state.expertise_level}

## Number of Agents
{state.agent_count}

## Scope Document
{scope}

{f"## Background from Seeds{chr(10)}{seed_context}" if seed_context else ""}

## Task
Decompose this topic into {state.agent_count} distinct research threads. Each thread should:
- Cover a specific aspect/subtopic
- Be independent enough for parallel research
- Have clear boundaries to avoid overlap
- Be roughly equal in scope

Return a JSON object (and ONLY valid JSON, no other text):
{{
    "threads": [
        {{
            "id": "thread_1",
            "title": "Thread title",
            "objective": "What this thread investigates",
            "questions": ["Question 1?", "Question 2?", "Question 3?"]
        }}
    ],
    "coverage_notes": "Brief note on how threads cover the topic",
    "potential_gaps": ["Any areas that might need follow-up"]
}}

Generate exactly {state.agent_count} threads.
"""

    with ui.spinner_task("Planning agent working..."):
        result = spawn_agent(prompt, model="opus", timeout_secs=540, allowed_tools=["Read"])

    if not result.success:
        ui.error(f"Plan generation failed: {result.error}")
        return None

    # Parse JSON from output
    plan = extract_json(result.output)
    if not plan:
        ui.error("Failed to parse plan JSON")
        return None

    threads = plan.get("threads", [])
    if not threads:
        ui.error("Plan has no research threads")
        return None

    REQUIRED_THREAD_KEYS = {"id", "title", "objective"}
    valid_threads = []
    for t in threads:
        if REQUIRED_THREAD_KEYS.issubset(t.keys()):
            valid_threads.append(t)
        else:
            missing = REQUIRED_THREAD_KEYS - t.keys()
            ui.warning(f"Dropping malformed thread (missing {missing})")
    threads = valid_threads
    if not threads:
        ui.error("No valid research threads in plan")
        return None

    plan["threads"] = threads
    plan["estimated_cost"] = _estimate_cost(state, len(threads))
    return plan


def _estimate_cost(state: State, thread_count: int) -> float:
    """Estimate total cost for the research run."""
    research_model = state.research_model
    research_rates = COST_PER_1K.get(research_model, COST_PER_1K["sonnet"])
    opus_rates = COST_PER_1K["opus"]
    sonnet_rates = COST_PER_1K["sonnet"]

    # Estimates per agent (in 1K tokens)
    input_per_agent = 8  # Prompt + context
    output_per_agent = 6  # Research output

    # Phase 1: Setup - Scope writer (Opus)
    scope_cost = (5 * opus_rates["input"]) + (2 * opus_rates["output"])

    # Phase 2: Planning (Opus) - already ran, but include for total estimate
    plan_cost = (8 * opus_rates["input"]) + (3 * opus_rates["output"])

    # Phase 3: Research (user-selected model)
    research_input = thread_count * input_per_agent
    research_output = thread_count * output_per_agent
    research_cost = (research_input * research_rates["input"]) + (research_output * research_rates["output"])

    # Summarization (Sonnet)
    summary_input = thread_count * 6  # Reading outputs
    summary_output = thread_count * 1  # Summaries
    summary_cost = (summary_input * sonnet_rates["input"]) + (summary_output * sonnet_rates["output"])

    # Decision agent iterations (Opus, estimate 2 iterations)
    decision_cost = 2 * (4 * opus_rates["input"] + 1 * opus_rates["output"])

    # Phase 4: Synthesis (Opus, scale with thread count)
    if thread_count <= 10:
        synth_agents = 1
    elif thread_count <= 20:
        synth_agents = 3
    else:
        synth_agents = 5

    synth_input = synth_agents * 20  # Reading full outputs
    synth_output = synth_agents * 10  # Writing report sections
    synth_cost = (synth_input * opus_rates["input"]) + (synth_output * opus_rates["output"])

    # Header + conclusion writers (Opus)
    bookend_cost = 2 * (10 * opus_rates["input"] + 5 * opus_rates["output"])

    # Reference compiler (Sonnet)
    sonnet_rates = COST_PER_1K["sonnet"]
    refs_cost = (15 * sonnet_rates["input"]) + (3 * sonnet_rates["output"])

    # Audio (Opus, optional but include in estimate)
    audio_cost = 0
    if state.generate_audio:
        audio_cost = (20 * opus_rates["input"]) + (15 * opus_rates["output"])

    total = (scope_cost + plan_cost + research_cost + summary_cost +
             decision_cost + synth_cost + bookend_cost + refs_cost + audio_cost)

    # Add 20% buffer
    return round(total * 1.2, 2)


def _write_plan_file(state: State, plan: dict, plan_file: Path):
    """Write the plan to a markdown file."""
    threads = plan.get("threads", [])
    coverage = plan.get("coverage_notes", "")
    gaps = plan.get("potential_gaps", [])

    lines = [
        f"# Research Plan: {state.topic}",
        "",
        f"**Report Type:** {state.report_type}",
        f"**Expertise Level:** {state.expertise_level}",
        f"**Research Model:** {state.research_model}",
        f"**Agent Count:** {len(threads)}",
        f"**Estimated Cost:** ${state.estimated_cost:.2f}",
        "",
        "## Coverage Notes",
        coverage,
        "",
        "## Potential Gaps",
    ]

    for gap in gaps:
        lines.append(f"- {gap}")

    lines.extend(["", "## Research Threads", ""])

    for thread in threads:
        lines.append(f"### {thread['id']}: {thread['title']}")
        lines.append(f"**Objective:** {thread['objective']}")
        lines.append("")
        lines.append("**Questions:**")
        for q in thread.get("questions", []):
            lines.append(f"- {q}")
        lines.append("")

    try:
        plan_file.write_text("\n".join(lines))
    except (OSError, PermissionError) as e:
        raise OSError(f"Failed to write plan file: {e}") from e
