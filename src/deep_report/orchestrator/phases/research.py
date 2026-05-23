#!/usr/bin/env python3
"""Phase 3: Research - Iterative research with decision agent evaluation."""

import threading
from pathlib import Path
from typing import Optional

from ..state import State
from ..utils import (
    spawn_agents_parallel,
    spawn_decision_agent,
    generate_mcp_config,
    extend_allowed_tools_for_imports,
    AgentResult,
    AGENT_TOOLS,
    DEFAULT_TIMEOUT,
    DECISION_TIMEOUT,
)
from ..approval import ApprovalGate
from ..progress import ProgressWriter
from ..ui import ui


def run_research(
    state: State,
    approval: Optional[ApprovalGate] = None,
    progress: Optional[ProgressWriter] = None,
    intervention_handler: Optional[object] = None,
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
    state.current_phase = 3
    state.checkpoint("research_started")

    report_dir = Path(state.report_dir)

    # Load scope and plan context
    scope_content = _read_file(report_dir / "state" / "scope.md")

    # Get seed context for prompts
    seed_context = _gather_seed_context(report_dir)

    iteration = state.research_iteration  # Resume from saved iteration
    max_iterations = state.max_iterations

    # APPROVAL GATE: Before first research run (with feedback loop)
    # Skip on resume when threads are already completed
    if approval and state.research_iteration == 0 and not state.completed_threads:
        while True:
            gate_result = approval.pre_research_gate(state)
            if gate_result is True:
                break
            elif isinstance(gate_result, str):
                from .plan import replan_with_feedback
                if not replan_with_feedback(state, gate_result):
                    ui.error("Re-planning failed, proceeding with existing plan")
                    break
                continue
            else:
                ui.warning("Research cancelled by user")
                return False

    while iteration < max_iterations:
        iteration += 1

        ui.info(f"Research Iteration {iteration}/{max_iterations}")
        if progress:
            progress.update(3, f"Iteration {iteration}/{max_iterations}", "starting")

        # Determine what to research: pending initial threads first, then followups
        threads_to_run = state.get_pending_threads()
        if not threads_to_run and iteration > 1:
            threads_to_run = state.get_pending_followups()

        if not threads_to_run:
            if state.completed_threads:
                # All threads done for this iteration — skip to decision agent
                ui.info(f"{len(state.completed_threads)} threads completed — evaluating coverage")
            else:
                ui.info("No threads to run, ending research")
                break
        else:
            # Run research agents in parallel
            ui.step(f"Spawning {len(threads_to_run)} research agents")
            if progress:
                progress.update(3, f"Spawning agents", f"{len(threads_to_run)} agents")

            results = _run_research_batch(
                state, threads_to_run, scope_content, seed_context, iteration, progress,
                intervention_handler=intervention_handler,
            )

            # on_complete already updated state + saved incrementally
            # Persist iteration number only after batch succeeds (not before,
            # so an interrupted batch doesn't skip the iteration on resume)
            state.research_iteration = iteration
            state.checkpoint(f"research_batch_{iteration}_complete")

        # Summarize all unsummarized outputs (including from prior runs).
        # Runs unconditionally so resumed runs with completed-but-unsummarized
        # threads still get summaries before the decision agent.
        ui.step("Summarizing research outputs")
        if progress:
            progress.update(3, "Summarizing", "checking for unsummarized outputs")
        _summarize_outputs(state)
        state.checkpoint(f"summaries_{iteration}_complete")

        # Decision agent: should we go deeper?
        if iteration < max_iterations:
            ui.step("Evaluating research coverage")
            if progress:
                progress.update(3, "Decision agent", "evaluating coverage")

            summaries = _gather_all_summaries(report_dir)
            # Use brief if available (detailed research instructions), otherwise topic
            decision_topic = state.brief or state.topic
            with ui.spinner_task(f"Decision agent evaluating coverage (up to {DECISION_TIMEOUT // 60} min)..."):
                decision = spawn_decision_agent(
                    summaries=summaries,
                    topic=decision_topic,
                    iteration=iteration,
                    max_iterations=max_iterations,
                )

            sufficient = decision.get("sufficient", True)

            ui.decision(
                iteration,
                sufficient,
                decision.get('reasoning', 'N/A'),
                coverage=decision.get('coverage'),
            )

            if progress:
                progress.decision(
                    iteration,
                    sufficient,
                    decision.get("reasoning", "N/A")
                )

            if approval and approval.interactive:
                # Interactive mode: ALWAYS show gate, let user decide
                followup_count = (
                    len(decision.get("gaps", []))
                    + len(decision.get("conflicts", []))
                    + len(decision.get("deepen", []))
                )
                if state.completed_threads:
                    avg_cost = state.total_cost / len(state.completed_threads)
                else:
                    avg_cost = 0.50
                if followup_count > 0:
                    estimated_additional = avg_cost * followup_count
                    decision["estimated_additional_cost"] = f"~${estimated_additional:.2f} ({followup_count} threads)"
                    ui.info(f"Estimated additional cost: ~${estimated_additional:.2f} for {followup_count} follow-up threads (running total: ${state.total_cost:.2f}, excludes third-party API costs)")

                decision["_sufficient"] = sufficient
                if not approval.iteration_gate(state, decision, iteration):
                    ui.info("Proceeding to synthesis")
                    break

                # If gate approved but no follow-ups remain (user pressed Enter on
                # sufficient assessment, or selected nothing), stop researching
                has_followups = (
                    decision.get("gaps") or decision.get("conflicts") or decision.get("deepen")
                )
                if not has_followups:
                    ui.success("No follow-up directions — proceeding to synthesis")
                    break
            else:
                # Non-interactive: respect decision agent's assessment
                if sufficient:
                    ui.success("Research deemed sufficient")
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
    for f in sorted(summaries_dir.glob("*.md")):
        try:
            content = f.read_text().strip()
            context.append(f"[{f.stem}]: {content}")
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
    intervention_handler: Optional[object] = None,
) -> dict[str, AgentResult]:
    """Run a batch of research agents in parallel."""

    report_dir = Path(state.report_dir)

    # Generate MCP config once per batch; select tool preset accordingly.
    # Catalog tools come from AGENT_TOOLS; CC-imported MCPs need a wildcard
    # `mcp__<name>` allow entry tacked on at spawn time, otherwise the strict
    # allowedTools allowlist denies every call into the imported server.
    mcp_config = generate_mcp_config(report_dir)
    if mcp_config:
        tool_preset = extend_allowed_tools_for_imports(AGENT_TOOLS["research"])
        ui.verbose(f"MCP config written to {mcp_config}")
    else:
        tool_preset = AGENT_TOOLS["research_fallback"]
        ui.verbose("No MCP API keys found — using WebSearch/WebFetch fallback")

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
            output_file=output_file,
            use_mcp=mcp_config is not None,
        )

        task_model = thread.get("model") or state.research_model
        tasks.append({
            "id": thread_id,
            "title": title,
            "prompt": prompt,
            "model": task_model,
            "output_file": str(output_file),
            "timeout_secs": DEFAULT_TIMEOUT,
            "max_retries": 3,
            "allowed_tools": tool_preset,
            "mcp_config": str(mcp_config) if mcp_config else None,
        })

    # Build thread info for live table display
    thread_info = [{"id": t["id"], "title": t.get("title", t["id"])} for t in tasks]
    # Cap concurrency to avoid API OTPM rate limits.
    # Opus OTPM limits: Tier1=8K, Tier2=90K, Tier3=160K, Tier4=400K.
    # Each agent request reserves max_tokens (~8-64K) of OTPM upfront.
    # Safe defaults: opus→3 concurrent, sonnet→8 concurrent.
    model = state.research_model
    if model == "opus":
        max_workers = min(len(tasks), 5)
    else:
        max_workers = min(len(tasks), 5)
    concurrent_note = f" — {max_workers} concurrent" if len(tasks) > max_workers else ""
    ui.research_table_start(thread_info, title=f"RESEARCH AGENTS (Iteration {iteration}){concurrent_note}")

    # Mark first batch as running (up to max_workers).
    for t in thread_info[:max_workers]:
        ui.research_table_update(t["id"], "running")
    # Mark remaining as queued so user knows they're waiting for a slot
    for t in thread_info[max_workers:]:
        ui.research_table_update(t["id"], "queued")

    # Progress callback with thread-safe counter and incremental state saves
    completed = [0]
    total = len(tasks)
    state_lock = threading.Lock()

    def on_complete(task_id: str, result: AgentResult):
        # In-memory mutations under lock (fast), disk I/O outside (slow)
        with state_lock:
            completed[0] += 1
            current = completed[0]

            # Sync from authoritative source (ui tracks all agents, all retries)
            state.total_cost = ui._session_cost

            if result.success:
                if task_id not in state.completed_threads:
                    state.completed_threads.append(task_id)
                if task_id.startswith("followup_"):
                    _mark_followup_complete(state, task_id, result)
                else:
                    for t in state.threads:
                        if t.get("id") == task_id:
                            t["status"] = "completed"
                            t["output_file"] = result.output_file
                            break
            else:
                if task_id not in state.failed_threads:
                    state.failed_threads.append(task_id)
                if task_id.startswith("followup_"):
                    _mark_followup_failed(state, task_id, result.error)
                else:
                    for t in state.threads:
                        if t.get("id") == task_id:
                            t["status"] = "failed"
                            break

        # Persist to disk outside the lock — state.save() has its own _save_lock
        state.save()

        # Update table status and running cost
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

    # Stream callback factory: creates per-thread loggers for real-time visibility
    def make_stream_cb(thread_id: str):
        def cb(event_type: str, name: str, data: dict):
            if event_type == "tool_use":
                # Log tool calls with thread ID for cross-reference with table
                # Note: avoid [brackets] around thread_id — Rich eats them as markup
                if name == "WebSearch":
                    query = data.get("query", "")[:80]
                    ui.verbose(f"{thread_id} WebSearch: {query}")
                elif name == "WebFetch":
                    url = data.get("url", "")[:80]
                    ui.verbose(f"{thread_id} WebFetch: {url}")
                elif name.startswith("mcp__brave-search__"):
                    query = data.get("query", "")[:80]
                    ui.verbose(f"{thread_id} BraveSearch: {query}")
                elif name.startswith("mcp__exa__"):
                    query = data.get("query", "")[:80]
                    ui.verbose(f"{thread_id} Exa: {query}")
                elif name.startswith("mcp__tavily__"):
                    query = data.get("query", "")[:80]
                    ui.verbose(f"{thread_id} Tavily: {query}")
                elif name.startswith("mcp__arxiv__"):
                    query = (data.get("query") or data.get("paper_id") or "")[:80]
                    ui.verbose(f"{thread_id} arXiv: {name.split('__')[-1]}: {query}")
                elif name.startswith("mcp__pubmed__"):
                    query = (data.get("query") or data.get("pmid") or "")[:80]
                    ui.verbose(f"{thread_id} PubMed: {name.split('__')[-1]}: {query}")
                elif name.startswith("mcp__openalex__"):
                    query = (data.get("query") or "")[:80]
                    ui.verbose(f"{thread_id} OpenAlex: {name.split('__')[-1]}: {query}")
                elif name.startswith("mcp__wikipedia__"):
                    query = (data.get("query") or data.get("title") or "")[:80]
                    ui.verbose(f"{thread_id} Wikipedia: {query}")
                elif name.startswith("mcp__context7__"):
                    query = (data.get("libraryName") or data.get("libraryId") or "")[:80]
                    ui.verbose(f"{thread_id} Context7: {query}")
                elif name.startswith("mcp__firecrawl__"):
                    url = data.get("url", "")[:80]
                    ui.verbose(f"{thread_id} Firecrawl: {url}")
                elif name.startswith("mcp__crawl4ai__"):
                    url = data.get("url", "")[:80]
                    ui.verbose(f"{thread_id} Crawl4AI: {url}")
                elif name.startswith("mcp__playwright__"):
                    url = data.get("url", "")[:80]
                    ui.verbose(f"{thread_id} Playwright: {name.split('__')[-1]}: {url}")
                elif name == "Write":
                    path = data.get("file_path", "")
                    fname = path.split("/")[-1] if "/" in path else path
                    ui.verbose(f"{thread_id} Write: {fname}")
                elif name == "Read":
                    path = data.get("file_path", "")
                    fname = path.split("/")[-1] if "/" in path else path
                    ui.verbose(f"{thread_id} Read: {fname}")
                else:
                    ui.verbose(f"{thread_id} {name}")
            elif event_type == "result":
                cost = data.get("cost", 0)
                ui.verbose(f"{thread_id} Done (${cost:.2f})")
        return cb

    # Log full agent conversations to logs/ for debugging
    log_dir = report_dir / "logs" / f"iteration_{iteration}"

    try:
        results = spawn_agents_parallel(tasks, max_workers=max_workers, on_complete=on_complete,
                                         intervention_handler=intervention_handler,
                                         stream_callback_factory=make_stream_cb,
                                         stagger_secs=60.0,
                                         log_dir=log_dir)
        ui.research_table_complete()
    finally:
        # Clean up mcp.json (plaintext API keys) — runs even on
        # KeyboardInterrupt / unexpected exception so the file doesn't
        # linger on disk after a crash. spawn_agents_parallel's executor
        # blocks until every child exits, so by the time we get here the
        # children have already finished reading mcp.json.
        if mcp_config:
            try:
                mcp_config.unlink(missing_ok=True)
            except OSError:
                pass

    # #14: Print failure summary
    failed_results = {tid: r for tid, r in results.items() if not r.success}
    if failed_results:
        ui.warning("Failed agents:")
        for tid, r in failed_results.items():
            err_line = r.error.split("\n")[0][:120] if r.error else "Unknown error"
            ui.dim(f"  {tid}: {err_line}")

    # #30: Summarization transition message
    completed_count = sum(1 for r in results.values() if r.success)
    if completed_count > 0:
        ui.info(f"All {completed_count} agents finished. Summarizing outputs for evaluation...")

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
    output_file: Path,
    use_mcp: bool = False,
) -> str:
    """Build the prompt for a research agent."""

    questions_text = "\n".join(f"- {q}" for q in questions) if questions else "- Explore the topic thoroughly"

    if use_mcp:
        search_section = """## Search Tools Available
- **WebSearch**: Quick factual lookups and general questions (returns summary + links)
- **WebFetch**: Read a specific URL you already have (converts to markdown)
- **mcp__exa__web_search_exa**: Deep research — returns full article text inline, not just snippets
- **mcp__exa__get_code_context_exa**: Code, API docs, technical questions (GitHub, Stack Overflow, official docs)
- **mcp__exa__company_research_exa**: Deep company intelligence (funding, team, tech stack, competitors)
- **mcp__brave-search__brave_web_search**: Filtered web search — use freshness param for date-bounded queries
- **mcp__brave-search__brave_news_search**: Breaking/recent news with time control (freshness defaults to 24h)
- **mcp__tavily__tavily_search**: Agent-optimized search; **mcp__tavily__tavily_extract**: clean page extract
- **mcp__arxiv__search_papers** / **read_paper** / **download_paper**: arXiv preprints (CS/ML/physics/math)
- **mcp__pubmed__pubmed_search_articles**: PubMed search (biomedical/clinical)
- **mcp__pubmed__pubmed_europepmc_search**: Europe PMC search — also covers bioRxiv/medRxiv preprints
- **mcp__pubmed__pubmed_fetch_fulltext** / **pubmed_fetch_articles**: Full-text via PMC/Europe PMC + Unpaywall
- **mcp__openalex__openalex_search_entities**: 270M scholarly works across all disciplines + citation graph
- **mcp__openalex__openalex_get_citation_graph**: Citations into/out of a given paper
- **mcp__wikipedia__search_wikipedia** / **get_summary** / **get_article**: Universal grounding
- **mcp__context7__resolve-library-id** / **query-docs**: Version-pinned library/API docs (essential for tech topics)
- **mcp__firecrawl__firecrawl_scrape**: Clean page extraction (supports onlyMainContent, JS actions)
- **mcp__crawl4ai__scrape**: Free backup page fetcher with stealth browsing
- **mcp__playwright__browser_navigate** + **browser_snapshot**: JS-heavy / anti-bot scrape fallback

## Search Strategy
1. Start with **WebSearch** for quick factual queries, **mcp__exa__web_search_exa** for deep research with full text, or **mcp__tavily__tavily_search** for agent-optimized results
2. Use **mcp__exa__get_code_context_exa** or **mcp__context7__query-docs** for code/API/library documentation questions
3. Use **mcp__brave-search__brave_news_search** for recent events or news-sensitive topics
4. Use **mcp__pubmed__pubmed_search_articles** for biomedical/clinical literature; **pubmed_europepmc_search** for preprints (bioRxiv/medRxiv) and broader EPMC coverage
5. Use **mcp__arxiv__search_papers** for CS/ML/physics/math preprints (use narrow queries) and **mcp__arxiv__read_paper** when you need full text
6. Use **mcp__openalex__openalex_search_entities** to discover works across all disciplines and **openalex_get_citation_graph** for citation networks
7. Use **mcp__wikipedia__search_wikipedia** + **get_summary** for grounding and authoritative entity overviews
8. Use **mcp__firecrawl__firecrawl_scrape** to fetch specific web pages; **mcp__playwright__browser_navigate**+**browser_snapshot** for JS-heavy / anti-bot pages; **mcp__crawl4ai__scrape** as alternative
9. Use **WebFetch** for lightweight page reads when you just need a summary of a URL"""
    else:
        search_section = """## Search Tools
Use WebSearch and WebFetch to find authoritative sources.

## Search Strategy
1. Use WebSearch for broad and specific queries
2. Use WebFetch to read full content of promising URLs
3. If WebFetch fails on a URL, skip it and try other sources"""

    return f"""You are a research agent. Investigate this aspect of the larger topic.

## Main Topic / Research Brief
{topic}

## Your Assignment
**Title:** {title}
**Objective:** {objective}

## Key Questions to Answer
{questions_text}

## Scope and Context
{scope if scope else "No scope document provided."}
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

{search_section}

Prioritize:
- Peer-reviewed papers (PubMed, arXiv, Google Scholar)
- Official reports and documentation
- Expert analyses from reputable institutions
- Recent data (prefer last 3-5 years unless foundational)

## Resilience Rules
- If a fetch or scrape call fails (403, timeout, error), do NOT retry it. Move on to your next search or fetch — there are plenty of other sources to explore.
- Failed fetches should not interrupt your research flow. Keep searching, keep fetching other URLs, and only write the report once you have gathered enough material.
- NEVER produce empty or placeholder responses like "No response requested."
- You MUST write the output file even if some fetches fail — use everything you successfully gathered.
- Always complete your task. Never stall or wait for external input.

CRITICAL: You MUST call Write tool with file_path="{output_file}" to save your research.
"""


