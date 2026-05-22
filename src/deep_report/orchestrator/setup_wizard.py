"""Interactive setup wizard for deep-report's MCP integrations.

Usage:
    deep-report --setup

The wizard:
1. Reports which prereqs (Node, uv, Docker) are present locally.
2. Shows a categorized checkbox list of every MCP in :mod:`mcp_catalog`.
3. Saves the user's enabled set to ``~/.deep-report/mcp_config.json``.
4. Highlights which selected servers are missing required environment
   variables and shows where to get the keys.

Run-time discovery in ``utils/agents.py`` consults this file and only
registers servers in the enabled set (intersected with what's actually
runnable on this host).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import questionary
    from questionary import Choice
except ImportError:
    questionary = None  # type: ignore
    Choice = None       # type: ignore

from .mcp_catalog import (
    CATALOG,
    CATEGORY_LABEL,
    TIER_BADGE,
    TIER_FREE,
    TIER_FREE_TIER,
    TIER_PAID,
    MCPSpec,
    default_enabled_keys,
)
from .ui import ui

CONFIG_PATH = Path.home() / ".deep-report" / "mcp_config.json"


# ──────────────────────────────────────────────────────────────────────
# Prereq detection
# ──────────────────────────────────────────────────────────────────────

def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def detect_prereqs() -> dict[str, bool]:
    return {
        "node": _has("node") and _has("npx"),
        "uv": _has("uv") or _has("uvx"),
        "docker": _has("docker"),
    }


def _spec_runnable(spec: MCPSpec, prereqs: dict[str, bool]) -> tuple[bool, str]:
    """Can this spec run on the current host? Returns (ok, reason_if_not)."""
    if spec.requires_node and not prereqs["node"]:
        return False, "needs Node + npx"
    if spec.requires_uv and not prereqs["uv"]:
        return False, "needs uv / uvx"
    if spec.requires_docker and not prereqs["docker"]:
        return False, "needs Docker"
    return True, ""


def _spec_env_status(spec: MCPSpec) -> tuple[list[str], list[str]]:
    """Return (missing_required, missing_optional) env vars."""
    miss_req = [v for v in spec.required_env if not os.environ.get(v)]
    miss_opt = [v for v in spec.optional_env if not os.environ.get(v)]
    return miss_req, miss_opt


# ──────────────────────────────────────────────────────────────────────
# Config persistence
# ──────────────────────────────────────────────────────────────────────

def load_config() -> Optional[dict]:
    """Read the saved config, or None if the user has never run the wizard."""
    if not CONFIG_PATH.exists():
        return None
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def save_config(enabled: list[str]) -> Path:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "enabled": sorted(enabled),
    }
    CONFIG_PATH.write_text(json.dumps(payload, indent=2) + "\n")
    return CONFIG_PATH


def enabled_keys() -> Optional[set[str]]:
    """Return the user's enabled set, or None if no config has been saved."""
    cfg = load_config()
    if cfg is None:
        return None
    return set(cfg.get("enabled", []))


# ──────────────────────────────────────────────────────────────────────
# Wizard
# ──────────────────────────────────────────────────────────────────────

def _tier_color(tier: str) -> str:
    return {
        TIER_FREE: "green",
        TIER_FREE_TIER: "yellow",
        TIER_PAID: "red",
    }.get(tier, "white")


def _render_choice(spec: MCPSpec, runnable: bool, missing_reason: str) -> "Choice":
    badge = TIER_BADGE.get(spec.tier, spec.tier.upper())
    title = f"{spec.display_name}  [{badge}]  — {spec.summary}"
    if not runnable:
        title += f"   ({missing_reason})"
    return Choice(
        title=title,
        value=spec.key,
        checked=spec.default_enabled and runnable,
        disabled=missing_reason if not runnable else False,
    )


def _print_summary(enabled: set[str]) -> None:
    """Print what's enabled vs what still needs keys."""
    by_cat: dict[str, list[MCPSpec]] = {}
    for s in CATALOG:
        if s.key in enabled:
            by_cat.setdefault(s.category, []).append(s)

    ui.header("Configured")
    if not by_cat:
        ui.warning("No servers enabled — deep-report will fall back to built-in WebSearch/WebFetch.")
        return

    missing_keys: list[tuple[MCPSpec, list[str]]] = []
    for cat, specs in by_cat.items():
        ui.info(f"\n{CATEGORY_LABEL.get(cat, cat).upper()}:")
        for s in specs:
            miss_req, miss_opt = _spec_env_status(s)
            status = "ready" if not miss_req else f"missing {', '.join(miss_req)}"
            ui.info(f"  • {s.display_name} — {status}")
            if miss_req:
                missing_keys.append((s, miss_req))

    if missing_keys:
        ui.header("Action required")
        ui.warning("Some enabled servers need API keys before they'll run.")
        for s, missing in missing_keys:
            ui.info(f"\n  {s.display_name}")
            ui.info(f"    Cost:       {s.cost_note or '(unspecified)'}")
            if s.key_signup_url:
                ui.info(f"    Sign up:    {s.key_signup_url}")
            ui.info(f"    Then set:   {' '.join(f'{v}=...' for v in missing)}")
        ui.info("\nAdd these to your shell rc (e.g. ~/.zshrc) or a project .env file.")


def run_wizard() -> int:
    """Run the interactive setup wizard. Returns process exit code."""
    if questionary is None:
        ui.error("questionary is not installed — required for the setup wizard.")
        ui.info("Run: pip install questionary")
        return 1

    ui.header("deep-report MCP setup")
    ui.info(
        "Pick the MCP servers you want to enable. Defaults are sensible for\n"
        "first-time users. You can re-run this wizard any time."
    )

    # Prereqs
    prereqs = detect_prereqs()
    ui.info("\nPrereqs detected:")
    ui.info(f"  Node + npx : {'yes' if prereqs['node'] else 'no'}")
    ui.info(f"  uv  / uvx  : {'yes' if prereqs['uv']   else 'no'}")
    ui.info(f"  Docker     : {'yes' if prereqs['docker'] else 'no'}")
    if not any(prereqs.values()):
        ui.warning(
            "No external runtimes detected. You'll only be able to use HTTP-based"
            " MCPs (Tavily, Exa) — install Node from https://nodejs.org for the rest."
        )

    # Existing config — pre-select what was enabled before, if any.
    existing = enabled_keys()
    if existing is not None:
        ui.info(f"\nFound existing config at {CONFIG_PATH} — pre-selecting previous picks.")

    # Build checkbox list, grouped by category. questionary doesn't support
    # group headers natively, so we use Separator-style disabled choices.
    choices: list = []
    for cat in CATEGORY_LABEL:
        cat_specs = [s for s in CATALOG if s.category == cat]
        if not cat_specs:
            continue
        choices.append(questionary.Separator(f"\n── {CATEGORY_LABEL[cat]} ──"))
        for spec in cat_specs:
            runnable, reason = _spec_runnable(spec, prereqs)
            choice = _render_choice(spec, runnable, reason)
            # Override default-checked with the saved preference if present.
            if existing is not None:
                choice.checked = spec.key in existing and runnable
            choices.append(choice)

    selection: Optional[list[str]] = questionary.checkbox(
        "Servers to enable (Space to toggle, Enter to confirm):",
        choices=choices,
    ).ask()

    if selection is None:
        ui.info("Cancelled.")
        return 1

    enabled = set(selection)
    path = save_config(sorted(enabled))
    ui.success(f"Saved to {path}")

    _print_summary(enabled)
    return 0
