#!/usr/bin/env python3
"""Watch deep-report progress in real-time.

Usage:
    python3 watch_progress.py ~/reports/<report-dir>
    python3 watch_progress.py --latest
    python3 watch_progress.py --latest --jsonl  # Raw JSON-lines output

This script tails the progress files and displays updates as they happen.
Run this in a separate terminal while the orchestrator is running.
"""

import os
import sys
import time
import json
import logging
import argparse
from pathlib import Path
from datetime import datetime


def find_latest_report() -> Path:
    """Find the most recently modified report directory.

    Searches in order:
    1. DEEP_REPORT_DIR env var (if set)
    2. ~/Documents/deep-reports/
    3. Current working directory
    """
    search_dirs = []

    # Check env var first
    env_dir = os.environ.get("DEEP_REPORT_DIR")
    if env_dir:
        search_dirs.append(Path(env_dir))

    # Default location
    search_dirs.append(Path.home() / "Documents" / "deep-reports")

    # Current working directory
    search_dirs.append(Path.cwd())

    candidates = []
    for reports_dir in search_dirs:
        if not reports_dir.exists():
            continue
        # Check if this dir itself is a report
        state_dir = reports_dir / "state"
        if state_dir.exists():
            try:
                mtime = state_dir.stat().st_mtime
                candidates.append((mtime, reports_dir))
            except OSError:
                pass
        # Check subdirectories
        try:
            for d in reports_dir.iterdir():
                if d.is_dir():
                    sub_state = d / "state"
                    if sub_state.exists():
                        try:
                            mtime = sub_state.stat().st_mtime
                            candidates.append((mtime, d))
                        except OSError:
                            pass
        except OSError:
            pass

    if not candidates:
        searched = ", ".join(str(d) for d in search_dirs if d.exists())
        print(f"No report directories found in: {searched or '(no directories exist)'}")
        sys.exit(1)

    # Return most recent
    candidates.sort(reverse=True)
    return candidates[0][1]


