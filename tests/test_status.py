"""status_report / print_status: empty defaults, saved-config snapshot,
imported entries, unset-key flagging, and keys.env path surfacing.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deep_report.orchestrator import setup_wizard


def _patch_paths(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    config_path = tmp_path / "mcp_config.json"
    keys_env_path = tmp_path / "keys.env"
    monkeypatch.setattr(setup_wizard, "CONFIG_PATH", config_path)
    monkeypatch.setattr(setup_wizard, "KEYS_ENV_PATH", keys_env_path)
    return config_path, keys_env_path


def test_status_no_config_returns_empty_safe_default(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    report = setup_wizard.status_report()
    assert report["enabled"] == []
    assert report["imported"] == []
    assert report["unset_keys_for_enabled"] == []
    assert report["saved_at"] is None
    assert report["config_version"] is None


def test_status_with_saved_config(monkeypatch, tmp_path):
    config_path, _ = _patch_paths(monkeypatch, tmp_path)
    # Stub env so BRAVE_API_KEY counts as present (arxiv has no required env).
    monkeypatch.setenv("BRAVE_API_KEY", "test-brave-key")
    config_path.write_text(json.dumps({
        "version": 2,
        "saved_at": "2026-01-01T00:00:00+00:00",
        "enabled": ["brave-search", "arxiv"],
    }))

    report = setup_wizard.status_report()
    keys = {e["key"] for e in report["enabled"]}
    assert keys == {"brave-search", "arxiv"}
    for entry in report["enabled"]:
        assert "display_name" in entry
        assert "prereq_ok" in entry
        assert "env_ok" in entry


def test_status_includes_imported(monkeypatch, tmp_path):
    config_path, _ = _patch_paths(monkeypatch, tmp_path)
    config_path.write_text(json.dumps({
        "version": 2,
        "saved_at": "2026-01-01T00:00:00+00:00",
        "enabled": [],
        "imported": {
            "custom-mcp": {"command": "node", "args": ["./srv.js"]},
        },
    }))

    report = setup_wizard.status_report()
    names = [e["name"] for e in report["imported"]]
    assert "custom-mcp" in names


def test_status_flags_unset_keys(monkeypatch, tmp_path):
    config_path, _ = _patch_paths(monkeypatch, tmp_path)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    config_path.write_text(json.dumps({
        "version": 2,
        "saved_at": "2026-01-01T00:00:00+00:00",
        "enabled": ["brave-search"],
    }))

    report = setup_wizard.status_report()
    assert "BRAVE_API_KEY" in report["unset_keys_for_enabled"]
    entry = next(e for e in report["enabled"] if e["key"] == "brave-search")
    assert entry["env_ok"] is False
    assert "BRAVE_API_KEY" in entry["missing_env"]


def test_status_includes_keys_env_path_when_exists(monkeypatch, tmp_path):
    _, keys_env_path = _patch_paths(monkeypatch, tmp_path)
    keys_env_path.write_text("BRAVE_API_KEY=abc\n")

    report = setup_wizard.status_report()
    assert report["keys_env_path"] == str(keys_env_path)


def test_status_omits_keys_env_path_when_absent(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    report = setup_wizard.status_report()
    assert report.get("keys_env_path") is None


def test_print_status_returns_zero(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    assert setup_wizard.print_status() == 0
