"""Tests for scripts/check_catalog_drift.py — README drift detector."""

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_catalog_drift.py"
README_PATH = REPO_ROOT / "README.md"


def _load_script_module():
    """Load the drift-check script as a fresh module for each test."""
    spec = importlib.util.spec_from_file_location(
        "_check_catalog_drift", SCRIPT_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_readme(table_block: str) -> str:
    return (
        "# Demo\n"
        "Some intro.\n\n"
        "<!-- CATALOG-TABLE:START — generated; do not hand-edit -->\n"
        f"{table_block}\n"
        "<!-- CATALOG-TABLE:END -->\n"
        "\nTrailing prose.\n"
    )


@pytest.fixture
def drift_script(monkeypatch):
    """Return the drift-check module with sys.argv reset to no flags."""
    module = _load_script_module()
    monkeypatch.setattr(sys, "argv", [str(SCRIPT_PATH)])
    return module


def test_drift_in_sync(drift_script, capsys):
    """The committed README must be in sync with the generated catalog."""
    rc = drift_script.main()
    assert rc == 0


def test_drift_detects_modification(drift_script, monkeypatch, tmp_path, capsys):
    fake_readme = tmp_path / "README.md"
    fake_readme.write_text(_make_readme("| this | is | stale |"), encoding="utf-8")
    monkeypatch.setattr(drift_script, "README_PATH", fake_readme)

    rc = drift_script.main()
    assert rc == 1
    err = capsys.readouterr().err
    assert "out of date" in err
    assert "--update" in err


def test_drift_update_rewrites(drift_script, monkeypatch, tmp_path, capsys):
    fake_readme = tmp_path / "README.md"
    fake_readme.write_text(_make_readme("| this | is | stale |"), encoding="utf-8")
    monkeypatch.setattr(drift_script, "README_PATH", fake_readme)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT_PATH), "--update"])

    rc = drift_script.main()
    assert rc == 0

    rewritten = fake_readme.read_text(encoding="utf-8")
    assert "| MCP | Tier | What it does |" in rewritten
    assert "this | is | stale" not in rewritten
    # markers preserved
    assert "<!-- CATALOG-TABLE:START" in rewritten
    assert "<!-- CATALOG-TABLE:END -->" in rewritten


def test_drift_handles_missing_markers(drift_script, monkeypatch, tmp_path, capsys):
    fake_readme = tmp_path / "README.md"
    fake_readme.write_text("# Demo\nNo markers here.\n", encoding="utf-8")
    monkeypatch.setattr(drift_script, "README_PATH", fake_readme)

    with pytest.raises(SystemExit) as exc:
        drift_script.main()
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "markers" in err.lower()
