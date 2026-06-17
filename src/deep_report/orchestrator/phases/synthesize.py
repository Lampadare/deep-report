#!/usr/bin/env python3
"""Phase 4: Synthesize - Multi-pass synthesis for report generation."""

import json
import threading
from pathlib import Path
from typing import Optional

from ..state import State
from ..utils import spawn_agent, spawn_agents_parallel, extract_json, AGENT_TOOLS, RoleEnforcer
from ..ui import ui


def run_synthesize(state: State) -> bool:
    """Run the synthesis phase.

    Uses single-pass for small reports, multi-pass for large ones.

    Returns:
        True if synthesis succeeded, False otherwise
    """
    state.current_phase = 4
    state.checkpoint("synthesize_started")

    report_dir = Path(state.report_dir)
    # Followup IDs already land in `state.completed_threads` via the research
    # completion handler, so adding completed-followup count here would
    # double-count and push the report past the multi-pass threshold
    # unnecessarily. Use the unique set of completed thread IDs as the
    # ground-truth count.
    agent_count = len(set(state.completed_threads))

    # Guard: need at least some completed research
    if agent_count == 0:
        ui.error("Synthesis failed: no completed research to synthesize")
        return False

    ui.info("Writing your report from all research findings. This typically takes 5-15 minutes.")

    # Determine synthesis strategy
    if agent_count <= 10:
        state.synthesis_strategy = "single"
        ui.step(f"Synthesizing report from {agent_count} agents")
        success = _single_pass_synthesis(state, report_dir)
    else:
        state.synthesis_strategy = "multi"
        ui.step(f"Synthesizing report from {agent_count} agents (multiple refinement passes)")
        success = _multi_pass_synthesis(state, report_dir)

    if not success:
        ui.warning("All research is preserved despite synthesis failure")
        ui.info(f"  Raw research: {report_dir}/full/agents/")
        ui.info(f"  Resume to retry: deep-report --resume {report_dir}")
        return False

    state.report_assembled = True
    state.checkpoint("report_assembled")

    # Compile references
    ui.step("Compiling references")
    _compile_references(state, report_dir)
    state.refs_compiled = True
    state.checkpoint("refs_compiled")

    # Audio version if requested
    if state.generate_audio:
        ui.step("Generating audio version")
        _generate_audio(state, report_dir)
        state.audio_generated = True
        state.checkpoint("audio_generated")

    # Download papers if requested
    if state.download_papers:
        ui.step("Downloading open-access papers")
        _download_papers(state, report_dir)
        state.papers_downloaded = True
        state.checkpoint("papers_downloaded")

    state.mark_phase_complete(4)
    return True


def _poll_output_file(path: Path, stop_event: threading.Event, word_count_ref: list,
                      interval: float = 10.0):
    """Background poller that updates word_count_ref[0] from a growing output file."""
    while not stop_event.wait(interval):
        try:
            if path.exists():
                word_count = len(path.read_text(encoding='utf-8', errors='replace').split())
                if word_count > 0:
                    word_count_ref[0] = word_count
        except (OSError, IOError, UnicodeDecodeError, ValueError):
            pass


