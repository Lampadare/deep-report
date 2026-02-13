#!/usr/bin/env python3
"""Agent spawning utilities for deep-report orchestrator.

Handles spawning Claude agents via CLI and collecting their outputs.
"""

import subprocess
import json
import time
import os
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Callable
from dataclasses import dataclass


# Default timeouts (tripled to handle memory pressure and complex tasks)
DEFAULT_TIMEOUT = 5400  # 90 minutes for research/synthesis
DECISION_TIMEOUT = 1080  # 18 minutes
SUMMARY_TIMEOUT = 1620   # 27 minutes
PLANNING_TIMEOUT = 1620  # 27 minutes


# Tool access presets for different agent roles
AGENT_TOOLS = {
    "research": ["Read", "Glob", "Grep", "WebSearch", "WebFetch", "Write"],
    "summarizer": ["Read", "Write"],
    "synthesis": ["Read", "Glob", "Write"],
    "decision": ["Read"],  # Read-only, output via stdout
}

# Allowed model names
ALLOWED_MODELS = ["sonnet", "opus", "haiku"]


def _extract_json(text: str) -> dict | None:
    """Extract first complete JSON object from text using brace matching."""
    start = text.find("{")
    if start < 0:
        return None

    # Use brace counting to find matching close brace
    depth = 0
    in_string = False
    escape_next = False

    for i, char in enumerate(text[start:], start):
        if escape_next:
            escape_next = False
            continue
        if char == '\\' and in_string:
            escape_next = True
            continue
        if char == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i+1])
                except json.JSONDecodeError:
                    return None
    return None


class CircuitBreaker:
    """Circuit breaker to pause after repeated failures.

    Opens after failure_threshold consecutive failures, then
    auto-resets after reset_timeout seconds. Thread-safe.
    """

    def __init__(self, failure_threshold: int = 3, reset_timeout: float = 60.0):
        self.failure_count = 0
        self.failure_threshold = failure_threshold
        self.last_failure_time = 0.0
        self.reset_timeout = reset_timeout
        self.is_open = False
        self._lock = threading.Lock()

    def record_failure(self) -> bool:
        """Record a failure. Opens circuit if threshold exceeded.

        Returns:
            True if circuit is now open, False otherwise
        """
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            if self.failure_count >= self.failure_threshold:
                self.is_open = True
            return self.is_open

    def record_success(self):
        """Record a success. Resets failure count."""
        with self._lock:
            self.failure_count = 0
            self.is_open = False

    def should_proceed(self) -> bool:
        """Check if operations should proceed.

        Returns:
            True if circuit is closed or timeout has elapsed
        """
        with self._lock:
            if not self.is_open:
                return True
            # Check if timeout elapsed (half-open state)
            if time.time() - self.last_failure_time > self.reset_timeout:
                self.is_open = False
                self.failure_count = 0
                return True
            return False

    def wait_if_needed(self) -> bool:
        """Wait for circuit to close if needed.

        Returns:
            True if we can proceed, False if still blocked after waiting
        """
        with self._lock:
            if not self.is_open:
                return True
            # Check if timeout elapsed (half-open state)
            if time.time() - self.last_failure_time > self.reset_timeout:
                self.is_open = False
                self.failure_count = 0
                return True
            remaining = self.reset_timeout - (time.time() - self.last_failure_time)

        # Sleep outside lock to avoid blocking other threads
        if remaining > 0:
            print(f"  Circuit breaker open, waiting {remaining:.0f}s...")
            time.sleep(remaining)

        # Re-acquire lock and re-check state before resetting
        with self._lock:
            # Only reset if still open (another thread may have reset it)
            if self.is_open:
                self.is_open = False
                self.failure_count = 0
        return True