def _summarize_outputs(state: State):
    """Summarize all unsummarized research outputs in parallel.

    Scans disk for agent outputs missing summaries, not just the current batch.
    This handles resumed runs where prior threads were completed but not summarized.
    Only summarizes outputs from successfully completed agents.
    """
    report_dir = Path(state.report_dir)
    agents_dir = report_dir / "full" / "agents"
    summaries_dir = report_dir / "summaries" / "agents"

    if not agents_dir.exists():
        return

    tasks = []
    for output_file in sorted(agents_dir.glob("*.md")):
        thread_id = output_file.stem
        summary_file = summaries_dir / f"{thread_id}_summary.md"

        # Skip partial outputs from failed/timed-out agents
        if thread_id not in state.completed_threads:
            continue

        # Skip if already summarized
        if summary_file.exists():
            continue

        try:
            content = output_file.read_text()
        except Exception as e:
            ui.warning(f"Failed to read {output_file}: {e}")
            continue

        if not content.strip():
            continue

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
            "title": thread_id,
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

    try:
        summary_results = spawn_agents_parallel(tasks, max_workers=10, on_complete=on_complete)
    finally:
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
    """Create follow-up research threads from decision agent output.

    If the iteration gate populated `*_with_model` sibling lists, each
    follow-up gets its own model. Otherwise the model falls back to
    `state.research_model`.
    """

    followups = []
    default_model = state.research_model

    def _zip_entries(flat_key: str, with_model_key: str):
        """Yield (focus, model) pairs from either *_with_model or flat list."""
        wm = decision.get(with_model_key)
        if wm:
            for entry in wm:
                yield entry.get("focus", ""), entry.get("model", default_model)
        else:
            for focus in decision.get(flat_key, []):
                yield focus, default_model

    # Gaps need new research
    for i, (gap, model) in enumerate(_zip_entries("gaps", "gaps_with_model")):
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
            "model": model,
        })

    # Conflicts need resolution
    for i, (conflict, model) in enumerate(_zip_entries("conflicts", "conflicts_with_model")):
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
            "model": model,
        })

    # Areas to deepen
    for i, (area, model) in enumerate(_zip_entries("deepen", "deepen_with_model")):
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
            "model": model,
        })

    for fu in followups:
        state.add_followup(fu)

    if followups:
        ui.info(f"Created {len(followups)} follow-up threads for iteration {iteration + 1}")


def _mark_followup_complete(state: State, followup_id: str, result: AgentResult):
    """Mark a followup as completed (in-memory only, caller saves)."""
    for fu in state.followups:
        if fu.get("id") == followup_id:
            fu["status"] = "completed"
            fu["output_file"] = result.output_file
            return


def _mark_followup_failed(state: State, followup_id: str, error: str):
    """Mark a followup as failed (in-memory only, caller saves)."""
    for fu in state.followups:
        if fu.get("id") == followup_id:
            fu["status"] = "failed"
            fu["error"] = error
            return