def _single_pass_synthesis(state: State, report_dir: Path) -> bool:
    """Single agent writes the entire report."""

    full_dir = report_dir / "full" / "agents"
    report_file = report_dir / "report.md"

    # Gather file paths — restrict to threads we actually marked as completed
    # so partial files left behind by timed-out / failed agents aren't fed in
    # as if they were valid research.
    completed = set(state.completed_threads)
    all_files = sorted(full_dir.glob("*.md"))
    research_files = [f for f in all_files if f.stem in completed]
    skipped = [f.stem for f in all_files if f.stem not in completed]
    if skipped:
        ui.warning(f"Skipping {len(skipped)} agent output(s) not in completed_threads: "
                   f"{', '.join(skipped[:5])}{'…' if len(skipped) > 5 else ''}")
    if not research_files:
        ui.error("Synthesis failed: no research files found")
        return False

    file_list = "\n".join(f"- {f}" for f in research_files)

    # Use brief if available (detailed research instructions), otherwise topic
    research_instructions = state.brief or state.topic

    prompt = f"""TASK: Synthesize research files into comprehensive report.

Topic: {research_instructions}
Report type: {state.report_type}
Expertise level: {state.expertise_level}
OUTPUT FILE: {report_file}

STEPS:
1. Use Read tool to read each of these {len(research_files)} research files:
{file_list}

2. Synthesize content into a comprehensive report with this structure:
   - Title and metadata
   - Executive Summary (500-800 words)
   - Table of Contents
   - Introduction
   - Main sections (synthesize by theme, not by source)
   - Discussion
   - Conclusions
   - Future Directions

3. Use Write tool to save the report

Guidelines:
- Target 15,000-25,000 words
- Integrate findings across sources, don't just concatenate
- Highlight consensus and conflicts in the literature
- Include specific data, statistics, citations
- Write for {state.expertise_level} readers
- Use markdown formatting with clear headers

CRITICAL: You MUST call Write tool with file_path="{report_file}" to save the report.
"""

    stop_event = threading.Event()
    word_count_ref = [0]
    poll_thread = threading.Thread(
        target=_poll_output_file, args=(report_file, stop_event, word_count_ref),
        daemon=True,
    )
    poll_thread.start()

    try:
        with ui.spinner_task("Synthesis agent working (this may take a while)..."):
            result = spawn_agent(
                prompt, model="opus", output_file=report_file, timeout_secs=2700,
                allowed_tools=AGENT_TOOLS["synthesis"]
            )
    finally:
        stop_event.set()
        poll_thread.join(timeout=2)

    if word_count_ref[0] > 0 and not result.success:
        ui.info(f"Partial synthesis output: {word_count_ref[0]:,} words written before failure")

    if result.success and report_file.exists():
        word_count = RoleEnforcer.count_words_streaming(report_file)
        ui.success(f"Report written: {word_count:,} words")
        return True

    ui.error(f"Single-pass synthesis failed: {result.error}")
    return False


def _multi_pass_synthesis(state: State, report_dir: Path) -> bool:
    """Multi-agent synthesis for large reports."""

    full_dir = report_dir / "full" / "agents"
    summaries_dir = report_dir / "summaries" / "agents"

    # Step 1: Cluster agents thematically
    ui.step("Clustering research threads")
    clusters = _cluster_threads(state, summaries_dir)

    # Step 2: Spawn synthesis agent per cluster
    ui.step(f"Synthesizing {len(clusters)} sections in parallel")
    synthesis_results = _synthesize_clusters(state, clusters, full_dir, report_dir)

    successful_parts = [r for r in synthesis_results if r["success"]]
    if len(successful_parts) < len(clusters) // 2:
        ui.error(f"Multi-pass synthesis failed: too many failures ({len(successful_parts)}/{len(clusters)} succeeded)")
        return False

    state.synthesis_parts = [p["file"] for p in successful_parts]
    state.save()

    # Step 3: Write header, transitions, conclusion
    ui.step("Writing report header and conclusion")
    header_file = report_dir / "state" / "report_header.md"
    conclusion_file = report_dir / "state" / "report_conclusion.md"

    header_ok = _write_header(state, report_dir, header_file, successful_parts)
    conclusion_ok = _write_conclusion(state, report_dir, conclusion_file, successful_parts)

    if not header_ok or not conclusion_ok:
        ui.warning("Header or conclusion generation had issues, continuing with available content")

    # Step 4: Assemble final report
    ui.step("Assembling final report")
    report_file = report_dir / "report.md"
    _assemble_report(report_file, header_file, successful_parts, conclusion_file)

    if report_file.exists():
        word_count = RoleEnforcer.count_words_streaming(report_file)
        ui.success(f"Report assembled: {word_count:,} words")
        return True

    return False


