#!/usr/bin/env python3
"""Phase 1: Setup - Directory creation, config, seed processing."""

import os
import re
import sys
import json
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional

from ..state import State
from ..utils import spawn_agent, AgentResult, AGENT_TOOLS


def _sanitize_topic(topic: str) -> str:
    """Sanitize topic to prevent path traversal and shell injection."""
    # Remove path traversal attempts
    topic = topic.replace("..", "").replace("/", " ").replace("\\", " ")
    # Remove shell metacharacters
    topic = re.sub(r'[;&|`$]', '', topic)
    # Collapse whitespace
    topic = re.sub(r'\s+', ' ', topic)
    return topic.strip()


def _is_writable(path: Path) -> bool:
    """Check if a directory is writable."""
    try:
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        test_file = path / ".write_test"
        test_file.touch()
        test_file.unlink()
        return True
    except (PermissionError, OSError):
        return False


def _determine_output_dir(args: dict) -> Path:
    """Determine output directory with smart defaults.

    Priority:
    1. Explicit --output flag
    2. cwd if writable
    3. Fallback to ~/Documents/deep-reports/
    """
    topic = args.get("topic", "research")
    slug = _slugify(topic)[:50]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    report_name = f"{slug}_{timestamp}"

    # 1. Explicit --output flag takes priority
    if args.get("report_dir"):
        return Path(args["report_dir"])

    # 2. Default to cwd if writable
    cwd = Path(args.get("cwd") or os.getcwd())
    cwd_output = cwd / report_name
    if _is_writable(cwd):
        return cwd_output

    # 3. Fallback to ~/Documents/deep-reports/
    fallback = Path.home() / "Documents" / "deep-reports" / report_name
    fallback.parent.mkdir(parents=True, exist_ok=True)
    return fallback


def _auto_detect_seeds(cwd: Path) -> Optional[Path]:
    """Auto-detect seed refs folder in cwd.

    Looks for common seed folder names and returns the first non-empty one.
    """
    candidates = ["seed-refs", "seeds", "references", "refs"]
    for name in candidates:
        path = cwd / name
        if path.is_dir():
            # Check if it has any files
            files = [f for f in path.iterdir() if f.is_file()]
            if files:
                return path
    return None


def run_setup(state: State, args: dict) -> bool:
    """Run the setup phase.

    Args:
        state: The orchestrator state
        args: Dict with keys:
            - topic: Research topic (required)
            - report_dir: Where to create report (optional, defaults to ~/reports/<topic-slug>)
            - model: Research model (sonnet/opus, default sonnet)
            - agent_count: Number of research agents (default 10)
            - seed_refs: Path to folder or list of URLs
            - download_papers: Whether to download cited papers
            - generate_audio: Whether to generate audio version
            - expertise_level: beginner/intermediate/expert
            - report_type: state-of-the-art/tutorial/comparison/survey

    Returns:
        True if setup succeeded, False otherwise
    """
    # Extract args
    topic = args.get("topic", "").strip()
    if not topic:
        print("ERROR: No topic provided")
        return False

    # Sanitize topic before any use
    topic = _sanitize_topic(topic)
    if not topic:
        print("ERROR: Topic is empty after sanitization")
        return False

    state.topic = topic

    # Determine report directory using new logic
    report_dir = _determine_output_dir(args)
    state.report_dir = str(report_dir)

    # Set state file path early, before any save() or checkpoint() calls
    state._state_file = str(Path(report_dir) / "state" / "orchestrator_state.json")

    state.checkpoint("setup_started")

    # Store config
    state.research_model = args.get("model", "sonnet")
    state.agent_count = min(args.get("agent_count", 10), 30)
    state.download_papers = args.get("download_papers", True)
    state.generate_audio = args.get("generate_audio", False)
    state.expertise_level = args.get("expertise_level", "intermediate")
    state.report_type = args.get("report_type", "state-of-the-art")
    state.seed_urls = args.get("seed_urls", [])
    state.seed_refs_folder = args.get("seed_refs_folder")

    # Auto-detect seeds if not explicitly provided
    if not state.seed_refs_folder and not state.seed_urls:
        cwd = Path(args.get("cwd") or os.getcwd())
        auto_seeds = _auto_detect_seeds(cwd)
        if auto_seeds:
            print(f"Auto-detected seed refs: {auto_seeds}")
            state.seed_refs_folder = str(auto_seeds)

    state.save()
    state.checkpoint("config_saved")

    # Create directory structure
    print(f"Creating report directory: {report_dir}")
    _create_directories(report_dir)
    state.checkpoint("directories_created")

    # Write initial manifest
    _write_manifest(state)
    state.checkpoint("manifest_written")

    # Process seeds if provided
    seeds_to_process = []

    if state.seed_refs_folder:
        folder = Path(state.seed_refs_folder)
        if folder.exists():
            seeds_to_process.extend([str(f) for f in folder.iterdir() if f.is_file()])

    if state.seed_urls:
        seeds_to_process.extend(state.seed_urls)

    if seeds_to_process:
        print(f"Processing {len(seeds_to_process)} seed references...")
        success = _process_seeds(state, seeds_to_process)
        if not success:
            print("WARNING: Seed processing had errors, continuing anyway")
        state.seeds_processed = True
        state.checkpoint("seeds_processed")

        # Summarize seeds
        print("Summarizing seeds...")
        _summarize_seeds(state)
        state.seeds_summarized = True
        state.checkpoint("seeds_summarized")

    # Write scope document
    print("Writing scope document...")
    _write_scope(state)
    state.scope_written = True
    state.checkpoint("scope_written")

    state.mark_phase_complete(1)
    print("Phase 1 (Setup) complete")
    return True


