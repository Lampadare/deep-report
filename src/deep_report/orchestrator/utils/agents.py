#!/usr/bin/env python3
"""Agent spawning utilities for deep-report orchestrator.

Handles spawning Claude agents via CLI and collecting their outputs.
"""

import subprocess
import json
import sys
import time
import os
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Callable, TYPE_CHECKING
from dataclasses import dataclass

if TYPE_CHECKING:
    from ..intervention import InterventionHandler

from ..ui import ui


# Default timeouts (tripled to handle memory pressure and complex tasks)
DEFAULT_TIMEOUT = 5400  # 90 minutes for research/synthesis
DECISION_TIMEOUT = 1080  # 18 minutes
SUMMARY_TIMEOUT = 1620   # 27 minutes
PLANNING_TIMEOUT = 1620  # 27 minutes


# Tool access presets for different agent roles
AGENT_TOOLS = {
    "research": [
        "Read", "Glob", "Grep", "Write",
        # Web search (primary: built-in + exa for full-text; brave as fallback + news)
        "WebSearch", "WebFetch",
        "mcp__exa__web_search_exa",
        "mcp__exa__get_code_context_exa",
        "mcp__exa__company_research_exa",
        "mcp__brave-search__brave_web_search",
        "mcp__brave-search__brave_news_search",
        "mcp__tavily__tavily_search",
        "mcp__tavily__tavily_extract",
        "mcp__tavily__tavily_crawl",
        "mcp__tavily__tavily_map",
        # Library / API docs (universal benefit for any tech topic)
        "mcp__context7__resolve-library-id",
        "mcp__context7__query-docs",
        # Academic papers — arXiv (replaces paper-search.arxiv)
        "mcp__arxiv__search_papers",
        "mcp__arxiv__read_paper",
        "mcp__arxiv__download_paper",
        # Academic papers — PubMed + Europe PMC (replaces paper-search.{pubmed,biorxiv,medrxiv})
        "mcp__pubmed__pubmed_search_articles",
        "mcp__pubmed__pubmed_fetch_articles",
        "mcp__pubmed__pubmed_fetch_fulltext",
        "mcp__pubmed__pubmed_europepmc_search",
        "mcp__pubmed__pubmed_find_related",
        # OpenAlex — cross-discipline discovery + citation graph
        "mcp__openalex__openalex_search_entities",
        "mcp__openalex__openalex_get_citation_graph",
        "mcp__openalex__openalex_resolve_name",
        # (Unpaywall not needed — cyanheads/pubmed-mcp already chains Unpaywall internally)
        # Wikipedia — universal grounding layer
        "mcp__wikipedia__search_wikipedia",
        "mcp__wikipedia__get_summary",
        "mcp__wikipedia__get_article",
        # Page fetching
        "mcp__firecrawl__firecrawl_scrape",
        "mcp__crawl4ai__scrape",
        # Playwright fallback — for JS-heavy / anti-bot pages (restricted subset)
        "mcp__playwright__browser_navigate",
        "mcp__playwright__browser_snapshot",
        "mcp__playwright__browser_wait_for",
        "mcp__playwright__browser_close",
    ],
    "research_fallback": [
        "Read", "Glob", "Grep", "Write", "WebSearch", "WebFetch",
    ],
    "seed_processing": [
        "Read", "Write",
        "mcp__firecrawl__firecrawl_scrape",
        "mcp__crawl4ai__scrape",
    ],
    "seed_processing_fallback": [
        "Read", "Write", "WebSearch", "WebFetch",
    ],
    "summarizer": ["Read", "Write"],
    "synthesis": ["Read", "Glob", "Write"],
    "decision": ["Read"],  # Read-only, output via stdout
}

# Allowed model names
ALLOWED_MODELS = ["sonnet", "opus", "haiku"]

# Docker image for crawl4ai MCP server
_CRAWL4AI_IMAGE = "uysalsadi/crawl4ai-mcp-server:latest"


def _cmd_exists(name: str) -> bool:
    """Check if a command is available on PATH."""
    import shutil
    return shutil.which(name) is not None


