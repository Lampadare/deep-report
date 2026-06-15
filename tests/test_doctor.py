"""doctor_report + print_doctor: status assembly and exit-code contract."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deep_report.orchestrator import doctor, setup_wizard
from deep_report.orchestrator.mcp_catalog import CATALOG


def _all_required_env() -> set[str]:
    out: set[str] = set()
    for spec in CATALOG:
        out.update(spec.required_env)
    return out


def _set_all_env(monkeypatch) -> None:
    """Populate every required env var so _spec_env_status returns clean."""
    for var in _all_required_env():
        monkeypatch.setenv(var, "x")


def _all_present_which(_cmd: str) -> str:
    return "/usr/bin/stub"


def _none_present_which(_cmd: str) -> None:
    return None


def test_doctor_report_returns_all_specs_by_default():
    assert len(doctor.doctor_report()) == len(CATALOG)


def test_doctor_report_filters_to_enabled_only():
    result = doctor.doctor_report({"brave-search"})
    assert len(result) == 1
    assert result[0]["key"] == "brave-search"


def test_doctor_report_marks_missing_prereqs(monkeypatch):
    monkeypatch.setattr(setup_wizard.shutil, "which", _none_present_which)
    report = doctor.doctor_report()
    npx_consumers = [e for e in report if "npx" in e["missing_prereqs"]]
    assert npx_consumers, "expected at least one spec to need npx"
    assert all(not e["prereq_ok"] for e in npx_consumers)


def test_doctor_report_marks_missing_env(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    report = doctor.doctor_report({"brave-search"})
    entry = report[0]
    assert "BRAVE_API_KEY" in entry["missing_env"]
    assert entry["env_ok"] is False


def test_doctor_report_ok_when_all_satisfied(monkeypatch):
    monkeypatch.setattr(setup_wizard.shutil, "which", _all_present_which)
    _set_all_env(monkeypatch)
    report = doctor.doctor_report()
    assert all(e["ok"] for e in report)


def test_print_doctor_returns_zero_when_all_ok(monkeypatch):
    monkeypatch.setattr(
        doctor,
        "doctor_report",
        lambda enabled=None: [{"ok": True, "missing_prereqs": [], "missing_env": []}],
    )
    monkeypatch.setattr(doctor, "load_enabled_keys", lambda: set())
    monkeypatch.setattr(doctor.ui, "_machine_mode", True, raising=False)
    assert doctor.print_doctor() == 0


def test_print_doctor_returns_one_when_any_broken(monkeypatch):
    entries = [
        {"ok": True, "missing_prereqs": [], "missing_env": []},
        {
            "ok": False,
            "missing_prereqs": ["npx"],
            "missing_env": [],
            "key": "x",
            "display_name": "X",
            "tier": "free",
        },
    ]
    monkeypatch.setattr(doctor, "doctor_report", lambda enabled=None: entries)
    monkeypatch.setattr(doctor, "load_enabled_keys", lambda: set())
    monkeypatch.setattr(doctor.ui, "_machine_mode", True, raising=False)
    assert doctor.print_doctor() == 1
