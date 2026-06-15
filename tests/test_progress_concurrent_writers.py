"""ProgressWriter._write_event holds up under concurrent writers (flock contention).

Validates two invariants we rely on for the machine-mode JSONL stream:
- every line a tailer reads parses as JSON (no interleaved bytes)
- every update() call produces exactly one event (no drops)

A third test covers fd cleanup when flock raises, since _write_event opens an
unmanaged fd before locking.
"""

import fcntl
import json
import os
import resource
import sys
import threading
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deep_report.orchestrator.progress import ProgressWriter


N_THREADS = 8
M_UPDATES = 50
EXPECTED = N_THREADS * M_UPDATES


def _hammer(pw: ProgressWriter, thread_id: int, count: int):
    for i in range(count):
        pw.update(phase=thread_id, step=f"step-{i}", detail=f"t{thread_id}-i{i}")


def _run_concurrent(pw: ProgressWriter):
    threads = [
        threading.Thread(target=_hammer, args=(pw, t, M_UPDATES))
        for t in range(N_THREADS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_concurrent_writes_no_corruption(tmp_path):
    pw = ProgressWriter(tmp_path)
    _run_concurrent(pw)

    progress_file = tmp_path / "state" / "progress.jsonl"
    lines = [ln for ln in progress_file.read_text().splitlines() if ln.strip()]

    # Every line must parse — flock should serialize the os.write calls.
    events = []
    for ln in lines:
        events.append(json.loads(ln))

    assert len(events) == EXPECTED, f"expected {EXPECTED} events, got {len(events)}"


def test_concurrent_writes_no_truncation(tmp_path):
    pw = ProgressWriter(tmp_path)
    _run_concurrent(pw)

    progress_file = tmp_path / "state" / "progress.jsonl"
    lines = [ln for ln in progress_file.read_text().splitlines() if ln.strip()]

    # Reconstruct the (thread_id, step_idx) set and confirm it matches the
    # cartesian product — proves no event was partially written or lost.
    seen = set()
    for ln in lines:
        event = json.loads(ln)
        # Required fields populated by update() + _write_event()
        assert event["type"] == "update"
        assert "timestamp" in event
        assert "elapsed_secs" in event
        assert "phase" in event
        assert "step" in event
        assert "detail" in event
        seen.add((event["phase"], event["step"], event["detail"]))

    expected = {
        (t, f"step-{i}", f"t{t}-i{i}")
        for t in range(N_THREADS)
        for i in range(M_UPDATES)
    }
    assert seen == expected


def test_fd_close_on_exception(tmp_path):
    """If flock raises, _write_event must still close the fd it opened.

    We patch fcntl.flock at the module level so the LOCK_EX call inside
    _write_event raises OSError. The function should log a warning and return,
    but must not leak the fd opened just above. We sample the open-fd count
    before and after a burst of failing writes and assert no growth.
    """
    pw = ProgressWriter(tmp_path)

    def _open_fd_count() -> int:
        # /dev/fd lists every fd the process has open. Works on macOS + Linux.
        return len(os.listdir("/dev/fd"))

    # Warm up the file so the first open doesn't skew the baseline.
    pw.update(phase=0, step="warmup")

    baseline = _open_fd_count()

    real_flock = fcntl.flock

    def _flaky_flock(fd, op):
        # Only fail the exclusive lock acquire; let unlocks (if reached) pass.
        if op & fcntl.LOCK_EX:
            raise OSError("simulated flock failure")
        return real_flock(fd, op)

    with mock.patch("deep_report.orchestrator.progress.fcntl.flock",
                    side_effect=_flaky_flock):
        for _ in range(200):
            pw.update(phase=1, step="boom")

    after = _open_fd_count()

    # Allow tiny slack for unrelated fds (stdout buffering, etc.) but no real leak.
    assert after - baseline <= 2, (
        f"fd leak detected: baseline={baseline}, after={after}"
    )

    # Sanity: the soft limit hasn't been approached either.
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    assert after < soft
