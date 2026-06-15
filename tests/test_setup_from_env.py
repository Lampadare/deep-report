"""save_config_from_env auto-enables runnable specs whose required_env is set."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deep_report.orchestrator import setup_wizard


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "mcp_config.json"
    monkeypatch.setattr(setup_wizard, "CONFIG_PATH", path)
    return path


@pytest.fixture
def all_runtimes_present(monkeypatch):
    """Pretend node/npx/uv/uvx/docker are all installed."""
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")


@pytest.fixture
def clear_required_env(monkeypatch):
    """Clear every required_env var across the catalog so each test starts clean."""
    for spec in setup_wizard.CATALOG:
        for var in spec.required_env:
            monkeypatch.delenv(var, raising=False)


def test_save_from_env_enables_specs_with_keys(
    config_path, all_runtimes_present, clear_required_env, monkeypatch
):
    monkeypatch.setenv("BRAVE_API_KEY", "x")
    cfg = setup_wizard.save_config_from_env()
    assert "brave-search" in cfg["enabled"]


def test_save_from_env_skips_specs_without_keys(
    config_path, all_runtimes_present, clear_required_env
):
    # FIRECRAWL_API_KEY is unset → firecrawl must not be enabled.
    cfg = setup_wizard.save_config_from_env()
    assert "firecrawl" not in cfg["enabled"]


def test_save_from_env_skips_unrunnable(
    config_path, all_runtimes_present, clear_required_env, monkeypatch
):
    real_spec_runnable = setup_wizard._spec_runnable

    def fake_spec_runnable(spec, prereqs):
        if spec.key == "arxiv":
            return False, "stubbed unrunnable"
        return real_spec_runnable(spec, prereqs)

    monkeypatch.setattr(setup_wizard, "_spec_runnable", fake_spec_runnable)
    cfg = setup_wizard.save_config_from_env()
    assert "arxiv" not in cfg["enabled"]


def test_save_from_env_enables_free_specs_when_runnable(
    config_path, all_runtimes_present, clear_required_env
):
    cfg = setup_wizard.save_config_from_env()
    enabled = set(cfg["enabled"])
    for key in ("arxiv", "wikipedia", "pubmed", "openalex", "context7"):
        assert key in enabled, f"{key} should be auto-enabled when runnable + no required env"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_save_from_env_writes_0o600(
    config_path, all_runtimes_present, clear_required_env
):
    setup_wizard.save_config_from_env()
    assert config_path.exists()
    assert config_path.stat().st_mode & 0o777 == 0o600


def test_save_from_env_returns_saved_dict(
    config_path, all_runtimes_present, clear_required_env, monkeypatch
):
    monkeypatch.setenv("BRAVE_API_KEY", "x")
    returned = setup_wizard.save_config_from_env()
    persisted = setup_wizard.load_config()
    assert returned == persisted