def generate_mcp_config(report_dir: Path) -> Optional[Path]:
    """Generate MCP config for agents based on available API keys and tools.

    Only includes servers whose dependencies are present (API keys, binaries).
    Returns path to config file, or None if no search providers available.
    """
    servers = {}

    has_npx = _cmd_exists("npx")

    if os.environ.get("BRAVE_API_KEY") and has_npx:
        servers["brave-search"] = {
            "command": "npx",
            "args": ["-y", "@brave/brave-search-mcp-server"],
            "env": {"BRAVE_API_KEY": os.environ["BRAVE_API_KEY"]},
        }

    if os.environ.get("FIRECRAWL_API_KEY") and has_npx:
        servers["firecrawl"] = {
            "command": "npx",
            "args": ["-y", "firecrawl-mcp"],
            "env": {"FIRECRAWL_API_KEY": os.environ["FIRECRAWL_API_KEY"]},
        }

    # Tavily — agent-optimized search via remote HTTP endpoint (free tier: 1k credits/mo).
    # No Node required — same pattern as Exa.
    tavily_key = os.environ.get("TAVILY_API_KEY")
    if tavily_key:
        servers["tavily"] = {
            "type": "http",
            "url": f"https://mcp.tavily.com/mcp/?tavilyApiKey={tavily_key}",
        }

    # Exa uses HTTP transport with API key passed as query parameter
    exa_key = os.environ.get("EXA_API_KEY")
    if exa_key:
        servers["exa"] = {
            "type": "http",
            "url": f"https://mcp.exa.ai/mcp?exaApiKey={exa_key}",
        }

    # Academic stack — replaces the abandoned paper-search-mcp (broken bioRxiv/medRxiv/PubMed bits)
    import shutil

    # PubMed + Europe PMC + bioRxiv/medRxiv via cyanheads/pubmed-mcp-server
    if has_npx:
        servers["pubmed"] = {
            "command": "npx",
            "args": ["-y", "@cyanheads/pubmed-mcp-server"],
        }
        if os.environ.get("NCBI_API_KEY"):
            servers["pubmed"]["env"] = {"NCBI_API_KEY": os.environ["NCBI_API_KEY"]}

    # OpenAlex — cross-discipline discovery + citation graph (270M works).
    # OPENALEX_API_KEY takes an email — used as the polite-pool contact.
    openalex_contact = os.environ.get("OPENALEX_API_KEY") or os.environ.get("OPENALEX_EMAIL")
    if has_npx and openalex_contact:
        servers["openalex"] = {
            "command": "npx",
            "args": ["-y", "@cyanheads/openalex-mcp-server"],
            "env": {"OPENALEX_API_KEY": openalex_contact},
        }

    # arXiv — prefer installed console script, fall back to uvx
    if shutil.which("arxiv-mcp-server"):
        servers["arxiv"] = {"command": "arxiv-mcp-server", "args": []}
    elif _cmd_exists("uvx"):
        servers["arxiv"] = {"command": "uvx", "args": ["arxiv-mcp-server"]}

    # Wikipedia — universal grounding, no key
    if shutil.which("wikipedia-mcp"):
        servers["wikipedia"] = {"command": "wikipedia-mcp", "args": []}
    elif _cmd_exists("uvx"):
        servers["wikipedia"] = {"command": "uvx", "args": ["wikipedia-mcp"]}

    # Context7 — version-pinned library/API docs. Works keyless; key raises rate limits.
    if has_npx:
        servers["context7"] = {
            "command": "npx",
            "args": ["-y", "@upstash/context7-mcp"],
        }
        if os.environ.get("CONTEXT7_API_KEY"):
            servers["context7"]["env"] = {"CONTEXT7_API_KEY": os.environ["CONTEXT7_API_KEY"]}

    # Playwright (Microsoft) — JS-heavy / anti-bot scrape fallback. Accessibility-tree based.
    if has_npx:
        servers["playwright"] = {
            "command": "npx",
            "args": ["-y", "@playwright/mcp@latest"],
        }

    # crawl4ai via docker — only if docker + image are available (skip if not pulled)
    if _cmd_exists("docker"):
        try:
            r = subprocess.run(
                ["docker", "image", "inspect", _CRAWL4AI_IMAGE],
                capture_output=True, timeout=5,
            )
            if r.returncode == 0:
                servers["crawl4ai"] = {
                    "command": "docker",
                    "args": ["run", "--rm", "-i", _CRAWL4AI_IMAGE],
                }
        except (subprocess.TimeoutExpired, OSError):
            pass

    if os.environ.get("DIGIKEY_CLIENT_ID"):
        digikey_dir = Path.home() / ".local" / "share" / "digikey-mcp"
        if digikey_dir.exists() and _cmd_exists("uv"):
            servers["digikey"] = {
                "command": "uv",
                "args": [
                    "--directory", str(digikey_dir),
                    "run", "python", "digikey_mcp_server.py",
                ],
            }

    # Require at least one search provider (web or academic)
    has_search = any(
        k in servers
        for k in (
            "brave-search", "exa", "firecrawl", "tavily",
            "pubmed", "openalex", "arxiv", "wikipedia", "context7",
        )
    )
    if not has_search:
        if servers:
            ui.verbose(f"MCP servers available ({list(servers)}) but no search provider — skipping MCP config")
        return None

    config = {"mcpServers": servers}
    state_dir = report_dir / "state"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        config_path = state_dir / "mcp.json"
        config_path.write_text(json.dumps(config, indent=2))
        return config_path
    except OSError as e:
        ui.warning(f"Failed to write MCP config: {e} — falling back to WebSearch/WebFetch")
        return None


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
            ui.verbose(f"Circuit breaker open, waiting {remaining:.0f}s...")
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
    estimated_cost: float = 0.0