def format_event(event: dict) -> str:
    """Format a JSONL event for display."""
    elapsed = event.get("elapsed_secs", 0)
    mins = int(elapsed // 60)
    secs = int(elapsed % 60)
    timestamp = f"[{mins:02d}:{secs:02d}]"

    event_type = event.get("type", "unknown")

    if event_type == "phase_start":
        return f"\n{'='*60}\n{timestamp} Phase {event['phase']} | STARTING: {event['name']}\n"

    elif event_type == "phase_complete":
        return f"{timestamp} Phase {event['phase']} | COMPLETE: {event['name']}\n{'='*60}\n"

    elif event_type == "agent_start":
        return f"{timestamp} Phase 3 | Agent [{event['current']}/{event['total']}] | {event['agent_id']} starting..."

    elif event_type == "agent_complete":
        status = "✓" if event.get("success") else "✗"
        detail = f"{event['agent_id']} {status}"
        if event.get("duration_secs"):
            detail += f" ({event['duration_secs']:.0f}s)"
        if event.get("retries", 0) > 0:
            detail += f" [{event['retries']} retries]"
        return f"{timestamp} Phase 3 | Agent [{event['done']}/{event['total']}] | {detail}"

    elif event_type == "decision":
        status = "SUFFICIENT" if event.get("sufficient") else "NEEDS MORE"
        reasoning = event.get("reasoning", "")[:100]
        return f"{timestamp} Phase 3 | Decision (iter {event['iteration']}) | {status}: {reasoning}"

    elif event_type == "error":
        return f"{timestamp} Phase {event['phase']} | ERROR | {event['message']}"

    elif event_type == "intervention_needed":
        return f"\n{'!'*60}\n{timestamp} INTERVENTION NEEDED | {event['issue']}\n{'!'*60}\n"

    elif event_type == "approval_waiting":
        return f"{timestamp} WAITING FOR APPROVAL | {event['gate_id']}"

    elif event_type == "approval_received":
        status = "APPROVED" if event.get("approved") else "REJECTED"
        return f"{timestamp} APPROVAL {status} | {event['gate_id']}"

    elif event_type == "summary":
        lines = [f"\n{'='*60}", f"{timestamp} SUMMARY"]
        for key, value in event.items():
            if key not in ("timestamp", "elapsed_secs", "type"):
                lines.append(f"  {key}: {value}")
        lines.append(f"{'='*60}")
        return "\n".join(lines)

    elif event_type == "update":
        line = f"{timestamp} Phase {event['phase']} | {event['step']}"
        if event.get("detail"):
            line += f" | {event['detail']}"
        return line

    else:
        return f"{timestamp} {event_type}: {json.dumps(event)}"


def watch_jsonl(report_dir: Path, follow: bool = True, raw: bool = False):
    """Watch the JSONL progress file for updates."""
    progress_file = report_dir / "state" / "progress.jsonl"
    legacy_file = report_dir / "state" / "progress.log"

    # Determine which file to watch
    use_jsonl = progress_file.exists()
    watch_file = progress_file if use_jsonl else legacy_file

    print(f"{'='*60}")
    print(f"Watching: {report_dir.name}")
    print(f"Progress file: {watch_file}")
    print(f"Format: {'JSON-lines' if use_jsonl else 'Legacy log'}")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")
    print()

    if not watch_file.exists():
        print("Waiting for progress file to be created...")
        while not watch_file.exists() and follow:
            time.sleep(0.5)
            # Check if the other format appeared
            if progress_file.exists():
                watch_file = progress_file
                use_jsonl = True
                break
            elif legacy_file.exists():
                watch_file = legacy_file
                use_jsonl = False
                break

        if not watch_file.exists():
            print("Progress file not found")
            return

    # Read existing content
    try:
        with open(watch_file, encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.rstrip()
                if not line:
                    continue

                if use_jsonl:
                    try:
                        event = json.loads(line)
                        if raw:
                            print(line)
                        else:
                            print(format_event(event))
                    except json.JSONDecodeError:
                        print(line)
                else:
                    print(line)

            last_pos = f.tell()
    except FileNotFoundError:
        last_pos = 0

    if not follow:
        return

    # Follow new content
    print("\n--- Following (Ctrl+C to stop) ---\n")
    try:
        while True:
            try:
                # Check for file truncation (e.g., new write replaced the file)
                try:
                    file_size = os.path.getsize(watch_file)
                    if last_pos > file_size:
                        last_pos = 0
                except OSError:
                    pass

                with open(watch_file, encoding='utf-8', errors='replace') as f:
                    f.seek(last_pos)
                    for line in f:
                        line = line.rstrip()
                        if not line:
                            continue

                        if use_jsonl:
                            try:
                                event = json.loads(line)
                                if raw:
                                    print(line)
                                else:
                                    print(format_event(event))
                            except json.JSONDecodeError:
                                print(line)
                        else:
                            print(line)

                    last_pos = f.tell()
            except FileNotFoundError:
                # File was deleted, wait and retry
                time.sleep(1)
                continue
            except Exception as e:
                logging.debug(f"watch_progress error: {e}")
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("\n\nStopped watching.")


def main():
    parser = argparse.ArgumentParser(
        description="Watch deep-report progress in real-time"
    )
    parser.add_argument("report_dir", nargs="?",
                        help="Report directory to watch")
    parser.add_argument("--latest", action="store_true",
                        help="Watch the most recent report")
    parser.add_argument("--no-follow", action="store_true",
                        help="Print existing content and exit (don't follow)")
    parser.add_argument("--jsonl", action="store_true",
                        help="Output raw JSON-lines instead of formatted text")

    args = parser.parse_args()

    if args.latest or not args.report_dir:
        report_dir = find_latest_report()
    else:
        report_dir = Path(args.report_dir)

    if not report_dir.exists():
        print(f"Report directory not found: {report_dir}")
        sys.exit(1)

    watch_jsonl(report_dir, follow=not args.no_follow, raw=args.jsonl)


if __name__ == "__main__":
    main()
