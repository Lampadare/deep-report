"""Catalog of MCP servers deep-report knows how to integrate.

This is the single source of truth for the setup wizard. Run-time discovery
(``generate_mcp_config`` in ``utils/agents.py``) consults this to decide which
servers to register based on what the user has enabled.

Keep entries factual and concise — the wizard renders them directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# Tier classification — drives the labels the wizard shows.
TIER_FREE = "free"                # truly free, no signup
TIER_FREE_TIER = "free_tier"      # free tier exists, needs API key
TIER_PAID = "paid"                # no useful free tier, paid only

# Category — used to group entries in the wizard checkbox UI.
CAT_WEB = "web"
CAT_ACADEMIC = "academic"
CAT_DOCS = "docs"
CAT_SCRAPE = "scrape"
CAT_VERTICAL = "vertical"


@dataclass(frozen=True)
class MCPSpec:
    """Metadata for one MCP server."""
    key: str                              # mcp.json server key (e.g. "brave-search")
    display_name: str
    category: str
    tier: str
    summary: str                          # one-line description shown in wizard
    transport: str                        # "npx" | "uvx" | "http" | "docker" | "console_script"

    # Required environment variables — server is skipped if any are missing.
    required_env: tuple[str, ...] = ()
    # Optional env vars (rate-limit boosts, etc.)
    optional_env: tuple[str, ...] = ()

    # Where the user signs up for a key
    key_signup_url: Optional[str] = None
    # Short note on pricing/cost
    cost_note: str = ""

    # Prereqs the wizard checks for
    requires_node: bool = False
    requires_uv: bool = False
    requires_docker: bool = False


CATALOG: tuple[MCPSpec, ...] = (
    # ─── Web search ────────────────────────────────────────────────────────
    MCPSpec(
        key="brave-search",
        display_name="Brave Search",
        category=CAT_WEB,
        tier=TIER_FREE_TIER,
        summary="Filtered web/news search with date and locale controls.",
        transport="npx",
        required_env=("BRAVE_API_KEY",),
        key_signup_url="https://brave.com/search/api/",
        cost_note="2,000 free queries/mo; ~$3/1k after.",
        requires_node=True,
    ),
    MCPSpec(
        key="exa",
        display_name="Exa",
        category=CAT_WEB,
        tier=TIER_FREE_TIER,
        summary="Semantic search with full-page text — best for deep research.",
        transport="http",
        required_env=("EXA_API_KEY",),
        key_signup_url="https://dashboard.exa.ai",
        cost_note="$10 free credits on signup; pay-as-you-go.",
    ),
    MCPSpec(
        key="firecrawl",
        display_name="Firecrawl",
        category=CAT_WEB,
        tier=TIER_FREE_TIER,
        summary="Search + scrape + crawl with anti-bot handling.",
        transport="npx",
        required_env=("FIRECRAWL_API_KEY",),
        key_signup_url="https://www.firecrawl.dev",
        cost_note="500 free credits/mo; ~$0.02/page after.",
        requires_node=True,
    ),
    MCPSpec(
        key="tavily",
        display_name="Tavily",
        category=CAT_WEB,
        tier=TIER_FREE_TIER,
        summary="Agent-optimized search + extract via remote endpoint.",
        transport="http",
        required_env=("TAVILY_API_KEY",),
        key_signup_url="https://app.tavily.com",
        cost_note="1,000 free credits/mo; pay-as-you-go.",
    ),
    # ─── Academic ──────────────────────────────────────────────────────────
    MCPSpec(
        key="pubmed",
        display_name="PubMed + Europe PMC",
        category=CAT_ACADEMIC,
        tier=TIER_FREE,
        summary="PubMed, Europe PMC, bioRxiv, medRxiv — full-text via EPMC.",
        transport="npx",
        optional_env=("NCBI_API_KEY",),
        key_signup_url="https://www.ncbi.nlm.nih.gov/account/",
        cost_note="Free. NCBI key raises rate limit (3→10 req/s).",
        requires_node=True,
    ),
    MCPSpec(
        key="openalex",
        display_name="OpenAlex",
        category=CAT_ACADEMIC,
        tier=TIER_FREE,
        summary="270M scholarly works + citation graph, all disciplines.",
        transport="npx",
        # No required env — runtime falls back to an anonymous polite-pool sentinel.
        optional_env=("OPENALEX_API_KEY", "OPENALEX_EMAIL"),
        key_signup_url="https://docs.openalex.org/how-to-use-the-api/rate-limits-and-authentication#the-polite-pool",
        cost_note="Free. Setting OPENALEX_EMAIL with your own email upgrades to polite pool (faster).",
        requires_node=True,
    ),
    MCPSpec(
        key="arxiv",
        display_name="arXiv",
        category=CAT_ACADEMIC,
        tier=TIER_FREE,
        summary="arXiv search, download, full-text read with local cache.",
        transport="console_script",
        cost_note="Free.",
        requires_uv=True,  # falls back to uvx if console script missing
    ),
    MCPSpec(
        key="wikipedia",
        display_name="Wikipedia",
        category=CAT_ACADEMIC,
        tier=TIER_FREE,
        summary="Universal grounding — summaries, sections, related topics.",
        transport="console_script",
        cost_note="Free.",
        requires_uv=True,  # falls back to uvx if console script missing
    ),
    # ─── Docs ──────────────────────────────────────────────────────────────
    MCPSpec(
        key="context7",
        display_name="Context7",
        category=CAT_DOCS,
        tier=TIER_FREE_TIER,
        summary="Version-pinned library/API docs — essential for tech topics.",
        transport="npx",
        optional_env=("CONTEXT7_API_KEY",),
        key_signup_url="https://context7.com",
        cost_note="Free without key; key raises rate limits.",
        requires_node=True,
    ),
    # ─── Scrape fallback ───────────────────────────────────────────────────
    MCPSpec(
        key="playwright",
        display_name="Playwright (Microsoft)",
        category=CAT_SCRAPE,
        tier=TIER_FREE,
        summary="JS-heavy and anti-bot scrape fallback (accessibility tree).",
        transport="npx",
        cost_note="Free; downloads Chromium on first run.",
        requires_node=True,
    ),
    MCPSpec(
        key="crawl4ai",
        display_name="crawl4ai (Docker)",
        category=CAT_SCRAPE,
        tier=TIER_FREE,
        summary="LLM-friendly crawler — alternative scrape fallback.",
        transport="docker",
        cost_note="Free; requires Docker + pulled image.",
        requires_docker=True,
    ),
    # (Vertical-domain MCPs like DigiKey are intentionally not in the catalog
    # because research agents don't have their tool names in AGENT_TOOLS.
    # Users who want them can configure via Claude Code and import through
    # the wizard's CC-import path, which uses a wildcard allow entry.)
)


# Convenience lookups
BY_KEY: dict[str, MCPSpec] = {s.key: s for s in CATALOG}


def in_category(category: str) -> list[MCPSpec]:
    return [s for s in CATALOG if s.category == category]


# Pretty labels for the wizard
CATEGORY_LABEL = {
    CAT_WEB: "Web search",
    CAT_ACADEMIC: "Academic & scholarly",
    CAT_DOCS: "Library / API docs",
    CAT_SCRAPE: "Scraping fallbacks",
    CAT_VERTICAL: "Vertical / niche",
}

TIER_BADGE = {
    TIER_FREE: "FREE",
    TIER_FREE_TIER: "FREE TIER (needs key)",
    TIER_PAID: "PAID",
}
