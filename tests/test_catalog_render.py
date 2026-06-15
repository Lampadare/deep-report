"""Tests for catalog_render — the README-facing MCP table renderer."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deep_report.orchestrator.catalog_render import (
    print_catalog,
    render_catalog_markdown,
)
from deep_report.orchestrator.mcp_catalog import CATALOG


HEADER = "| MCP | Tier | What it does |"
SEPARATOR = "|---|---|---|"


def test_render_starts_with_header():
    output = render_catalog_markdown()
    assert output.startswith(HEADER)


def test_render_has_separator_row():
    output = render_catalog_markdown()
    assert SEPARATOR in output


def test_render_has_all_catalog_entries():
    output = render_catalog_markdown()
    data_rows = [
        line
        for line in output.splitlines()
        if line.startswith("|") and line != HEADER and line != SEPARATOR
    ]
    assert len(data_rows) == len(CATALOG)


def test_render_includes_brave_and_exa():
    output = render_catalog_markdown()
    assert "Brave Search" in output
    assert "Exa" in output


def test_render_is_deterministic():
    assert render_catalog_markdown() == render_catalog_markdown()


def test_print_catalog_exit_zero(capsys):
    rc = print_catalog()
    assert rc == 0
    captured = capsys.readouterr()
    assert HEADER in captured.out
