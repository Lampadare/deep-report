"""AI-powered topic analysis for smart interview defaults."""

import json
import re
import threading
from pathlib import Path
from typing import Optional

from .utils import spawn_agent, extract_json


PROFILE_DIR = Path.home() / ".deep-report"
PROFILE_FILE = PROFILE_DIR / "profile.json"

DEFAULT_REPORT_TYPES = [
    {"value": "deep-dive", "description": "In-depth analysis of the latest developments and best practices"},
    {"value": "tutorial", "description": "Step-by-step learning guide with examples"},
    {"value": "comparison", "description": "Side-by-side analysis of approaches or technologies"},
    {"value": "survey", "description": "Broad landscape overview of a field"},
]


def _prepend_to_defaults(value: str, reason: str = "") -> list[dict]:
    """Build report_types list by prepending the AI recommendation to defaults."""
    value = re.sub(r'[^a-z0-9-]', '-', value.strip().lower())
    value = re.sub(r'-+', '-', value).strip('-')
    if not value or value in {e["value"] for e in DEFAULT_REPORT_TYPES}:
        return DEFAULT_REPORT_TYPES
    desc = reason[:120] if reason else f"AI-recommended format for this topic"
    return [{"value": value, "description": desc}] + DEFAULT_REPORT_TYPES


def _validate_report_types(raw: list) -> list[dict]:
    """Validate and sanitize AI-returned report_types array."""
    if not isinstance(raw, list):
        return DEFAULT_REPORT_TYPES
    validated = []
    seen = set()
    for entry in raw[:10]:
        if not isinstance(entry, dict):
            continue
        value = entry.get("value", "")
        desc = entry.get("description", "")
        if not isinstance(value, str) or not value.strip():
            continue
        # Normalize to kebab-case
        value = re.sub(r'[^a-z0-9-]', '-', value.strip().lower())
        value = re.sub(r'-+', '-', value).strip('-')
        if not value or value in seen:
            continue
        seen.add(value)
        validated.append({"value": value, "description": str(desc)[:120]})
    return validated if len(validated) >= 3 else DEFAULT_REPORT_TYPES


