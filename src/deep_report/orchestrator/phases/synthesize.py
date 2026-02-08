#!/usr/bin/env python3
"""Phase 4: Synthesize - Multi-pass synthesis for report generation."""

import json
from pathlib import Path
from typing import Optional

from ..state import State
from ..utils import spawn_agent, spawn_agents_parallel, AgentResult, extract_json, AGENT_TOOLS, RoleEnforcer


def run_synthesize(state: State) -> bool:
    """Run the synthesis phase.

    Uses single-pass for small reports, multi-pass for large ones.

    Returns:
        True if synthesis succeeded, False otherwise
    """
    state.checkpoint("synthesize_started")

    report_dir = Path(state.report_dir)
    agent_count = len(state.completed_threads) + len([f for f in state.followups if f.get("status") == "completed"])

    # Guard: need at least some completed research
    if agent_count == 0:
        print("ERROR: No completed research to synthesize")
        return False

    # Determine synthesis strategy
    if agent_count <= 10:
        state.synthesis_strategy = "single"
        print(f"Using single-pass synthesis for {agent_count} agents")
        success = _single_pass_synthesis(state, report_dir)
    else:
        state.synthesis_strategy = "multi"
        print(f"Using multi-pass synthesis for {agent_count} agents")
        success = _multi_pass_synthesis(state, report_dir)

    if not success:
        print("ERROR: Synthesis failed")
        return False

    state.report_assembled = True
    state.checkpoint("report_assembled")

    # Compile references
    print("Compiling references...")
    _compile_references(state, report_dir)
    state.refs_compiled = True
    state.checkpoint("refs_compiled")

    # Audio version if requested
    if state.generate_audio:
        print("Generating audio version...")
        _generate_audio(state, report_dir)
        state.audio_generated = True
        state.checkpoint("audio_generated")

    # Download papers if requested
    if state.download_papers:
        print("Downloading open-access papers...")
        _download_papers(state, report_dir)
        state.papers_downloaded = True
        state.checkpoint("papers_downloaded")

    state.mark_phase_complete(4)
    print("Phase 4 (Synthesize) complete")
    return True


def _single_pass_synthesis(state: State, report_dir: Path) -> bool:
    """Single agent writes the entire report."""

    full_dir = report_dir / "full" / "agents"
    report_file = report_dir / "report.md"

    # Gather file paths - agent will read them
    research_files = sorted(full_dir.glob("*.md"))
    if not research_files:
        print("No research files found")
        return False

    file_list = "\n".join(f"- {f}" for f in research_files)

    prompt = f"""You are a synthesis agent. Write a comprehensive research report.

## Topic
{state.topic}

## Report Type
{state.report_type}

## Expertise Level
{state.expertise_level}

## Research Files
Read and synthesize these {len(research_files)} research files:

{file_list}

Use the Read tool to read each file, then synthesize their content.

## Output Requirements
Write the complete report to: {report_file}

Structure:
1. Title and metadata
2. Executive Summary (500-800 words)
3. Table of Contents
4. Introduction
5. Main sections (synthesize by theme, not by source)
6. Discussion
7. Conclusions
8. Future Directions

Guidelines:
- Target 15,000-25,000 words
- Integrate findings across sources, don't just concatenate
- Highlight consensus and conflicts in the literature
- Include specific data, statistics, citations
- Write for {state.expertise_level} readers
- Use markdown formatting with clear headers

Write ONLY the report content. References will be compiled separately.
"""

    result = spawn_agent(
        prompt, model="opus", output_file=report_file, timeout_secs=900,
        allowed_tools=AGENT_TOOLS["synthesis"]
    )

    if result.success and report_file.exists():
        word_count = RoleEnforcer.count_words_streaming(report_file)
        print(f"Report written: {word_count:,} words")
        return True

    print(f"Single-pass synthesis failed: {result.error}")
    return False


def _multi_pass_synthesis(state: State, report_dir: Path) -> bool:
    """Multi-agent synthesis for large reports."""

    full_dir = report_dir / "full" / "agents"
    summaries_dir = report_dir / "summaries" / "agents"

    # Step 1: Cluster agents thematically
    print("Clustering research threads...")
    clusters = _cluster_threads(state, summaries_dir)

    # Step 2: Spawn synthesis agent per cluster
    print(f"Synthesizing {len(clusters)} sections in parallel...")
    synthesis_results = _synthesize_clusters(state, clusters, full_dir, report_dir)

    successful_parts = [r for r in synthesis_results if r["success"]]
    if len(successful_parts) < len(clusters) // 2:
        print(f"Too many synthesis failures: {len(successful_parts)}/{len(clusters)}")
        return False

    state.synthesis_parts = [p["file"] for p in successful_parts]
    state.save()

    # Step 3: Write header, transitions, conclusion
    print("Writing report header and conclusion...")
    header_file = report_dir / "state" / "report_header.md"
    conclusion_file = report_dir / "state" / "report_conclusion.md"

    _write_header(state, report_dir, header_file, successful_parts)
    _write_conclusion(state, report_dir, conclusion_file, successful_parts)

    # Step 4: Assemble final report
    print("Assembling final report...")
    report_file = report_dir / "report.md"
    _assemble_report(report_file, header_file, successful_parts, conclusion_file)

    if report_file.exists():
        word_count = RoleEnforcer.count_words_streaming(report_file)
        print(f"Report assembled: {word_count:,} words")
        return True

    return False


