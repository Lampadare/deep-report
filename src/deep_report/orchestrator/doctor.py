"""Per-MCP health report for the ``--doctor`` command.

Reports, for each MCP (or the user-enabled subset):
- whether host prereqs (Node, uv, Docker) are present
- whether required env vars are set
- a concrete fix hint when something's missing

This module never spawns an agent or runs a real report. It's a pure
diagnostic — safe to call from CI or any non-interactive context.
"""
from __future__ import annotations

import json
from typing import Optional

from .mcp_catalog import (
    CATALOG,
    TIER_BADGE,
    TIER_FREE,
    TIER_FREE_TIER,
    TIER_PAID,
    MCPSpec,
)
from .setup_wizard import (
    _spec_env_status,
    _spec_runnable,
    detect_prereqs,
    enabled_keys as load_enabled_keys,
)
from .ui import RICH_AVAILABLE, theme, ui

try:
    from rich.box import ROUNDED
    from rich.table import Table
except ImportError:
    Table = None  # type: ignore
    ROUNDED = None  # type: ignore


# ──────────────────────────────────────────────────────────────────────
# Report builder
# ──────────────────────────────────────────────────────────────────────

def _missing_prereqs(spec: MCPSpec, prereqs: dict[str, bool]) -> list[str]:
    """Return host commands the spec needs that aren't on PATH."""
    missing: list[str] = []
    if spec.requires_node and not prereqs["node"]:
        missing.append("npx")
    if spec.requires_uv and not prereqs["uv"]:
        missing.append("uvx")
    if spec.requires_docker and not prereqs["docker"]:
        missing.append("docker")
    return missing


def doctor_report(enabled_keys: Optional[set[str]] = None) -> list[dict]:
    """Per-MCP status. ``enabled_keys=None`` means all catalog entries.

    Each dict has the shape::

        {
          key, display_name, tier, category,
          requires_node, requires_uv, requires_docker,
          prereq_ok, missing_prereqs,    # list of host commands not on PATH
          env_ok, missing_env,            # list of required env vars not set
          signup_url, cost_note,
          ok                              # prereq_ok AND env_ok
        }
    """
    prereqs = detect_prereqs()
    if enabled_keys is None:
        specs: list[MCPSpec] = list(CATALOG)
    else:
        # Preserve catalog order, but only the keys the caller asked for.
        specs = [s for s in CATALOG if s.key in enabled_keys]

    out: list[dict] = []
    for spec in specs:
        runnable, _reason = _spec_runnable(spec, prereqs)
        missing_pre = _missing_prereqs(spec, prereqs)
        miss_req, _miss_opt = _spec_env_status(spec)
        env_ok = not miss_req
        out.append({
            "key": spec.key,
            "display_name": spec.display_name,
            "tier": spec.tier,
            "category": spec.category,
            "requires_node": spec.requires_node,
            "requires_uv": spec.requires_uv,
            "requires_docker": spec.requires_docker,
            "prereq_ok": runnable,
            "missing_prereqs": missing_pre,
            "env_ok": env_ok,
            "missing_env": list(miss_req),
            "signup_url": spec.key_signup_url,
            "cost_note": spec.cost_note or None,
            "ok": runnable and env_ok,
        })
    return out


# ──────────────────────────────────────────────────────────────────────
# Fix hints
# ──────────────────────────────────────────────────────────────────────

_PREREQ_FIX = {
    "npx": "install Node.js (brew install node, or https://nodejs.org)",
    "uvx": "install uv (curl -LsSf https://astral.sh/uv/install.sh | sh)",
    "docker": "install Docker Desktop (https://www.docker.com/products/docker-desktop)",
}


def _fix_lines(entry: dict) -> list[str]:
    """Concrete remediation lines for an entry with ok=False."""
    lines: list[str] = []
    for cmd in entry["missing_prereqs"]:
        lines.append(_PREREQ_FIX.get(cmd, f"install {cmd}"))
    if entry["missing_env"]:
        vars_csv = ", ".join(entry["missing_env"])
        signup = entry.get("signup_url")
        if signup:
            lines.append(f"set {vars_csv} — sign up at {signup}")
        else:
            lines.append(f"set {vars_csv}")
    if entry["key"] == "crawl4ai" and not entry["missing_prereqs"]:
        # Docker is installed, but the image still needs pulling.
        lines.append("pull docker image: docker pull unclecode/crawl4ai")
    return lines