class TopicAnalyzer:
    """Analyzes a research topic to recommend interview defaults.

    Runs an AI analysis in a background thread with a short timeout.
    Falls back to keyword matching if the AI call fails or is too slow.
    """

    def __init__(self, topic: str, seed_refs: Optional[str] = None):
        self.topic = topic
        self.seed_refs = seed_refs
        self._result: Optional[dict] = None
        self._event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def analyze_async(self) -> None:
        """Start background AI analysis."""
        self._thread = threading.Thread(target=self._run_analysis, daemon=True)
        self._thread.start()

    def get_recommendations(self, timeout: float = 5.0) -> dict:
        """Wait for AI result, return recommendations or fallback."""
        self._event.wait(timeout=timeout)
        if self._result:
            return self._result
        return self._fallback_analysis()

    def _run_analysis(self) -> None:
        """Call Claude to analyze the topic and recommend settings."""
        try:
            seed_info = ""
            if self.seed_refs:
                seed_path = Path(self.seed_refs)
                if seed_path.is_dir():
                    files = [f.name for f in seed_path.iterdir()
                             if f.is_file() and not f.name.startswith('.')][:10]
                    seed_info = f"Seed files: {', '.join(files)}"
                elif self.seed_refs.startswith("http"):
                    seed_info = f"Seed URLs: {self.seed_refs[:200]}"

            prompt = (
                f"Analyze this research topic and recommend report settings.\n\n"
                f"Topic: {self.topic[:500]}\n"
                f"{seed_info}\n\n"
                f"Return ONLY a JSON object with these fields:\n"
                f"- report_types: array of 5-10 report format options tailored to this topic. "
                f"Each entry has \"value\" (kebab-case, 1-3 words) and \"description\" (1 sentence). "
                f"Include the classic four (deep-dive, tutorial, comparison, survey) plus "
                f"topic-specific formats that would produce a better output. "
                f"Order by relevance to the topic.\n"
                f"- report_type: the recommended value from report_types\n"
                f"- report_type_reason: brief reason\n"
                f"- expertise: beginner|intermediate|expert\n"
                f"- expertise_reason: brief reason\n"
                f"- agent_count: integer 3-30\n"
                f"- agent_count_reason: brief reason\n"
                f"- model: sonnet|opus\n"
                f"- model_reason: brief reason\n"
            )

            result = spawn_agent(
                prompt,
                model="sonnet",
                timeout_secs=60,
                allowed_tools=[],
            )

            if result.success and result.output:
                parsed = extract_json(result.output)
                if parsed and "report_type" in parsed:
                    # Validate report_types if present, or synthesize from report_type
                    if "report_types" in parsed:
                        parsed["report_types"] = _validate_report_types(parsed["report_types"])
                    else:
                        # AI returned report_type but not the array — prepend it to defaults
                        rec = parsed["report_type"]
                        reason = parsed.get("report_type_reason", "")
                        parsed["report_types"] = _prepend_to_defaults(rec, reason)
                    self._result = parsed
        except Exception:
            pass
        finally:
            self._event.set()

    def _fallback_analysis(self) -> dict:
        """Enhanced keyword matching for topic analysis."""
        t = self.topic.lower()

        # Expertise detection
        expert_signals = [
            "advanced", "novel", "optimization", "theorem", "proof",
            "phd", "doctoral", "state-of-the-art", "sota", "frontier",
            "architecture", "mechanism", "formal", "signal chain",
            "impedance", "spectroscopy", "pharmacokinetic", "nanoscale",
            "stochastic", "bayesian", "variational", "lattice",
            "perturbation", "renormalization", "topological",
        ]
        beginner_signals = [
            "introduction", "beginner", "basics", "getting started",
            "what is", "overview", "101", "primer", "guide for",
            "explain", "simple", "easy", "for beginners",
        ]

        if any(_word_boundary_match(s, t) for s in expert_signals):
            expertise = "expert"
            expertise_reason = "Topic suggests advanced/technical content"
        elif any(_word_boundary_match(s, t) for s in beginner_signals):
            expertise = "beginner"
            expertise_reason = "Topic suggests introductory content"
        else:
            expertise = "intermediate"
            expertise_reason = "General topic, intermediate is a good default"

        # Report type detection
        comparison_signals = [" vs ", "versus", "comparison", "compare", "differences between"]
        tutorial_signals = ["how to", "tutorial", "guide", "learn", "step by step", "implement"]
        survey_signals = ["survey", "landscape", "overview of", "review of", "state of"]

        if any(_word_boundary_match(s, t) for s in comparison_signals):
            report_type = "comparison"
            report_type_reason = "Topic involves comparing alternatives"
        elif any(_word_boundary_match(s, t) for s in tutorial_signals):
            report_type = "tutorial"
            report_type_reason = "Topic suggests a how-to or learning guide"
        elif any(_word_boundary_match(s, t) for s in survey_signals):
            report_type = "survey"
            report_type_reason = "Topic suggests a broad landscape review"
        else:
            report_type = "deep-dive"
            report_type_reason = "Deep analysis is the best default for this topic"

        # Agent count heuristic
        word_count = len(t.split())
        if word_count > 30:
            agent_count = 15
            agent_count_reason = "Detailed topic benefits from more agents"
        elif word_count < 8:
            agent_count = 8
            agent_count_reason = "Focused topic needs fewer agents"
        else:
            agent_count = 10
            agent_count_reason = "Standard coverage for this topic"

        return {
            "report_types": DEFAULT_REPORT_TYPES,
            "report_type": report_type,
            "report_type_reason": report_type_reason,
            "expertise": expertise,
            "expertise_reason": expertise_reason,
            "agent_count": agent_count,
            "agent_count_reason": agent_count_reason,
            "model": "sonnet",
            "model_reason": "Sonnet offers good quality at lower cost",
        }


def _word_boundary_match(signal: str, text: str) -> bool:
    """Check if signal appears in text at word boundaries."""
    return bool(re.search(r'\b' + re.escape(signal) + r'\b', text))


class UserProfile:
    """Tracks user preferences across report runs.

    Reads/writes ~/.deep-report/profile.json to inform future recommendations.
    """

    def __init__(self):
        self._data = self._load()

    def _load(self) -> dict:
        """Load profile from disk."""
        try:
            if PROFILE_FILE.exists():
                return json.loads(PROFILE_FILE.read_text(encoding='utf-8', errors='replace'))
        except (json.JSONDecodeError, OSError):
            pass
        return {"history": []}

    def _save(self) -> None:
        """Persist profile to disk."""
        try:
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            PROFILE_FILE.write_text(json.dumps(self._data, indent=2), encoding='utf-8')
        except OSError:
            pass

    def get_preferences(self) -> dict:
        """Return most common choices from history."""
        history = self._data.get("history", [])
        if not history:
            return {}

        prefs = {}
        for key in ("report_type", "expertise_level", "model", "agent_count"):
            values = [h.get(key) for h in history if h.get(key) is not None]
            if values:
                # Most common value
                from collections import Counter
                most_common = Counter(values).most_common(1)
                if most_common:
                    prefs[key] = most_common[0][0]

        return prefs

    def update(self, config: dict) -> None:
        """Record a report's configuration in history."""
        entry = {
            "topic": config.get("topic", ""),
            "report_type": config.get("report_type", ""),
            "expertise_level": config.get("expertise_level", ""),
            "model": config.get("model", ""),
            "agent_count": config.get("agent_count", 10),
        }
        history = self._data.get("history", [])
        history.append(entry)
        # Keep last 50 entries
        self._data["history"] = history[-50:]
        self._save()
