import json

from .agents import (
    spawn_agent,
    spawn_agent_with_retry,
    spawn_agents_parallel,
    spawn_decision_agent,
    AgentResult,
    CircuitBreaker,
    ProcessTracker,
    process_tracker,
    generate_mcp_config,
    extend_allowed_tools_for_imports,
    AGENT_TOOLS,
    DEFAULT_TIMEOUT,
    DECISION_TIMEOUT,
    SUMMARY_TIMEOUT,
    PLANNING_TIMEOUT,
)
from .role_enforcer import RoleEnforcer


def extract_json(text: str) -> dict | None:
    """Extract first JSON object from text using brace counting."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == '\\' and in_string:
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None