# ──────────────────────────────────────────────────────────────────────
# Output
# ──────────────────────────────────────────────────────────────────────

def _tier_color(tier: str) -> str:
    return {
        TIER_FREE: theme.success,
        TIER_FREE_TIER: theme.warning,
        TIER_PAID: theme.error,
    }.get(tier, "white")


def _print_machine(entries: list[dict]) -> None:
    """One JSON object per line on stdout — parseable from any driver."""
    for entry in entries:
        print(json.dumps(entry, sort_keys=True))


def _print_rich(entries: list[dict]) -> None:
    """Rich table + per-spec fix hints."""
    if not RICH_AVAILABLE or Table is None or ui.console is None:
        _print_plain(entries)
        return

    ui.header("deep-report --doctor")

    if not entries:
        ui.warning("No MCPs to report on — run `deep-report --setup` to enable some.")
        return

    table = Table(show_header=True, box=ROUNDED)
    table.add_column("MCP", style=theme.heading, no_wrap=True)
    table.add_column("Tier", width=22)
    table.add_column("Prereqs", width=24)
    table.add_column("Env", width=28)

    for entry in entries:
        tier_badge = TIER_BADGE.get(entry["tier"], entry["tier"].upper())
        tier_cell = f"[{_tier_color(entry['tier'])}]{tier_badge}[/]"

        if entry["prereq_ok"]:
            prereq_cell = f"[{theme.success}]✓[/]"
        else:
            missing = ", ".join(entry["missing_prereqs"]) or "?"
            prereq_cell = f"[{theme.error}]✗ {missing}[/]"

        if entry["env_ok"]:
            env_cell = f"[{theme.success}]✓[/]"
        else:
            missing = ", ".join(entry["missing_env"]) or "?"
            env_cell = f"[{theme.error}]✗ {missing}[/]"

        table.add_row(entry["display_name"], tier_cell, prereq_cell, env_cell)

    ui.console.print(table)

    needs_fix = [e for e in entries if not e["ok"]]
    ready = len(entries) - len(needs_fix)
    summary = (
        f"{ready} of {len(entries)} ready; "
        f"{len(needs_fix)} need attention"
    )
    if needs_fix:
        ui.warning(summary)
    else:
        ui.success(summary)

    for entry in needs_fix:
        ui.console.print()
        ui.console.print(
            f"[bold]{entry['display_name']}[/]  "
            f"[{theme.dim}]({entry['key']})[/]"
        )
        for line in _fix_lines(entry):
            ui.console.print(f"  [{theme.warning}]Fix:[/] {line}")


def _print_plain(entries: list[dict]) -> None:
    """Plain-text fallback when Rich isn't available."""
    print()
    print("=" * 60)
    print("deep-report --doctor")
    print("=" * 60)
    if not entries:
        print("No MCPs to report on — run `deep-report --setup` to enable some.")
        return
    for entry in entries:
        status = "OK" if entry["ok"] else "NEEDS ATTENTION"
        print(f"\n{entry['display_name']} [{entry['tier']}] — {status}")
        if entry["missing_prereqs"]:
            print(f"  prereqs missing: {', '.join(entry['missing_prereqs'])}")
        if entry["missing_env"]:
            print(f"  env missing:     {', '.join(entry['missing_env'])}")
        if not entry["ok"]:
            for line in _fix_lines(entry):
                print(f"  Fix: {line}")
    needs_fix = [e for e in entries if not e["ok"]]
    ready = len(entries) - len(needs_fix)
    print()
    print(f"{ready} of {len(entries)} ready; {len(needs_fix)} need attention")


def print_doctor(enabled_only: bool = True) -> int:
    """Print the health report.

    ``enabled_only=True`` (default) → only the MCPs the user has enabled via the
    setup wizard. If no config exists yet, we fall back to the full catalog so
    a fresh install still gets useful output.

    Machine mode → JSON Lines (one entry per line) on stdout.
    Interactive mode → Rich table + per-spec fix hints.

    Returns ``0`` if every reported MCP is OK, else ``1``.
    """
    if enabled_only:
        enabled = load_enabled_keys()
    else:
        enabled = None  # all catalog entries

    entries = doctor_report(enabled)

    if ui._machine_mode:
        _print_machine(entries)
    elif RICH_AVAILABLE:
        _print_rich(entries)
    else:
        _print_plain(entries)

    return 0 if all(e["ok"] for e in entries) else 1