class ProcessTracker:
    """Tracks spawned child processes for clean shutdown on Ctrl+C."""

    def __init__(self):
        self._processes: dict[int, subprocess.Popen] = {}
        self._lock = threading.RLock()
        self._shutting_down = False

    def register(self, proc: subprocess.Popen):
        with self._lock:
            self._processes[proc.pid] = proc

    def unregister(self, pid: int):
        with self._lock:
            self._processes.pop(pid, None)

    @property
    def is_shutting_down(self) -> bool:
        return self._shutting_down

    def shutdown(self, timeout: float = 10.0):
        """SIGTERM all tracked processes, wait, then SIGKILL survivors."""
        import signal as _signal
        acquired = self._lock.acquire(timeout=1)
        try:
            self._shutting_down = True
            pids = list(self._processes.keys())
            procs = list(self._processes.values())
        finally:
            if acquired:
                self._lock.release()

        for pid in pids:
            try:
                os.killpg(os.getpgid(pid), _signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass

        deadline = time.time() + timeout
        for proc in procs:
            remaining = max(0.1, deadline - time.time())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                pass

        for pid in pids:
            try:
                os.killpg(os.getpgid(pid), _signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass

        acquired = self._lock.acquire(timeout=1)
        try:
            self._processes.clear()
        finally:
            if acquired:
                self._lock.release()


process_tracker = ProcessTracker()


@dataclass
class AgentResult:
    """Result from an agent execution."""
    success: bool
    output: str
    error: str = ""
    duration_secs: float = 0.0
    output_file: Optional[str] = None
    retries: int = 0


def _is_structural_error(error: str) -> bool:
    """Check if error requires user intervention (no point retrying)."""
    structural_patterns = [
        "permission denied",
        "authentication",
        "rate limit",
        "billing",
        "api key",
        "not found",
        "quota",
        "unauthorized",
    ]
    error_lower = error.lower()
    return any(p in error_lower for p in structural_patterns)


def spawn_agent(
    prompt: str,
    model: str = "sonnet",
    output_file: Optional[Path] = None,
    timeout_secs: int = DEFAULT_TIMEOUT,
    cwd: Optional[Path] = None,
    allowed_tools: Optional[list[str]] = None,
) -> AgentResult:
    """Spawn a single Claude agent and return its output.

    Args:
        prompt: The prompt to send to Claude
        model: Model to use (sonnet, opus, haiku)
        output_file: If provided, agent will write output here
        timeout_secs: Maximum time to wait
        cwd: Working directory for the agent
        allowed_tools: If provided, restrict agent to these tools only

    Returns:
        AgentResult with success status and output
    """
    start = time.time()

    # Validate model parameter
    if model not in ALLOWED_MODELS:
        return AgentResult(
            success=False,
            output="",
            error=f"Invalid model '{model}'. Allowed: {ALLOWED_MODELS}",
            duration_secs=0.0
        )

    # Build the command
    cmd = ["claude", "--print", "--model", model]

    # Add tool restrictions if specified
    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])

    process = None
    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd or os.getcwd(),
            start_new_session=True,  # Create new process group for clean termination
        )
        process_tracker.register(process)
        try:
            stdout, stderr = process.communicate(input=prompt, timeout=timeout_secs)
        except subprocess.TimeoutExpired:
            # Kill the entire process group to ensure all children are terminated
            import signal
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                # Fallback if process group kill fails
                process.kill()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
            return AgentResult(
                success=False,
                output="",
                error=f"Agent timed out after {timeout_secs}s",
                duration_secs=timeout_secs
            )
        finally:
            process_tracker.unregister(process.pid)

        duration = time.time() - start

        if process.returncode == 0:
            # Check if output file was created
            if output_file and output_file.exists():
                return AgentResult(
                    success=True,
                    output=stdout,
                    duration_secs=duration,
                    output_file=str(output_file)
                )
            elif output_file:
                # Agent didn't write the file - mark as failure
                # Include stderr and last part of stdout for debugging
                debug_info = ""
                if stderr:
                    debug_info = f" stderr: {stderr[:200]}"
                elif stdout:
                    debug_info = f" (agent output: {stdout[-300:][:150]}...)"
                return AgentResult(
                    success=False,
                    output=stdout,
                    duration_secs=duration,
                    error=f"Output file not created by agent{debug_info}"
                )
            else:
                return AgentResult(
                    success=True,
                    output=stdout,
                    duration_secs=duration
                )
        else:
            return AgentResult(
                success=False,
                output=stdout,
                error=stderr,
                duration_secs=duration
            )

    except Exception as e:
        if process is not None:
            try:
                process.kill()
                process.wait()
            except Exception:
                pass
            process_tracker.unregister(process.pid)
        return AgentResult(
            success=False,
            output="",
            error=str(e),
            duration_secs=time.time() - start
        )


def spawn_agent_with_retry(
    prompt: str,
    model: str = "sonnet",
    output_file: Optional[Path] = None,
    timeout_secs: int = DEFAULT_TIMEOUT,
    cwd: Optional[Path] = None,
    max_retries: int = 3,
    backoff_base: float = 30.0,
    allowed_tools: Optional[list[str]] = None,
) -> AgentResult:
    """Spawn agent with exponential backoff retry.

    Args:
        prompt: The prompt to send to Claude
        model: Model to use (sonnet, opus)
        output_file: If provided, agent will write output here
        timeout_secs: Maximum time to wait per attempt
        cwd: Working directory for the agent
        max_retries: Maximum retry attempts
        backoff_base: Base wait time for exponential backoff
        allowed_tools: If provided, restrict agent to these tools only

    Returns:
        AgentResult with success status, output, and retry count
    """
    last_error = None
    total_duration = 0.0

    for attempt in range(max_retries):
        result = spawn_agent(
            prompt=prompt,
            model=model,
            output_file=output_file,
            timeout_secs=timeout_secs,
            cwd=cwd,
            allowed_tools=allowed_tools,
        )
        total_duration += result.duration_secs

        if result.success:
            result.retries = attempt
            return result

        last_error = result.error

        # Check if error is structural (no point retrying)
        if _is_structural_error(result.error):
            result.retries = attempt
            return result

        # Exponential backoff before retry
        if attempt < max_retries - 1:
            wait_time = backoff_base * (2 ** attempt)
            print(f"  Retry {attempt+1}/{max_retries} in {wait_time:.0f}s... (error: {result.error[:50]})")
            time.sleep(wait_time)

    return AgentResult(
        success=False,
        output="",
        error=f"Failed after {max_retries} retries. Last error: {last_error}",
        duration_secs=total_duration,
        retries=max_retries
    )