def _slugify(text: str) -> str:
    """Convert text to URL-safe slug."""
    import re
    text = text.lower()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_-]+', '-', text)
    return text.strip('-')


def _create_directories(report_dir: Path):
    """Create the full directory structure."""
    dirs = [
        report_dir / "full" / "agents",
        report_dir / "full" / "seeds",
        report_dir / "summaries" / "agents",
        report_dir / "summaries" / "seeds",
        report_dir / "state",
        report_dir / "papers",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


def _write_manifest(state: State):
    """Write the manifest.json file."""
    manifest = {
        "topic": state.topic,
        "created_at": state.created_at,
        "research_model": state.research_model,
        "agent_count": state.agent_count,
        "download_papers": state.download_papers,
        "generate_audio": state.generate_audio,
        "expertise_level": state.expertise_level,
        "report_type": state.report_type,
        "current_phase": state.current_phase,
        "current_step": state.current_step,
    }
    manifest_path = Path(state.report_dir) / "state" / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))


def _process_seeds(state: State, seeds: list[str]) -> bool:
    """Process seed references using the process_seeds.py script."""
    script_path = Path(__file__).parent.parent.parent / "references" / "process_seeds.py"

    if not script_path.exists():
        print(f"WARNING: process_seeds.py not found at {script_path}")
        # Fallback: spawn an agent to process seeds
        return _process_seeds_via_agent(state, seeds)

    cmd = [
        sys.executable,
        str(script_path),
        state.report_dir,
    ] + seeds + [
        "--topic", state.topic
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            print(f"Seed processing error: {result.stderr}")
            return False
        return True
    except subprocess.TimeoutExpired:
        print("Seed processing timed out")
        return False
    except Exception as e:
        print(f"Seed processing failed: {e}")
        return False


def _process_seeds_via_agent(state: State, seeds: list[str]) -> bool:
    """Fallback: use a Claude agent to process seeds."""
    report_dir = Path(state.report_dir)

    for i, seed in enumerate(seeds):
        output_file = report_dir / "full" / "seeds" / f"seed_{i+1}.md"

        if seed.startswith("http"):
            prompt = f"""Fetch and extract the main content from this URL: {seed}

Write the extracted content (title, main text, key data) to: {output_file}

Focus on factual information relevant to the topic: {state.topic}
Remove navigation, ads, and boilerplate.
"""
        else:
            prompt = f"""Read and summarize this file: {seed}

Write a summary of key content to: {output_file}

Focus on information relevant to the topic: {state.topic}
"""

        result = spawn_agent(
            prompt, model="sonnet", output_file=output_file, timeout_secs=300,
            allowed_tools=["Read", "WebSearch", "WebFetch", "Write"]
        )
        if not result.success:
            print(f"Failed to process seed {seed}: {result.error}")

    return True


def _summarize_seeds(state: State):
    """Summarize all processed seeds.

    Reads content directly and passes to summarizer to avoid Read tool failures.
    """
    report_dir = Path(state.report_dir)
    seeds_dir = report_dir / "full" / "seeds"
    summaries_dir = report_dir / "summaries" / "seeds"

    if not seeds_dir.exists():
        return

    for seed_file in seeds_dir.glob("*.md"):
        summary_file = summaries_dir / seed_file.name

        # Read content here and pass directly
        try:
            content = seed_file.read_text()
        except Exception as e:
            print(f"Failed to read {seed_file}: {e}")
            continue

        prompt = f"""Summarize this seed reference for the topic: {state.topic}

<content>
{content}
</content>

Write a concise summary (300-500 words) to: {summary_file}

Include:
- Key findings and data points
- Relevance to topic: {state.topic}
- Source/citation info if present
"""

        result = spawn_agent(
            prompt, model="haiku", output_file=summary_file, timeout_secs=120,
            allowed_tools=["Write"]  # Only needs Write when content is passed
        )
        if not result.success:
            print(f"Failed to summarize {seed_file.name}: {result.error}")


def _write_scope(state: State):
    """Write a scope document based on topic and seeds."""
    report_dir = Path(state.report_dir)
    scope_file = report_dir / "state" / "scope.md"

    # Gather seed summaries if they exist
    seed_context = ""
    summaries_dir = report_dir / "summaries" / "seeds"
    if summaries_dir.exists():
        for f in summaries_dir.glob("*.md"):
            content = f.read_text()[:2000]  # Limit per seed
            seed_context += f"\n### {f.stem}\n{content}\n"

    prompt = f"""Write a research scope document for this topic: {state.topic}

Report type: {state.report_type}
Expertise level: {state.expertise_level}
Number of research agents: {state.agent_count}

{f"## Seed Material Context{seed_context}" if seed_context else ""}

Write the scope document to: {scope_file}

Include:
1. Research objectives (3-5 bullet points)
2. Key questions to answer
3. Boundaries (what's in scope vs out of scope)
4. Expected sections for the final report
5. Quality criteria

Keep it under 1000 words.
"""

    result = spawn_agent(
        prompt, model="opus", output_file=scope_file, timeout_secs=180,
        allowed_tools=["Read", "Write"]
    )
    if not result.success:
        # Write a minimal scope if agent fails
        scope_file.write_text(f"""# Research Scope: {state.topic}

## Objectives
- Provide comprehensive coverage of {state.topic}
- Synthesize current state of knowledge
- Identify key findings and open questions

## Report Type
{state.report_type}

## Target Audience
{state.expertise_level} level readers
""")
