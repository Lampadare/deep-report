"""save_config / enabled_keys / imported_servers roundtrip + defensive guards."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deep_report.orchestrator import setup_wizard
from deep_report.orchestrator.mcp_catalog import BY_KEY


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "mcp_config.json"
    monkeypatch.setattr(setup_wizard, "CONFIG_PATH", path)
    return path


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_save_writes_0o600(config_path):
    setup_wizard.save_config(["brave-search"])
    assert config_path.exists()
    assert config_path.stat().st_mode & 0o777 == 0o600


def test_enabled_keys_returns_none_without_enabled_key(config_path):
    config_path.write_text(json.dumps({"version": 2, "saved_at": "2026-01-01T00:00:00+00:00"}))
    assert setup_wizard.enabled_keys() is None


def test_enabled_keys_returns_none_for_non_dict_root(config_path):
    config_path.write_text("42")
    assert setup_wizard.enabled_keys() is None


def test_imported_servers_returns_empty_on_non_dict(config_path):
    config_path.write_text("[]")
    assert setup_wizard.imported_servers() == {}


def test_imported_servers_filters_catalog_shadows(config_path):
    # Pick any real catalog name so the filter actually triggers.
    shadow_name = next(iter(BY_KEY))
    payload = {
        "version": 2,
        "enabled": [],
        "imported": {
            shadow_name: {"command": "npx", "args": ["-y", "stale"]},
            "custom-server": {"command": "node", "args": ["./srv.js"]},
        },
    }
    config_path.write_text(json.dumps(payload))
    result = setup_wizard.imported_servers()
    assert shadow_name not in result
    assert "custom-server" in result


def test_save_roundtrip(config_path):
    enabled = ["brave-search", "exa"]
    imported = {"my-custom": {"command": "node", "args": ["./x.js"], "env": {"X": "1"}}}
    setup_wizard.save_config(enabled, imported=imported)

    assert setup_wizard.enabled_keys() == set(enabled)
    assert setup_wizard.imported_servers() == imported