# Rough cost-per-second estimates by model (based on typical token throughput)
_COST_PER_SEC = {
    "opus": 0.02,
    "sonnet": 0.004,
    "haiku": 0.001,
}


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


def _communicate_streaming(
    process: subprocess.Popen,
    prompt: str,
    timeout_secs: int,
    callback: Callable,
    log_fh=None,
) -> tuple[str, str]:
    """Write prompt and read stdout line-by-line, firing callback for each event.

    Parses stream-json lines to extract tool calls and results.
    Returns (final_text_output, stderr) compatible with communicate().
    """
    # Write prompt and close stdin
    process.stdin.write(prompt)
    process.stdin.close()

    lines = []
    stderr_lines = []
    final_text = ""

    def read_stderr():
        for line in process.stderr:
            stderr_lines.append(line)

    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stderr_thread.start()

    deadline = time.time() + timeout_secs
    try:
        for line in process.stdout:
            lines.append(line)
            if log_fh:
                log_fh.write(line)
                log_fh.flush()
            if time.time() > deadline:
                raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout_secs)

            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                continue

            etype = event.get("type")

            # Extract tool calls from assistant messages
            if etype == "assistant":
                msg = event.get("message", {})
                for block in msg.get("content", []):
                    if block.get("type") == "tool_use":
                        tool_name = block.get("name", "?")
                        tool_input = block.get("input", {})
                        callback("tool_use", tool_name, tool_input)

            # Extract final result text
            elif etype == "result":
                final_text = event.get("result", "")
                cost = event.get("total_cost_usd", 0)
                callback("result", "done", {"cost": cost, "text_len": len(final_text)})

    except subprocess.TimeoutExpired:
        raise

    stderr_thread.join(timeout=5)
    process.wait(timeout=10)

    stderr_str = "".join(stderr_lines)
    # In stream-json mode, the actual text output is in the result event
    return final_text or "".join(lines), stderr_str


def spawn_agent(
    prompt: str,
    model: str = "sonnet",
    output_file: Optional[Path] = None,
    timeout_secs: int = DEFAULT_TIMEOUT,
    cwd: Optional[Path] = None,
    allowed_tools: Optional[list[str]] = None,
    stream_callback: Optional[Callable] = None,
    log_file: Optional[Path] = None,
    mcp_config: Optional[Path] = None,
) -> AgentResult:
    """Spawn a single Claude agent and return its output.

    Args:
        prompt: The prompt to send to Claude
        model: Model to use (sonnet, opus, haiku)
        output_file: If provided, agent will write output here
        timeout_secs: Maximum time to wait
        cwd: Working directory for the agent
        allowed_tools: If provided, restrict agent to these tools only
        log_file: If provided, dump all raw stdout/stderr to this file
        mcp_config: If provided, path to MCP server config file

    Returns:
        AgentResult with success status and output
    """
    result = _spawn_agent_impl(
        prompt, model, output_file, timeout_secs, cwd,
        allowed_tools, stream_callback, log_file, mcp_config,
    )
    ui.add_cost(result.estimated_cost)
    return result


