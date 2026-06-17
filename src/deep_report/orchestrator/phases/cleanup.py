#!/usr/bin/env python3
"""Phase 5: Cleanup - Summary, metrics, and finalization."""

import json
from pathlib import Path
from datetime import datetime

from ..state import State
from ..utils import RoleEnforcer
from ..ui import ui, format_duration


def run_cleanup(state: State) -> bool:
    """Run the cleanup phase.

    Generates summary, calculates metrics, and finalizes the report.

    Returns:
        True if cleanup succeeded
    """
    state.current_phase = 5
    state.checkpoint("cleanup_started")

    report_dir = Path(state.report_dir)

    # Calculate metrics
    metrics = _calculate_metrics(state, report_dir)

    # Write summary
    summary_file = report_dir / "SUMMARY.md"
    _write_summary(state, metrics, summary_file)

    # Update manifest
    _finalize_manifest(state, metrics)

    state.mark_phase_complete(5)

    # Print summary
    _print_summary(state, metrics, report_dir)

    return True


def _calculate_metrics(state: State, report_dir: Path) -> dict:
    """Calculate final metrics."""

    metrics = {
        "topic": state.topic,
        "report_type": state.report_type,
        "research_model": state.research_model,
        "completed_at": datetime.now().isoformat(),
    }

    # Count words in report (streaming to avoid loading full content)
    report_file = report_dir / "report.md"
    if report_file.exists():
        try:
            word_count = 0
            with open(report_file, encoding='utf-8', errors='replace') as f:
                for line in f:
                    word_count += len(line.split())
            metrics["report_word_count"] = word_count
        except (OSError, IOError) as e:
            ui.warning(f"Report metrics reading failed: {e}")
            metrics["report_word_count"] = 0
    else:
        metrics["report_word_count"] = 0

    # Count research outputs (streaming to avoid loading full content)
    full_dir = report_dir / "full" / "agents"
    if full_dir.exists():
        raw_words = 0
        for f in full_dir.glob("*.md"):
            raw_words += RoleEnforcer.count_words_streaming(f)
        metrics["raw_research_words"] = raw_words
    else:
        metrics["raw_research_words"] = 0

    # Agent stats
    metrics["agents_planned"] = len(state.threads)
    metrics["agents_completed"] = len(state.completed_threads)
    metrics["agents_failed"] = len(state.failed_threads)

    # Follow-up stats
    completed_followups = len([f for f in state.followups if f.get("status") == "completed"])
    failed_followups = len([f for f in state.followups if f.get("status") == "failed"])
    metrics["followups_completed"] = completed_followups
    metrics["followups_failed"] = failed_followups
    metrics["research_iterations"] = state.research_iteration

    # Paper downloads
    papers_dir = report_dir / "papers"
    if papers_dir.exists():
        metrics["papers_downloaded"] = len(list(papers_dir.glob("*.pdf")))
    else:
        metrics["papers_downloaded"] = 0

    # Reference count
    refs_file = report_dir / "refs.md"
    if refs_file.exists():
        try:
            ref_count = 0
            with open(refs_file, encoding='utf-8', errors='replace') as f:
                for line in f:
                    if line.startswith("- "):
                        ref_count += 1
            metrics["reference_count"] = ref_count
        except (OSError, IOError) as e:
            ui.warning(f"Reference counting failed: {e}")
            metrics["reference_count"] = 0
    else:
        metrics["reference_count"] = 0

    # Audio
    audio_file = report_dir / "report_audio.md"
    metrics["audio_generated"] = audio_file.exists()
    if audio_file.exists():
        try:
            metrics["audio_word_count"] = len(audio_file.read_text(encoding='utf-8', errors='replace').split())
        except (OSError, IOError) as e:
            ui.warning(f"Audio metrics reading failed: {e}")
            metrics["audio_word_count"] = 0

    # Cost (estimated based on activity)
    metrics["estimated_cost"] = state.estimated_cost

    return metrics


