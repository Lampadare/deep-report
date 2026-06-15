"""Render :data:`mcp_catalog.CATALOG` as the README-facing MCP table.

The README's "What MCPs are available" section uses a 3-column markdown table.
This module is the single source of truth for that table — running
``deep-report --print-catalog`` (or invoking :func:`render_catalog_markdown`)
yields the exact text that should live in the README. Phase 4 will replace
the hand-maintained README block with the output of this renderer.
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

    Returns a 3-column markdown table (``| MCP | Tier | What it does |``)
    in :data:`CATALOG` order — stable and deterministic. No trailing newline.
    """
    lines = [
        "| MCP | Tier | What it does |",
        "|---|---|---|",
    ]
    for spec in CATALOG:
        name = _readme_name(spec)
        tier = _tier_phrase(spec)
        desc = spec.short_desc or spec.summary
        lines.append(f"| **{name}** | {tier} | {desc} |")
    return "\n".join(lines)


def print_catalog() -> int:
    """Print :func:`render_catalog_markdown` to stdout. Returns 0."""
    print(render_catalog_markdown())
    return 0