def _spawn_agent_impl(
    prompt: str,
    model: str = "sonnet",
    output_file: Optional[Path] = None,
    timeout_secs: int = DEFAULT_TIMEOUT,
    cwd: Optional[Path] = None,
    allowed_tools: Optional[list[str]] = None,
    stream_callback: Optional[Callable] = None,
    log_file: Optional[Path] = None,
    mcp_config: Optional[Path] = None,
) -> AgentResult:
    """Internal implementation of spawn_agent."""
    start = time.time()

    # Don't spawn if shutdown is in progress
    if process_tracker.is_shutting_down:
        return AgentResult(
            success=False, output="",
            error="Shutdown in progress",
            duration_secs=0.0
        )

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

    # Use stream-json when we have a callback OR a log file (for full conversation capture)
    use_streaming = stream_callback is not None or log_file is not None
    if use_streaming:
        cmd.extend(["--verbose", "--output-format", "stream-json"])

    # Restrict built-in tools AND auto-approve all listed tools (built-in + MCP)
    if allowed_tools:
        # --tools only restricts built-in tools (Read, Write, Bash, etc.)
        builtin_tools = [t for t in allowed_tools if not t.startswith("mcp__")]
        if builtin_tools:
            cmd.extend(["--tools", ",".join(builtin_tools)])
        # --allowedTools auto-approves and denies everything not listed
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])

    # Add MCP server config if provided
    if mcp_config:
        cmd.extend(["--mcp-config", str(mcp_config)])
        # Ignore user's personal MCP servers — only use ours
        cmd.append("--strict-mcp-config")

    process = None
    log_fh = None
    try:
        if log_file:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_fh = open(log_file, "w", encoding="utf-8")

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
            if use_streaming:
                _cb = stream_callback or (lambda *a: None)
                stdout, stderr = _communicate_streaming(
                    process, prompt, timeout_secs, _cb, log_fh=log_fh
                )
            else:
                stdout, stderr = process.communicate(input=prompt, timeout=timeout_secs)
                # Dump full output to log file for non-streaming agents
                if log_fh:
                    log_fh.write(stdout)
                    if stderr:
                        log_fh.write(f"\n--- STDERR ---\n{stderr}")
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
                duration_secs=timeout_secs,
                estimated_cost=_COST_PER_SEC.get(model, 0.004) * timeout_secs,
            )
        finally:
            if not process_tracker.is_shutting_down:
                process_tracker.unregister(process.pid)

        duration = time.time() - start

        if process.returncode == 0:
            cost = _COST_PER_SEC.get(model, 0.004) * duration

            # Check if output file was created
            if output_file and output_file.exists():
                return AgentResult(
                    success=True,
                    output=stdout,
                    duration_secs=duration,
                    output_file=str(output_file),
                    estimated_cost=cost,
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
                    error=f"Output file not created by agent{debug_info}",
                    estimated_cost=cost,
                )
            else:
                return AgentResult(
                    success=True,
                    output=stdout,
                    duration_secs=duration,
                    estimated_cost=cost,
                )
        else:
            return AgentResult(
                success=False,
                output=stdout,
                error=stderr,
                duration_secs=duration,
                estimated_cost=_COST_PER_SEC.get(model, 0.004) * duration,
            )

    except Exception as e:
        if process is not None:
            try:
                process.kill()
                process.wait()
            except Exception:
                pass
            if not process_tracker.is_shutting_down:
                process_tracker.unregister(process.pid)
        elapsed = time.time() - start
        return AgentResult(
            success=False,
            output="",
            error=str(e),
            duration_secs=elapsed,
            estimated_cost=_COST_PER_SEC.get(model, 0.004) * elapsed,
        )
    finally:
        if log_fh:
            try:
                log_fh.close()
            except Exception:
                pass


