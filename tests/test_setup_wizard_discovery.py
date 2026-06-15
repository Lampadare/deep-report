"""discover_cc_servers must be defensive against hand-edited / malformed
``~/.claude.json`` files and must filter known-dead servers (paper-search)
out of the discovered set.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deep_report.orchestrator import setup_wizard


def _patch_home_and_cwd(monkeypatch, tmp_path: Path) -> Path:
    """Redirect Path.home() and Path.cwd() into tmp_path so the discovery
    function sees a clean fake home without ~/.claude.json or .mcp.json."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    fake_cwd = tmp_path / "cwd"
    fake_cwd.mkdir()
    monkeypatch.setattr(setup_wizard.Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(setup_wizard.Path, "cwd", classmethod(lambda cls: fake_cwd))
    return fake_home


def test_discover_handles_missing_claude_json(monkeypatch, tmp_path):
    """No ~/.claude.json and no .mcp.json → empty dict, no crash."""
    _patch_home_and_cwd(monkeypatch, tmp_path)
    assert setup_wizard.discover_cc_servers() == {}


def test_discover_handles_non_dict_mcp_servers(monkeypatch, tmp_path):
    """``"mcpServers": "oops"`` must not crash — returns {}."""
    fake_home = _patch_home_and_cwd(monkeypatch, tmp_path)
    (fake_home / ".claude.json").write_text(json.dumps({"mcpServers": "oops"}))
    assert setup_wizard.discover_cc_servers() == {}


def test_discover_handles_non_dict_entry(monkeypatch, tmp_path):
    """An entry whose value isn't a dict gets dropped; valid siblings survive."""
    fake_home = _patch_home_and_cwd(monkeypatch, tmp_path)
    (fake_home / ".claude.json").write_text(json.dumps({
        "mcpServers": {
            "bogus": "oops",
            "good": {"command": "npx", "args": ["-y", "some-mcp"]},
        }
    }))
    result = setup_wizard.discover_cc_servers()
    assert "bogus" not in result
    assert "good" in result


def test_discover_returns_valid_entries(monkeypatch, tmp_path):
    """A well-formed claude.json with http + command MCPs → both returned."""
    fake_home = _patch_home_and_cwd(monkeypatch, tmp_path)
    (fake_home / ".claude.json").write_text(json.dumps({
        "mcpServers": {
            "my-http": {"type": "http", "url": "https://example.com/mcp"},
            "my-cmd": {"command": "npx", "args": ["-y", "@scope/my-mcp"]},
        }
    }))
    result = setup_wizard.discover_cc_servers()
    assert set(result.keys()) == {"my-http", "my-cmd"}
    assert result["my-http"]["type"] == "http"
    assert result["my-cmd"]["command"] == "npx"


def test_discover_filters_paper_search_blocklist(monkeypatch, tmp_path):
    """``paper-search`` is in _IMPORT_BLOCKLIST and must be filtered out."""
    assert "paper-search" in setup_wizard._IMPORT_BLOCKLIST
    fake_home = _patch_home_and_cwd(monkeypatch, tmp_path)
    (fake_home / ".claude.json").write_text(json.dumps({
        "mcpServers": {
            "paper-search": {"command": "uvx", "args": ["paper-search"]},
            "keeper": {"command": "npx", "args": ["-y", "ok-mcp"]},
        }
    }))
    result = setup_wizard.discover_cc_servers()
    assert "paper-search" not in result
    assert "keeper" in result


def test_discover_handles_json_decode_error(monkeypatch, tmp_path, capsys):
    """Malformed JSON in ~/.claude.json → returns {} and emits a warning."""
    fake_home = _patch_home_and_cwd(monkeypatch, tmp_path)
    (fake_home / ".claude.json").write_text("{not valid json")

    warnings: list[str] = []
    monkeypatch.setattr(setup_wizard.ui, "warning", lambda msg: warnings.append(msg))

    assert setup_wizard.discover_cc_servers() == {}
    assert any("claude.json" in w for w in warnings)
