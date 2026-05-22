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
    BY_KEY,
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

# Names we never import even if a user has them in CC — these are either already
# in the catalog (catalog wins) or actively retired by deep-report.
_IMPORT_BLOCKLIST = {"paper-search"}  # replaced by cyanheads/pubmed-mcp-server


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


def _summarize_cc_entry(cfg: dict) -> str:
    """Short human label for a CC-imported server config."""
    if cfg.get("type") == "http":
        url = cfg.get("url", "")
        host = url.split("/")[2] if url.count("/") >= 2 else url
        return f"http → {host}"
    cmd = cfg.get("command", "?")
    args = cfg.get("args", [])
    # Show first non-flag arg if present (e.g. the npm package name)
    pkg = next((a for a in args if not a.startswith("-")), "")
    return f"{cmd} {pkg}".strip()


def discover_cc_servers() -> dict[str, dict]:
    """Return MCP servers configured in Claude Code that aren't in our catalog.

    Reads:
    - ``~/.claude.json`` (top-level ``mcpServers``)
    - ``~/.claude.json`` per-project block matching cwd (or any ancestor)
    - ``.mcp.json`` in cwd

    Excludes any name that matches a catalog key (catalog wins) or appears
    on the import blocklist.
    """
    found: dict[str, dict] = {}

    def consider(name: str, cfg: dict) -> None:
        if name in BY_KEY or name in _IMPORT_BLOCKLIST or name in found:
            return
        if not isinstance(cfg, dict):
            return
        found[name] = cfg

    def iter_servers(maybe_dict) -> list[tuple[str, dict]]:
        """Defensive iteration over a dict-shaped value. Returns [] for any
        non-dict (handles hand-edits like ``"mcpServers": "oops"`` or null)."""
        if not isinstance(maybe_dict, dict):
            return []
        return list(maybe_dict.items())

    # User-scope ~/.claude.json
    claude_json = Path.home() / ".claude.json"
    if claude_json.exists():
        try:
            data = json.loads(claude_json.read_text())
        except json.JSONDecodeError:
            ui.warning(f"~/.claude.json is not valid JSON — skipping CC import discovery")
            data = {}
        except OSError:
            data = {}
        if not isinstance(data, dict):
            data = {}

        for name, cfg in iter_servers(data.get("mcpServers")):
            consider(name, cfg)
        # Project scope — any project whose path is the cwd or an ancestor
        try:
            cwd = Path.cwd().resolve()
        except OSError:
            cwd = None
        if cwd is not None:
            projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
            for raw_path, proj in projects.items():
                try:
                    proj_path = Path(raw_path).resolve()
                except (OSError, ValueError, TypeError):
                    continue
                if proj_path == cwd or cwd.is_relative_to(proj_path):
                    for name, cfg in iter_servers(
                        proj.get("mcpServers") if isinstance(proj, dict) else None
                    ):
                        consider(name, cfg)

    # Project-local .mcp.json
    try:
        local_mcp = Path.cwd() / ".mcp.json"
    except OSError:
        local_mcp = None
    if local_mcp is not None and local_mcp.exists():
        try:
            data = json.loads(local_mcp.read_text())
        except json.JSONDecodeError:
            ui.warning(f"{local_mcp} is not valid JSON — skipping local MCP import discovery")
            data = {}
        except OSError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        for name, cfg in iter_servers(data.get("mcpServers")):
            consider(name, cfg)

    return found


# ──────────────────────────────────────────────────────────────────────
# Config persistence
# ──────────────────────────────────────────────────────────────────────

def load_config() -> Optional[dict]:
    """Read the saved config, or None if the user has never run the wizard."""
    if not CONFIG_PATH.exists():
        return None
    try:
        return json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError:
        ui.warning(f"MCP config at {CONFIG_PATH} is not valid JSON — re-run `deep-report --setup`")
        return None
    except OSError:
        return None