def _write_summary(state: State, metrics: dict, summary_file: Path):
    """Write a summary markdown file."""

    lines = [
        f"# Report Summary: {state.topic}",
        "",
        f"**Generated:** {metrics['completed_at']}",
        f"**Report Type:** {state.report_type}",
        f"**Expertise Level:** {state.expertise_level}",
        "",
        "## Metrics",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Report Word Count | {metrics['report_word_count']:,} |",
        f"| Raw Research Words | {metrics['raw_research_words']:,} |",
        f"| Agents Completed | {metrics['agents_completed']}/{metrics['agents_planned']} |",
        f"| Follow-up Threads | {metrics['followups_completed']} completed, {metrics['followups_failed']} failed |",
        f"| Research Iterations | {metrics['research_iterations']} |",
        f"| Papers Downloaded | {metrics['papers_downloaded']} |",
        f"| References | {metrics['reference_count']} |",
        f"| Audio Version | {'Yes' if metrics['audio_generated'] else 'No'} |",
        f"| Estimated Cost | ${metrics['estimated_cost']:.2f} |",
        "",
        "## Files",
        "",
        "- `report.md` - Main research report",
        "- `refs.md` - Compiled references",
    ]

    if metrics["audio_generated"]:
        lines.append("- `report_audio.md` - Audio-friendly version")

    if metrics["papers_downloaded"] > 0:
        lines.append(f"- `papers/` - {metrics['papers_downloaded']} downloaded PDFs")

    lines.extend([
        "- `full/agents/` - Full research outputs",
        "- `summaries/agents/` - Agent summaries",
        "- `state/` - Orchestrator state and plan",
        "",
        "## Research Threads",
        "",
    ])

    for thread in state.threads:
        status = "✓" if thread.get("id") in state.completed_threads else "✗"
        lines.append(f"- [{status}] **{thread.get('id')}**: {thread.get('title', 'Untitled')}")

    if state.followups:
        lines.extend([
            "",
            "## Follow-up Threads",
            "",
        ])
        for fu in state.followups:
            status = "✓" if fu.get("status") == "completed" else "✗"
            lines.append(f"- [{status}] **{fu.get('id')}** ({fu.get('reason')}): {fu.get('focus')}")

    try:
        summary_file.write_text("\n".join(lines), encoding='utf-8')
    except (OSError, PermissionError) as e:
        ui.warning(f"Summary writing failed: {e}")


def _finalize_manifest(state: State, metrics: dict):
    """Update the manifest with final metrics."""

    manifest_file = Path(state.report_dir) / "state" / "manifest.json"

    try:
        if manifest_file.exists():
            manifest = json.loads(manifest_file.read_text(encoding='utf-8', errors='replace'))
        else:
            manifest = {}
    except (OSError, IOError, json.JSONDecodeError) as e:
        ui.warning(f"Manifest reading failed: {e}")
        manifest = {}

    manifest.update({
        "status": "completed",
        "completed_at": metrics["completed_at"],
        "current_phase": 5,
        "current_step": "complete",
        "report_word_count": metrics["report_word_count"],
        "raw_research_words": metrics["raw_research_words"],
        "agents_completed": metrics["agents_completed"],
        "agents_failed": metrics["agents_failed"],
        "research_iterations": metrics["research_iterations"],
        "papers_downloaded": metrics["papers_downloaded"],
        "audio_generated": metrics["audio_generated"],
        "estimated_cost": metrics["estimated_cost"],
        "reference_count": metrics["reference_count"],
    })

    try:
        manifest_file.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    except (OSError, PermissionError) as e:
        ui.warning(f"Manifest writing failed: {e}")

    # Also save final state
    state.save()


def _print_summary(state: State, metrics: dict, report_dir: Path):
    """Print final summary to console."""

    # Compute elapsed wall-clock time if we have a start timestamp
    elapsed_str = None
    if state.created_at:
        try:
            start = datetime.fromisoformat(state.created_at)
            end = datetime.fromisoformat(metrics['completed_at'])
            elapsed_secs = (end - start).total_seconds()
            elapsed_str = format_duration(elapsed_secs)
        except (ValueError, TypeError):
            pass

    # Build cost display: show both estimated and actual if actual is available
    total_cost = state.total_cost
    if total_cost > 0:
        cost_str = f"Estimated: ${metrics['estimated_cost']:.2f} | Actual: ~${total_cost:.2f}"
    else:
        cost_str = f"${metrics['estimated_cost']:.2f}"

    stats = {
        "Report": f"{metrics['report_word_count']:,} words",
        "Raw research": f"{metrics['raw_research_words']:,} words",
        "Agents": f"{metrics['agents_completed']}/{metrics['agents_planned']} completed",
        "Iterations": metrics['research_iterations'],
        "Cost": cost_str,
    }

    if elapsed_str:
        stats["Total time"] = elapsed_str

    if metrics["audio_generated"]:
        stats["Audio"] = f"{metrics.get('audio_word_count', 0):,} words"

    if metrics["papers_downloaded"] > 0:
        stats["Papers"] = f"{metrics['papers_downloaded']} PDFs"

    if metrics["reference_count"] > 0:
        stats["References"] = f"{metrics['reference_count']}"

    stats["Agent outputs"] = f"{report_dir}/full/agents/"

    ui.final_summary(str(report_dir), stats)