def _cluster_threads(state: State, summaries_dir: Path) -> list[dict]:
    """Cluster research threads into thematic groups."""

    # Gather thread summaries — only for threads that actually completed.
    # Partial summaries left behind by timed-out / failed agents would
    # otherwise be fed into clustering and downstream cluster synthesis.
    completed = set(state.completed_threads)
    thread_summaries = []
    skipped_summaries = []
    for f in sorted(summaries_dir.glob("*_summary.md")):
        thread_id = f.stem.replace("_summary", "")
        if thread_id not in completed:
            skipped_summaries.append(thread_id)
            continue
        try:
            content = f.read_text(encoding='utf-8', errors='replace')[:500]
            thread_summaries.append(f"{thread_id}: {content}")
        except (OSError, IOError) as e:
            ui.warning(f"Summary reading failed for {f.name}: {e}")
    if skipped_summaries:
        ui.warning(f"Skipping {len(skipped_summaries)} summary file(s) "
                   f"not in completed_threads: "
                   f"{', '.join(skipped_summaries[:5])}"
                   f"{'…' if len(skipped_summaries) > 5 else ''}")

    num_clusters = min(5, max(2, len(thread_summaries) // 4))

    # Use brief if available (detailed research instructions), otherwise topic
    research_instructions = state.brief or state.topic

    prompt = f"""Cluster these research threads into {num_clusters} thematic groups.

## Topic
{research_instructions}

## Threads
{chr(10).join(thread_summaries)}

Return a JSON object (ONLY valid JSON):
{{
    "clusters": [
        {{
            "id": 1,
            "title": "Section title",
            "theme": "What this section covers",
            "thread_ids": ["thread_1", "thread_3", "thread_7"]
        }}
    ]
}}

Group related threads together. Every thread must be assigned to exactly one cluster.
"""

    with ui.spinner_task("Clustering research threads..."):
        result = spawn_agent(
            prompt, model="sonnet", timeout_secs=360,
            allowed_tools=AGENT_TOOLS["decision"]  # Read-only, output via stdout
        )

    if result.success:
        data = extract_json(result.output)
        if data:
            # The cluster agent occasionally hallucinates or echoes thread IDs
            # that aren't in `completed`. Validate every returned thread_id so
            # downstream _synthesize_clusters can't read partial-agent files.
            raw_clusters = data.get("clusters", []) or []
            cleaned: list[dict] = []
            dropped_ids: list[str] = []
            for cluster in raw_clusters:
                tids = cluster.get("thread_ids") or []
                kept = [tid for tid in tids if tid in completed]
                bad = [tid for tid in tids if tid not in completed]
                if bad:
                    dropped_ids.extend(bad)
                if kept:
                    cleaned.append({**cluster, "thread_ids": kept})
            if dropped_ids:
                ui.warning(f"Cluster agent returned {len(dropped_ids)} non-completed "
                           f"thread_id(s); dropping: "
                           f"{', '.join(dropped_ids[:5])}"
                           f"{'…' if len(dropped_ids) > 5 else ''}")
            if cleaned:
                return cleaned
            # If every cluster came back empty after validation, fall through
            # to the deterministic even-split fallback below.

    # Fallback: even split, restricted to completed threads.
    all_threads = [
        f.stem.replace("_summary", "")
        for f in sorted(summaries_dir.glob("*_summary.md"))
        if f.stem.replace("_summary", "") in completed
    ]
    clusters = []
    chunk_size = max(1, len(all_threads) // num_clusters)

    for i in range(num_clusters):
        start_idx = i * chunk_size
        end_idx = start_idx + chunk_size if i < num_clusters - 1 else len(all_threads)
        clusters.append({
            "id": i + 1,
            "title": f"Section {i + 1}",
            "theme": "Research findings",
            "thread_ids": all_threads[start_idx:end_idx]
        })

    return clusters


def _synthesize_clusters(
    state: State,
    clusters: list[dict],
    full_dir: Path,
    report_dir: Path
) -> list[dict]:
    """Synthesize each cluster in parallel."""

    tasks = []

    for cluster in clusters:
        cluster_id = cluster["id"]
        title = cluster["title"]
        theme = cluster["theme"]
        thread_ids = cluster["thread_ids"]

        # Gather file paths for this cluster - agent will read them
        cluster_files = []
        for tid in thread_ids:
            for pattern in [f"{tid}.md", f"{tid}_*.md"]:
                for f in full_dir.glob(pattern):
                    cluster_files.append(f)
                    break

        if not cluster_files:
            continue

        file_list = "\n".join(f"- {f}" for f in cluster_files)
        output_file = report_dir / "state" / f"part_{cluster_id}_synthesis.md"

        # Use brief if available (detailed research instructions), otherwise topic
        research_instructions = state.brief or state.topic

        prompt = f"""TASK: Synthesize research files into report section.

Topic: {research_instructions}
Section Title: {title}
Theme: {theme}
OUTPUT FILE: {output_file}

STEPS:
1. Use Read tool to read each of these {len(cluster_files)} research files:
{file_list}

2. Synthesize into a coherent section (5,000-8,000 words):
   - Integrate findings, don't just summarize each source
   - Maintain logical flow and narrative
   - Include specific data and citations
   - Write for {state.expertise_level} readers
   - Use markdown with ## and ### subsection headers
   - Do NOT include title or executive summary

3. Use Write tool to save the section

CRITICAL: You MUST call Write tool with file_path="{output_file}" to save the section.
"""

        tasks.append({
            "id": f"cluster_{cluster_id}",
            "prompt": prompt,
            "model": "opus",
            "output_file": str(output_file),
            "timeout_secs": 1800,
            "allowed_tools": AGENT_TOOLS["synthesis"],
        })

    # Progress tracking for synthesis
    ui.agent_progress_start(len(tasks), "Synthesizing sections")
    completed_count = [0]
    completed_lock = threading.Lock()

    def on_complete(task_id: str, result):
        with completed_lock:
            completed_count[0] += 1
            current = completed_count[0]
        status = "✓" if result.success else "✗"
        ui.agent_progress_update(current, f"{task_id}: {status}")

    # Run in parallel
    try:
        results = spawn_agents_parallel(tasks, max_workers=5, on_complete=on_complete)
    finally:
        ui.agent_progress_complete(f"Synthesized {len(clusters)} sections")

    synthesis_results = []
    for cluster in clusters:
        task_id = f"cluster_{cluster['id']}"
        result = results.get(task_id)
        output_file = report_dir / "state" / f"part_{cluster['id']}_synthesis.md"

        synthesis_results.append({
            "cluster_id": cluster["id"],
            "title": cluster["title"],
            "success": result.success if result else False,
            "file": str(output_file),
            "error": result.error if result and not result.success else None
        })

    return synthesis_results


def _write_header(state: State, report_dir: Path, header_file: Path, parts: list[dict]) -> bool:
    """Write report header with executive summary and TOC.

    Returns:
        True if header was written successfully, False otherwise
    """
    # Gather part file paths - agent will read them
    part_files = [(p["title"], Path(p["file"])) for p in parts if Path(p["file"]).exists()]
    file_list = "\n".join(f"- **{title}**: {f}" for title, f in part_files)
    section_titles = ", ".join(p["title"] for p in parts)

    # Use brief if available (detailed research instructions), otherwise topic
    research_instructions = state.brief or state.topic

    prompt = f"""TASK: Write report header section.

Topic: {research_instructions}
Report type: {state.report_type}
OUTPUT FILE: {header_file}

STEPS:
1. Use Read tool to read the first ~300 words of each section file:
{file_list}

2. Write the header with:
   - Report title (# heading)
   - Metadata (date, report type, expertise level)
   - Executive Summary (500-800 words covering all sections)
   - Table of Contents (sections: {section_titles})
   - Introduction (300-500 words setting context)

3. Use Write tool to save the header

CRITICAL: You MUST call Write tool with file_path="{header_file}" to save the header.
"""

    with ui.spinner_task("Writing report header..."):
        result = spawn_agent(
            prompt, model="opus", output_file=header_file, timeout_secs=900,
            allowed_tools=AGENT_TOOLS["synthesis"]
        )

    if not result.success:
        ui.warning(f"Header writing failed: {result.error}")
        return False

    if not header_file.exists():
        ui.warning("Header writing failed: output file not created")
        return False

    return True


def _write_conclusion(state: State, report_dir: Path, conclusion_file: Path, parts: list[dict]) -> bool:
    """Write report conclusion synthesizing all parts.

    Returns:
        True if conclusion was written successfully, False otherwise
    """
    # Gather part file paths - agent will read them
    part_files = [(p["title"], Path(p["file"])) for p in parts if Path(p["file"]).exists()]
    file_list = "\n".join(f"- **{title}**: {f}" for title, f in part_files)

    # Use brief if available (detailed research instructions), otherwise topic
    research_instructions = state.brief or state.topic

    prompt = f"""TASK: Write report conclusion section.

Topic: {research_instructions}
OUTPUT FILE: {conclusion_file}

STEPS:
1. Use Read tool to read the last ~200 words of each section file:
{file_list}

2. Write the conclusion (1,500-2,500 words) with:
   - Discussion section (integrate findings across all sections)
   - Key conclusions (numbered list of main takeaways)
   - Future directions (what's next for this field)
   - Limitations (what we didn't cover, caveats)

3. Use Write tool to save the conclusion

CRITICAL: You MUST call Write tool with file_path="{conclusion_file}" to save the conclusion.
"""

    with ui.spinner_task("Writing conclusion..."):
        result = spawn_agent(
            prompt, model="opus", output_file=conclusion_file, timeout_secs=900,
            allowed_tools=AGENT_TOOLS["synthesis"]
        )

    if not result.success:
        ui.warning(f"Conclusion writing failed: {result.error}")
        return False

    if not conclusion_file.exists():
        ui.warning("Conclusion writing failed: output file not created")
        return False

    return True


def _assemble_report(report_file: Path, header_file: Path, parts: list[dict], conclusion_file: Path):
    """Concatenate all parts into final report."""

    sections = []

    # Header
    if header_file.exists():
        try:
            sections.append(header_file.read_text(encoding='utf-8', errors='replace'))
        except (OSError, IOError) as e:
            ui.warning(f"Header reading failed: {e}")

    # Main sections
    for p in sorted(parts, key=lambda x: x["cluster_id"]):
        part_file = Path(p["file"])
        if part_file.exists():
            try:
                content = part_file.read_text(encoding='utf-8', errors='replace')
                sections.append(f"\n\n---\n\n# {p['title']}\n\n{content}")
            except (OSError, IOError) as e:
                ui.warning(f"Part file reading failed for {part_file.name}: {e}")
        else:
            ui.warning(f"Missing synthesis part: {part_file.name}")

    # Conclusion
    if conclusion_file.exists():
        try:
            sections.append(f"\n\n---\n\n{conclusion_file.read_text(encoding='utf-8', errors='replace')}")
        except (OSError, IOError) as e:
            ui.warning(f"Conclusion reading failed: {e}")

    # Check if we have actual content beyond just the header
    has_header_only = len(sections) == 1 and header_file.exists()
    if not sections or has_header_only:
        ui.error("Report assembly failed: no content sections available")
        raise RuntimeError("Report assembly failed: no content sections available")

    try:
        report_file.write_text("\n".join(sections), encoding='utf-8')
    except (OSError, PermissionError) as e:
        ui.error(f"Report writing failed: {e}")
        raise


def _compile_references(state: State, report_dir: Path):
    """Compile all references from the report."""

    report_file = report_dir / "report.md"
    refs_file = report_dir / "refs.md"

    if not report_file.exists():
        return

    prompt = f"""TASK: Extract and compile references from research report.

INPUT FILE: {report_file}
OUTPUT FILE: {refs_file}

STEPS:
1. Use Read tool to read {report_file}
2. Extract all references, citations, and URLs
3. Organize into deduplicated list:

# References

## Academic Papers
- Author (Year). Title. Journal. DOI/URL

## Reports and Documentation
- Organization (Year). Title. URL

## Web Sources
- Author/Site. Title. URL. Accessed date.

4. Use Write tool to save the references

CRITICAL: You MUST call Write tool with file_path="{refs_file}" to save the references.
"""

    with ui.spinner_task("Compiling references..."):
        spawn_agent(
            prompt, model="sonnet", output_file=refs_file, timeout_secs=900,
            allowed_tools=AGENT_TOOLS["synthesis"]
        )


def _generate_audio(state: State, report_dir: Path):
    """Generate audio-friendly version of the report."""

    report_file = report_dir / "report.md"
    audio_file = report_dir / "report_audio.md"

    if not report_file.exists():
        return

    # Check file size before reading into memory
    MAX_AUDIO_SIZE = 500 * 1024  # 500KB
    try:
        file_size = report_file.stat().st_size
    except OSError as e:
        ui.warning(f"Report file stat failed: {e}")
        return

    if file_size > MAX_AUDIO_SIZE:
        ui.warning(f"Report too large for single-read audio generation ({file_size // 1024}KB > {MAX_AUDIO_SIZE // 1024}KB)")
        ui.info("Using streaming multi-pass audio generation")
        # Read in chunks for multi-pass, which already handles large reports
        try:
            report_content = report_file.read_text(encoding='utf-8', errors='replace')
        except (OSError, IOError) as e:
            ui.warning(f"Audio conversion failed: could not read report: {e}")
            return
        _generate_audio_multi(state, report_dir, report_content)
        return

    # Check report size
    try:
        report_content = report_file.read_text(encoding='utf-8', errors='replace')
    except (OSError, IOError) as e:
        ui.warning(f"Audio conversion failed: could not read report: {e}")
        return
    word_count = len(report_content.split())

    if word_count > 20000:
        # Split audio generation by section
        _generate_audio_multi(state, report_dir, report_content)
    else:
        _generate_audio_single(state, report_file, audio_file)


def _generate_audio_single(state: State, report_file: Path, audio_file: Path):
    """Single-pass audio generation."""

    prompt = f"""TASK: Rewrite research report for audio/podcast format.

INPUT FILE: {report_file}
OUTPUT FILE: {audio_file}

STEPS:
1. Use Read tool to read {report_file}
2. Rewrite for audio (60-70% of original word count):
   - Use conversational, spoken language
   - Expand abbreviations and acronyms
   - Replace visual elements (tables, figures) with descriptions
   - Add transitions between sections ("Now let's explore...")
   - Prefer shorter sentences (aim for clarity)
   - Keep technical accuracy
3. Use Write tool to save the audio version

CRITICAL: You MUST call Write tool with file_path="{audio_file}" to save the audio version.
"""

    with ui.spinner_task("Converting to audio format..."):
        spawn_agent(
            prompt, model="opus", output_file=audio_file, timeout_secs=1800,
            allowed_tools=AGENT_TOOLS["synthesis"]
        )


def _generate_audio_multi(state: State, report_dir: Path, report_content: str):
    """Multi-pass audio generation for large reports in parallel."""

    audio_file = report_dir / "report_audio.md"

    # Split by major sections
    sections = report_content.split("\n# ")
    tasks = []

    for i, section in enumerate(sections):
        if i == 0 and not section.startswith("#"):
            section = "# " + section
        elif i > 0:
            section = "# " + section

        part_file = report_dir / "state" / f"audio_part_{i}.md"

        prompt = f"""TASK: Rewrite section for audio format.

OUTPUT FILE: {part_file}

<section>
{section[:15000]}
</section>

Rewrite with:
- Conversational language
- Expanded acronyms
- Shorter sentences
- Technical accuracy preserved

CRITICAL: You MUST call Write tool with file_path="{part_file}" to save.
"""

        tasks.append({
            "id": f"audio_{i}",
            "prompt": prompt,
            "model": "opus",
            "output_file": str(part_file),
            "timeout_secs": 900,
            "allowed_tools": AGENT_TOOLS["synthesis"],
        })

    if not tasks:
        return

    # Progress tracking for audio generation
    ui.agent_progress_start(len(tasks), "Converting sections to audio")
    completed_count = [0]
    completed_lock = threading.Lock()

    def on_complete(task_id: str, result):
        with completed_lock:
            completed_count[0] += 1
            current = completed_count[0]
        status = "✓" if result.success else "✗"
        ui.agent_progress_update(current, f"{task_id}: {status}")
        ui.verbose(f"Audio {task_id}: {status}")

    try:
        results = spawn_agents_parallel(tasks, max_workers=5, on_complete=on_complete)
    finally:
        ui.agent_progress_complete(f"Converted {len(tasks)} sections to audio")

    # Combine parts in order
    audio_parts = []
    temp_files = []
    for i in range(len(sections)):
        part_file = report_dir / "state" / f"audio_part_{i}.md"
        temp_files.append(part_file)
        if part_file.exists():
            try:
                audio_parts.append(part_file.read_text(encoding='utf-8', errors='replace'))
            except Exception as e:
                ui.warning(f"Audio part {i} reading failed: {e}")

    if audio_parts:
        try:
            audio_file.write_text("\n\n---\n\n".join(audio_parts), encoding='utf-8')
        except (OSError, PermissionError) as e:
            ui.warning(f"Audio file writing failed: {e}")

    # Clean up temporary audio part files
    for temp_file in temp_files:
        try:
            if temp_file.exists():
                temp_file.unlink()
        except OSError:
            pass  # Best effort cleanup


def _download_papers(state: State, report_dir: Path):
    """Download open-access papers cited in the report."""

    refs_file = report_dir / "refs.md"
    papers_dir = report_dir / "papers"

    if not refs_file.exists():
        ui.info("No refs.md found, skipping paper downloads")
        return

    try:
        from ..papers import PaperDownloader

        ui.verbose(f"Downloading papers to: {papers_dir}")
        downloader = PaperDownloader(papers_dir)
        with ui.spinner_task("Downloading open-access papers..."):
            results = downloader.download_all(refs_file)

        # Print summary
        ui.info(downloader.get_summary())

    except ImportError:
        # Fallback if requests not installed
        ui.warning("Paper download requires 'requests' library. Skipping.")
    except Exception as e:
        ui.warning(f"Paper download failed: {e}")