def spawn_agent_with_retry(
    prompt: str,
    model: str = "sonnet",
    output_file: Optional[Path] = None,
    timeout_secs: int = DEFAULT_TIMEOUT,
    cwd: Optional[Path] = None,
    max_retries: int = 3,
    backoff_base: float = 30.0,
    allowed_tools: Optional[list[str]] = None,
    task_label: Optional[str] = None,
    intervention_handler: "Optional[InterventionHandler]" = None,
    stream_callback: Optional[Callable] = None,
    log_file: Optional[Path] = None,
    mcp_config: Optional[Path] = None,
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
        task_label: Optional label for retry log messages
        intervention_handler: Optional InterventionHandler for interactive mode.
            When provided and a structural error occurs, the user is prompted
            to retry/skip/quit instead of silently returning failure.
        mcp_config: If provided, path to MCP server config file

    Returns:
        AgentResult with success status, output, and retry count
    """
    last_error = None
    total_duration = 0.0
    label = task_label or "agent"

    for attempt in range(max_retries):
        # Bail immediately if shutdown is in progress
        if process_tracker.is_shutting_down:
            return AgentResult(
                success=False, output="",
                error="Shutdown in progress",
                duration_secs=total_duration, retries=attempt
            )

        result = spawn_agent(
            prompt=prompt,
            model=model,
            output_file=output_file,
            timeout_secs=timeout_secs,
            cwd=cwd,
            allowed_tools=allowed_tools,
            stream_callback=stream_callback,
            log_file=log_file,
            mcp_config=mcp_config,
        )
        total_duration += result.duration_secs

        if result.success:
            result.retries = attempt
            return result

        last_error = result.error

        # Don't retry if shutting down
        if process_tracker.is_shutting_down:
            result.retries = attempt
            return result

        # Structural errors: ask user if handler provided, otherwise return failure
        if _is_structural_error(result.error):
            if intervention_handler is not None:
                # Returns True to retry, False to skip; raises KeyboardInterrupt to quit
                should_retry = intervention_handler.categorize_and_handle(
                    result.error, {"task": label}
                )
                if should_retry:
                    continue  # retry from top of loop
            result.retries = attempt
            return result

        # Exponential backoff before retry (interruptible by shutdown)
        if attempt < max_retries - 1:
            wait_time = backoff_base * (2 ** attempt)
            err_msg = str(result.error)[:100] + ("..." if len(str(result.error)) > 100 else "")
            ui.info(f"Retrying {label} in {wait_time:.0f}s... (attempt {attempt+1}/{max_retries})")
            ui.verbose(f"  Error: {err_msg}")
            # Sleep in 1s intervals so we can check shutdown flag
            for _ in range(int(wait_time)):
                if process_tracker.is_shutting_down:
                    return AgentResult(
                        success=False, output="",
                        error="Shutdown in progress",
                        duration_secs=total_duration, retries=attempt
                    )
                time.sleep(1)

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
    intervention_handler: "Optional[InterventionHandler]" = None,
    stream_callback_factory: Optional[Callable] = None,
    stagger_secs: float = 0.0,
    log_dir: Optional[Path] = None,
) -> dict[str, AgentResult]:
    """Spawn multiple agents in parallel.

    Args:
        tasks: List of dicts with keys: id, prompt, model, output_file
        max_workers: Maximum concurrent agents
        on_complete: Callback(task_id, result) called after each completion
        circuit_breaker: Optional circuit breaker to pause on repeated failures
        intervention_handler: Optional InterventionHandler for interactive mode
        stream_callback_factory: Optional factory(task_id) -> callback for streaming
        stagger_secs: Delay between task submissions to avoid API thundering herd
        log_dir: If provided, dump full agent output to log_dir/<task_id>.jsonl

    Returns:
        Dict mapping task_id to AgentResult
    """
    results = {}

    # Use default circuit breaker if none provided
    cb = circuit_breaker or CircuitBreaker(failure_threshold=3, reset_timeout=60.0)

    # Process completions immediately via done callbacks (not after all submissions)
    results_lock = threading.Lock()

    def _on_done(future, task_id):
        try:
            result = future.result()
        except BaseException as e:
            result = AgentResult(success=False, output="", error=str(e))

        # Update circuit breaker
        if result.success:
            cb.record_success()
        else:
            is_open = cb.record_failure()
            if is_open:
                ui.verbose(f"Circuit breaker opened after {cb.failure_threshold} consecutive failures")

        with results_lock:
            results[task_id] = result

        if on_complete:
            try:
                on_complete(task_id, result)
            except Exception as e:
                ui.warning(f"on_complete callback failed for {task_id}: {e}")

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = []

        for i, task in enumerate(tasks):
            if process_tracker.is_shutting_down:
                break

            # Atomic circuit breaker check: wait_if_needed waits and returns proceed status
            if not cb.wait_if_needed():
                continue

            # Stagger submissions to avoid API thundering herd
            if stagger_secs > 0 and i > 0:
                time.sleep(stagger_secs)

            # Build per-task stream callback if factory provided
            task_stream_cb = None
            if stream_callback_factory:
                task_stream_cb = stream_callback_factory(task.get("id", ""))

            # Build per-task log file if log_dir provided
            task_log_file = None
            if log_dir:
                task_log_file = log_dir / f"{task.get('id', f'task_{i}')}.jsonl"

            task_id = task.get("id", "")

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
                    task_label=task_id,
                    intervention_handler=intervention_handler,
                    stream_callback=task_stream_cb,
                    log_file=task_log_file,
                    mcp_config=Path(task["mcp_config"]) if task.get("mcp_config") else None,
                )
            except RuntimeError:
                # Pool is shutting down
                break
            future.add_done_callback(lambda f, tid=task_id: _on_done(f, tid))
            futures.append(future)

        # Wait for all submitted tasks to finish
        for future in futures:
            try:
                future.result(timeout=None)
            except BaseException:
                pass  # _on_done callback already collected the result

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
    "coverage": {{
        "area name": {{"score": 0-100, "note": "brief status"}},
        "another area": {{"score": 0-100, "note": "brief status"}}
    }},
    "gaps": ["area1 not covered", "area2 missing"],
    "conflicts": ["finding X vs finding Y need resolution"],
    "deepen": ["area1 needs more detail", "area2 needs more sources"]
}}

Coverage scoring guide:
- 80-100: Well covered with strong evidence and multiple sources
- 50-79: Partially covered, has gaps or limited sourcing
- 0-49: Poorly covered, needs significant additional research

Map each major topic area from the research brief to a coverage entry.

If iteration >= {max_iterations}, set sufficient=true regardless.
If there are no significant gaps/conflicts/areas to deepen, set sufficient=true.

IMPORTANT: Even when setting sufficient=true, ALWAYS populate the gaps, conflicts, and
deepen lists with potential improvements or directions worth exploring. These are shown to
the user as optional directions they may choose to pursue. Aim for at least 3-5 suggestions
total across all three lists, even when coverage is strong.
"""

    result = spawn_agent(
        prompt, model="opus", timeout_secs=DECISION_TIMEOUT,
        allowed_tools=AGENT_TOOLS["decision"]
    )

    if not result.success:
        if iteration < max_iterations:
            ui.warning("Decision agent failed — continuing research as a precaution")
            return {
                "sufficient": False,
                "reasoning": "Decision agent failed — defaulting to continue research",
                "gaps": [], "conflicts": [], "deepen": [],
            }
        ui.warning("Decision agent failed on final iteration, marking sufficient")
        return {"sufficient": True, "reasoning": "Decision agent failed on final iteration", "gaps": [], "conflicts": [], "deepen": []}

    # Parse JSON from output
    parsed = _extract_json(result.output)
    if parsed:
        return parsed

    # Default if parsing fails
    if iteration < max_iterations - 1:
        ui.warning("Decision agent output could not be parsed — continuing research as a precaution")
        return {
            "sufficient": False,
            "reasoning": "Could not parse decision output — defaulting to continue research",
            "gaps": [], "conflicts": [], "deepen": [],
        }
    ui.warning("Decision agent output could not be parsed on final iteration, marking sufficient")
    return {"sufficient": True, "reasoning": "Could not parse decision on final iteration", "gaps": [], "conflicts": [], "deepen": []}


