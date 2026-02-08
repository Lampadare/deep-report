import json

from .agents import (
    spawn_agent,
    spawn_agent_with_retry,
    spawn_agents_parallel,
    spawn_decision_agent,
    spawn_summarizer,
    AgentResult,
    CircuitBreaker,
    AGENT_TOOLS,
    DEFAULT_TIMEOUT,
    DECISION_TIMEOUT,
    SUMMARY_TIMEOUT,
    PLANNING_TIMEOUT,
)
from .role_enforcer import RoleEnforcer


def extract_json(text: str) -> dict | None:
    """Extract first JSON object from text.

    Args:
        text: Text that may contain a JSON object

    Returns:
        Parsed JSON dict or None if not found/invalid
    """
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            return None
    return None