def _cluster_threads(state: State, summaries_dir: Path) -> list[dict]:
    """Cluster research threads into thematic groups."""

    # Gather all thread summaries
    thread_summaries = []
    for f in sorted(summaries_dir.glob("*_summary.md")):
        thread_id = f.stem.replace("_summary", "")
        content = f.read_text()[:500]
        thread_summaries.append(f"{thread_id}: {content}")

    num_clusters = min(5, max(2, len(thread_summaries) // 4))

    prompt = f"""Cluster these research threads into {num_clusters} thematic groups.

## Topic
{state.topic}

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

    result = spawn_agent(
        prompt, model="sonnet", timeout_secs=120,
        allowed_tools=AGENT_TOOLS["decision"]  # Read-only, output via stdout
    )

    if result.success:
        data = extract_json(result.output)
        if data:
            return data.get("clusters", [])

    # Fallback: even split
    all_threads = [f.stem.replace("_summary", "") for f in sorted(summaries_dir.glob("*_summary.md"))]
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

        prompt = f"""Synthesize these research outputs into a coherent report section.

## Topic
{state.topic}

## Section
**Title:** {title}
**Theme:** {theme}

## Source Files
Read and synthesize these {len(cluster_files)} research files:

{file_list}

Use the Read tool to read each file, then synthesize their content.

## Output Requirements
Write the synthesized section to: {output_file}

Guidelines:
- Target 5,000-8,000 words
- Integrate findings, don't just summarize each source
- Maintain logical flow and narrative
- Include specific data and citations
- Write for {state.expertise_level} readers

Use markdown with clear subsection headers (## and ###).
Do NOT include title or executive summary - just the section content.
"""

        tasks.append({
            "id": f"cluster_{cluster_id}",
            "prompt": prompt,
            "model": "opus",
            "output_file": str(output_file),
            "timeout_secs": 600,
            "allowed_tools": AGENT_TOOLS["synthesis"],
        })

    # Run in parallel
    results = spawn_agents_parallel(tasks, max_workers=5)

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


def _write_header(state: State, report_dir: Path, header_file: Path, parts: list[dict]):
    """Write report header with executive summary and TOC."""

    # Gather part file paths - agent will read them
    part_files = [(p["title"], Path(p["file"])) for p in parts if Path(p["file"]).exists()]
    file_list = "\n".join(f"- **{title}**: {f}" for title, f in part_files)
    section_titles = ", ".join(p["title"] for p in parts)

    prompt = f"""Write the header section for this research report.

## Topic
{state.topic}

## Report Type
{state.report_type}

## Section Files
Read the beginning of each section file to understand the content:

{file_list}

Use the Read tool to read each file (just the first ~300 words is enough for context).

## Output Requirements
Write to: {header_file}

Include:
1. Report title (# heading)
2. Metadata (date, report type, expertise level)
3. Executive Summary (500-800 words covering all sections)
4. Table of Contents (list the sections: {section_titles})
5. Introduction (300-500 words setting context)

Write markdown. Be concise but comprehensive.
"""

    spawn_agent(
        prompt, model="opus", output_file=header_file, timeout_secs=300,
        allowed_tools=AGENT_TOOLS["synthesis"]
    )


def _write_conclusion(state: State, report_dir: Path, conclusion_file: Path, parts: list[dict]):
    """Write report conclusion synthesizing all parts."""

    # Gather part file paths - agent will read them
    part_files = [(p["title"], Path(p["file"])) for p in parts if Path(p["file"]).exists()]
    file_list = "\n".join(f"- **{title}**: {f}" for title, f in part_files)

    prompt = f"""Write the conclusion section for this research report.

## Topic
{state.topic}

## Section Files
Read the end of each section file to understand the conclusions:

{file_list}

Use the Read tool to read each file (focus on the last ~200 words for context).

## Output Requirements
Write to: {conclusion_file}

Include:
1. Discussion section (integrate findings across all sections)
2. Key conclusions (numbered list of main takeaways)
3. Future directions (what's next for this field)
4. Limitations (what we didn't cover, caveats)

Target 1,500-2,500 words. Write markdown.
"""

    spawn_agent(
        prompt, model="opus", output_file=conclusion_file, timeout_secs=300,
        allowed_tools=AGENT_TOOLS["synthesis"]
    )


def _assemble_report(report_file: Path, header_file: Path, parts: list[dict], conclusion_file: Path):
    """Concatenate all parts into final report."""

    sections = []

    # Header
    if header_file.exists():
        sections.append(header_file.read_text())

    # Main sections
    for p in sorted(parts, key=lambda x: x["cluster_id"]):
        part_file = Path(p["file"])
        if part_file.exists():
            content = part_file.read_text()
            sections.append(f"\n\n---\n\n# {p['title']}\n\n{content}")

    # Conclusion
    if conclusion_file.exists():
        sections.append(f"\n\n---\n\n{conclusion_file.read_text()}")

    report_file.write_text("\n".join(sections))


def _compile_references(state: State, report_dir: Path):
    """Compile all references from the report."""

    report_file = report_dir / "report.md"
    refs_file = report_dir / "refs.md"

    if not report_file.exists():
        return

    prompt = f"""Extract and compile all references from this research report.

Read the report file: {report_file}

Write a deduplicated, organized reference list to: {refs_file}

Format:
# References

## Academic Papers
- Author (Year). Title. Journal. DOI/URL

## Reports and Documentation
- Organization (Year). Title. URL

## Web Sources
- Author/Site. Title. URL. Accessed date.

Remove duplicates. Organize by type. Include all URLs.
"""

    spawn_agent(
        prompt, model="sonnet", output_file=refs_file, timeout_secs=300,
        allowed_tools=AGENT_TOOLS["synthesis"]
    )


def _generate_audio(state: State, report_dir: Path):
    """Generate audio-friendly version of the report."""

    report_file = report_dir / "report.md"
    audio_file = report_dir / "report_audio.md"

    if not report_file.exists():
        return

    # Check report size
    report_content = report_file.read_text()
    word_count = len(report_content.split())

    if word_count > 20000:
        # Split audio generation by section
        _generate_audio_multi(state, report_dir, report_content)
    else:
        _generate_audio_single(state, report_file, audio_file)


def _generate_audio_single(state: State, report_file: Path, audio_file: Path):
    """Single-pass audio generation."""

    prompt = f"""Rewrite this research report for audio/podcast format.

Read the report: {report_file}

Write the audio version to: {audio_file}

Guidelines:
- Use conversational, spoken language
- Expand abbreviations and acronyms
- Replace visual elements (tables, figures) with descriptions
- Add transitions between sections ("Now let's explore...")
- Prefer shorter sentences (aim for clarity)
- Keep technical accuracy

Target 60-70% of original word count.
"""

    spawn_agent(
        prompt, model="opus", output_file=audio_file, timeout_secs=600,
        allowed_tools=AGENT_TOOLS["synthesis"]
    )


def _generate_audio_multi(state: State, report_dir: Path, report_content: str):
    """Multi-pass audio generation for large reports."""

    audio_file = report_dir / "report_audio.md"

    # Split by major sections
    sections = report_content.split("\n# ")
    audio_parts = []

    for i, section in enumerate(sections):
        if i == 0 and not section.startswith("#"):
            section = "# " + section
        elif i > 0:
            section = "# " + section

        part_file = report_dir / "state" / f"audio_part_{i}.md"

        prompt = f"""Rewrite this section for audio format.

{section[:15000]}

Write to: {part_file}

Use conversational language, expand acronyms, shorter sentences.
Keep technical accuracy.
"""

        result = spawn_agent(
            prompt, model="opus", output_file=part_file, timeout_secs=300,
            allowed_tools=AGENT_TOOLS["synthesis"]
        )
        if result.success and part_file.exists():
            audio_parts.append(part_file.read_text())

    # Combine
    audio_file.write_text("\n\n---\n\n".join(audio_parts))


def _download_papers(state: State, report_dir: Path):
    """Download open-access papers cited in the report."""

    refs_file = report_dir / "refs.md"
    papers_dir = report_dir / "papers"

    if not refs_file.exists():
        print("No refs.md found, skipping paper downloads")
        return

    try:
        from ..papers import PaperDownloader

        print(f"Downloading papers to: {papers_dir}")
        downloader = PaperDownloader(papers_dir)
        results = downloader.download_all(refs_file)

        # Print summary
        print(f"\n{downloader.get_summary()}")

    except ImportError:
        # Fallback if requests not installed
        print("Paper download requires 'requests' library. Skipping.")
    except Exception as e:
        print(f"Paper download error: {e}")
