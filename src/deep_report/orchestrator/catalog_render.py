"""Render :data:`mcp_catalog.CATALOG` as the README-facing MCP table.

The README's "What MCPs are available" section uses a 4-column markdown table
(MCP, Needs, Tier, What it does). This module is the single source of truth —
running ``deep-report --print-catalog`` yields the exact text that should
live in the README between the ``<!-- CATALOG-TABLE:START -->`` markers.
"""
from __future__ import annotations

import re

from .mcp_catalog import (
    CATALOG,
    TIER_FREE,
    TIER_FREE_TIER,
    TIER_PAID,
    MCPSpec,
)


_PAREN_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")


def _readme_name(spec: MCPSpec) -> str:
    """README-facing display name.

    Strips a trailing parenthetical from ``display_name`` (used in the wizard
    to disambiguate vendor / runtime, e.g. "Playwright (Microsoft)") since the
    README keeps the name lean.
    """
    return _PAREN_SUFFIX.sub("", spec.display_name).strip()


def _needs_phrase(spec: MCPSpec) -> str:
    """Human-readable runtime requirement for the README's "Needs" column.

    Concatenates flagged runtimes; HTTP-only specs show an em dash.
    """
    parts: list[str] = []
    if spec.requires_node:
        parts.append("Node")
    if spec.requires_uv:
        parts.append("Python+uv")
    if spec.requires_docker:
        parts.append("Docker")
    return " + ".join(parts) if parts else "—"


def _tier_phrase(spec: MCPSpec) -> str:
    """Human-readable tier phrase for the README's "Tier" column.

    Uses :attr:`MCPSpec.tier_label` when set, otherwise falls back to a
    sensible default derived from :attr:`MCPSpec.tier`.
    """
    if spec.tier_label:
        return spec.tier_label
    if spec.tier == TIER_FREE:
        return "Free"
    if spec.tier == TIER_FREE_TIER:
        return "Free tier"
    if spec.tier == TIER_PAID:
        return "Paid"
    return spec.tier


def render_catalog_markdown() -> str:
    """Render the CATALOG as the README's MCP table.

    Returns a 4-column markdown table (``| MCP | Needs | Tier | What it does |``)
    in :data:`CATALOG` order — stable and deterministic. No trailing newline.
    """
    lines = [
        "| MCP | Needs | Tier | What it does |",
        "|---|---|---|---|",
    ]
    for spec in CATALOG:
        name = _readme_name(spec)
        needs = _needs_phrase(spec)
        tier = _tier_phrase(spec)
        desc = spec.short_desc or spec.summary
        lines.append(f"| **{name}** | {needs} | {tier} | {desc} |")
    return "\n".join(lines)


def print_catalog() -> int:
    """Print :func:`render_catalog_markdown` to stdout. Returns 0."""
    print(render_catalog_markdown())
    return 0