def spawn_agents_parallel(
    tasks: list[dict],
    max_workers: int = 5,
    on_complete: Optional[Callable] = None,
    circuit_breaker: Optional[CircuitBreaker] = None,
) -> dict[str, AgentResult]:
    """Spawn multiple agents in parallel.

    Args:
        tasks: List of dicts with keys: id, prompt, model, output_file
        max_workers: Maximum concurrent agents
        on_complete: Callback(task_id, result) called after each completion
        circuit_breaker: Optional circuit breaker to pause on repeated failures

    Returns:
        Dict mapping task_id to AgentResult
    """
    results = {}

    # Use default circuit breaker if none provided
    cb = circuit_breaker or CircuitBreaker(failure_threshold=3, reset_timeout=60.0)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}

        for task in tasks:
            # Atomic circuit breaker check: wait_if_needed waits and returns proceed status
            if not cb.wait_if_needed():
                continue

            # Use retry wrapper for reliability
            try:
                future = pool.submit(
                    spawn_agent_with_retry,
                    prompt=task["prompt"],
                    model=task.get("model", "sonnet"),
                    output_file=Path(task["output_file"]) if task.get("output_file") else None,
                    timeout_secs=task.get("timeout_secs", DEFAULT_TIMEOUT),
                    cwd=Path(task["cwd"]) if task.get("cwd") else None,
                    max_retries=task.get("max_retries", 3),
                    allowed_tools=task.get("allowed_tools"),
                )
            except RuntimeError:
                # Pool is shutting down
                break
            futures[future] = task["id"]

        for future in as_completed(futures):
            task_id = futures[future]
            try:
                result = future.result()
            except Exception as e:
                result = AgentResult(success=False, output="", error=str(e))

            # Update circuit breaker
            if result.success:
                cb.record_success()
            else:
                is_open = cb.record_failure()
                if is_open:
                    print(f"  Circuit breaker opened after {cb.failure_threshold} consecutive failures")

            results[task_id] = result

            if on_complete:
                try:
                    on_complete(task_id, result)
                except Exception as e:
                    print(f"  Warning: on_complete callback failed for {task_id}: {e}")

    return results


def spawn_decision_agent(
    summaries: list[str],
    topic: str,
    iteration: int,
    max_iterations: int,
) -> dict:
    """Spawn a decision agent to evaluate research progress.

    Returns a dict with:
        - sufficient: bool - whether research is sufficient
        - gaps: list of areas needing more research
        - conflicts: list of conflicting findings to resolve
        - deepen: list of areas to explore more deeply
    """
    prompt = f"""You are a research evaluation agent. Review these summaries from iteration {iteration} of {max_iterations} max.

## Topic
{topic}

## Agent Summaries
{chr(10).join(summaries)}

## Task
Evaluate whether the research is sufficient or needs more depth.

Return a JSON object (and ONLY a JSON object, no other text):
{{
    "sufficient": true/false,
    "reasoning": "1-2 sentence explanation",
    "gaps": ["area1 not covered", "area2 missing"],
    "conflicts": ["finding X vs finding Y need resolution"],
    "deepen": ["area1 needs more detail", "area2 needs more sources"]
}}

If iteration >= {max_iterations}, set sufficient=true regardless.
If there are no significant gaps/conflicts/areas to deepen, set sufficient=true.
Be conservative - only request more research if genuinely needed.
"""

    result = spawn_agent(
        prompt, model="opus", timeout_secs=DECISION_TIMEOUT,
        allowed_tools=AGENT_TOOLS["decision"]
    )

    if not result.success:
        # Default to sufficient if agent fails
        return {"sufficient": True, "reasoning": "Decision agent failed", "gaps": [], "conflicts": [], "deepen": []}

    # Parse JSON from output
    parsed = _extract_json(result.output)
    if parsed:
        return parsed

    # Default if parsing fails
    return {"sufficient": True, "reasoning": "Could not parse decision", "gaps": [], "conflicts": [], "deepen": []}