def save_config(enabled: list[str], imported: Optional[dict[str, dict]] = None) -> Path:
    """Atomically save the user's MCP selection. Uses temp + os.replace so an
    interrupted write can't corrupt the previously-saved selection.

    ``imported`` holds the full MCP config for any servers the user inherited
    from their Claude Code setup (keyed by server name). Those configs may
    embed plaintext API keys, so the file is chmod 0600 after the move.
    """
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "version": 2,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "enabled": sorted(enabled),
    }
    if imported:
        payload["imported"] = imported
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(tmp, CONFIG_PATH)
    try:
        CONFIG_PATH.chmod(0o600)  # imported.env may contain plaintext API keys
    except OSError:
        pass  # Windows / odd FS — non-fatal
    return CONFIG_PATH


def enabled_keys() -> Optional[set[str]]:
    """Return the user's enabled set, or None if no config has been saved.

    Treats a config without an ``enabled`` key as unconfigured (returns None +
    warns) rather than silently disabling every server.
    """
    cfg = load_config()
    if cfg is None:
        return None
    if "enabled" not in cfg:
        ui.warning(f"MCP config at {CONFIG_PATH} has no 'enabled' key — re-run `deep-report --setup`")
        return None
    return set(cfg["enabled"])


def imported_servers() -> dict[str, dict]:
    """Return the persisted CC-imported server configs (full MCP blocks).

    Empty dict when there are no imports or no config. ``enabled_keys()`` still
    governs which of these the runtime actually registers.

    Defensive guards:
    - If the on-disk ``imported`` block isn't a dict (e.g. hand-edited to a list
      or string), return an empty dict rather than crashing downstream iteration.
    - Filter out any names now present in the catalog — a future catalog release
      adding a server with the same name as a saved import would otherwise
      register the user's stale import config alongside the canonical one.
    """
    cfg = load_config()
    if cfg is None:
        return {}
    raw = cfg.get("imported")
    if not isinstance(raw, dict):
        return {}
    return {name: blk for name, blk in raw.items()
            if name not in BY_KEY and isinstance(blk, dict)}


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
    # Nothing is pre-checked. The caller flips `checked` on re-runs to restore
    # the user's previous selection — first-run users opt in explicitly.
    return Choice(
        title=title,
        value=spec.key,
        checked=False,
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

    if not sys.stdin.isatty():
        ui.error("--setup needs an interactive terminal (arrow keys + space toggles).")
        ui.info(f"To pre-seed without a TTY, write {CONFIG_PATH} yourself "
                "(see docs for schema).")
        return 2

    ui.header("deep-report MCP setup")
    ui.info(
        "Pick the MCP servers you want deep-report to use.\n"
        "Nothing is pre-selected — tick (Space) what you want, then Enter."
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

    # CC-imported servers — anything the user has configured in Claude Code that
    # we don't already cover with the catalog (paper-search is explicitly blocked).
    cc_imports = discover_cc_servers()
    if cc_imports:
        ui.info(f"\nDiscovered {len(cc_imports)} MCP(s) from your Claude Code config — "
                "you can opt these into deep-report below.")

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

    # Append the CC-imports section if we found any.
    if cc_imports:
        choices.append(questionary.Separator("\n── Imported from Claude Code ──"))
        for name, cfg in sorted(cc_imports.items()):
            label = f"{name}  [IMPORTED]  — {_summarize_cc_entry(cfg)}"
            # Nothing pre-checked — user opts in (matches catalog-row behaviour).
            check = name in existing if existing is not None else False
            choices.append(Choice(title=label, value=name, checked=check))

    selection: Optional[list[str]] = questionary.checkbox(
        "Servers to enable (Space to toggle, Enter to confirm):",
        choices=choices,
    ).ask()

    if selection is None:
        ui.info("Cancelled.")
        return 1

    enabled = set(selection)
    # Persist the full config for any CC import the user kept ticked, so the
    # runtime doesn't need to re-read ~/.claude.json on every spawn.
    imported_to_save = {name: cfg for name, cfg in cc_imports.items() if name in enabled}
    path = save_config(sorted(enabled), imported=imported_to_save or None)
    ui.success(f"Saved to {path}")

    _print_summary(enabled)
    if imported_to_save:
        ui.info(f"\nImported from Claude Code ({len(imported_to_save)}):")
        for name in sorted(imported_to_save):
            ui.info(f"  • {name}")
    return 0
