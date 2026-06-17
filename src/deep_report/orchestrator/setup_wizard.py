"""Interactive setup wizard for deep-report's MCP integrations.

Usage:
    deep-report --setup

The wizard:
1. Reports which prereqs (Node, uv, Docker) are present locally.
2. Shows a categorized checkbox list of every MCP in :mod:`mcp_catalog`.
3. Saves the user's enabled set to ``~/.deep-report/mcp_config.json``.
4. Highlights which selected servers are missing required environment
   variables and shows where to get the keys.

Run-time discovery in ``utils/agents.py`` consults this file and only
registers servers in the enabled set (intersected with what's actually
runnable on this host).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

try:
    import questionary
    from questionary import Choice
except ImportError:
    questionary = None  # type: ignore
    Choice = None       # type: ignore

from .mcp_catalog import (
    BY_KEY,
    CATALOG,
    CATEGORY_LABEL,
    TIER_BADGE,
    TIER_FREE,
    TIER_FREE_TIER,
    TIER_PAID,
    MCPSpec,
)
from .ui import ui

CONFIG_PATH = Path.home() / ".deep-report" / "mcp_config.json"
KEYS_ENV_PATH = Path.home() / ".deep-report" / "keys.env"

# Module-level flag so the Windows chmod warning only fires once per process.
_WINDOWS_CHMOD_WARNED = False


def _warn_windows_chmod_once(path: Path) -> None:
    """Warn (once per session) that 0o600 is not enforced on Windows."""
    global _WINDOWS_CHMOD_WARNED
    if sys.platform != 'win32' or _WINDOWS_CHMOD_WARNED:
        return
    _WINDOWS_CHMOD_WARNED = True
    ui.warning(
        f"On Windows, the 0o600 file mode is not enforced. "
        f"{path} is readable by any process running as your user. "
        f"Treat it accordingly."
    )


# Names we never import even if a user has them in CC — these are either already
# in the catalog (catalog wins) or actively retired by deep-report.
_IMPORT_BLOCKLIST = {"paper-search"}  # replaced by cyanheads/pubmed-mcp-server


# ──────────────────────────────────────────────────────────────────────
# Prereq detection
# ──────────────────────────────────────────────────────────────────────

def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def detect_prereqs() -> dict[str, bool]:
    return {
        "node": _has("node") and _has("npx"),
        "uv": _has("uv") or _has("uvx"),
        "docker": _has("docker"),
    }


def _spec_runnable(spec: MCPSpec, prereqs: dict[str, bool]) -> tuple[bool, str]:
    """Can this spec run on the current host? Returns (ok, reason_if_not)."""
    if spec.requires_node and not prereqs["node"]:
        return False, "needs Node + npx"
    if spec.requires_uv and not prereqs["uv"]:
        return False, "needs uv / uvx"
    if spec.requires_docker and not prereqs["docker"]:
        return False, "needs Docker"
    return True, ""


def _spec_env_status(spec: MCPSpec) -> tuple[list[str], list[str]]:
    """Return (missing_required, missing_optional) env vars."""
    miss_req = [v for v in spec.required_env if not os.environ.get(v)]
    miss_opt = [v for v in spec.optional_env if not os.environ.get(v)]
    return miss_req, miss_opt


def _summarize_cc_entry(cfg: dict) -> str:
    """Short human label for a CC-imported server config."""
    if cfg.get("type") == "http":
        url = cfg.get("url", "")
        # urlsplit strips userinfo (user:password@) from the displayed host so
        # credentials embedded in a user's MCP URL never appear in the summary.
        parts = urlsplit(url)
        host = parts.hostname or "?"
        if parts.port:
            host = f"{host}:{parts.port}"
        return f"http → {host}"
    cmd = cfg.get("command", "?")
    args = cfg.get("args", [])
    # Show first non-flag arg if present (e.g. the npm package name)
    pkg = next((a for a in args if not a.startswith("-")), "")
    return f"{cmd} {pkg}".strip()


def discover_cc_servers() -> dict[str, dict]:
    """Return MCP servers configured in Claude Code that aren't in our catalog.

    Reads:
    - ``~/.claude.json`` (top-level ``mcpServers``)
    - ``~/.claude.json`` per-project block matching cwd (or any ancestor)
    - ``.mcp.json`` in cwd

    Excludes any name that matches a catalog key (catalog wins) or appears
    on the import blocklist.
    """
    found: dict[str, dict] = {}

    def consider(name: str, cfg: dict) -> None:
        if name in BY_KEY or name in _IMPORT_BLOCKLIST or name in found:
            return
        if not isinstance(cfg, dict):
            return
        found[name] = cfg

    def iter_servers(maybe_dict) -> list[tuple[str, dict]]:
        """Defensive iteration over a dict-shaped value. Returns [] for any
        non-dict (handles hand-edits like ``"mcpServers": "oops"`` or null)."""
        if not isinstance(maybe_dict, dict):
            return []
        return list(maybe_dict.items())

    # User-scope ~/.claude.json
    claude_json = Path.home() / ".claude.json"
    if claude_json.exists():
        try:
            data = json.loads(claude_json.read_text(encoding='utf-8', errors='replace'))
        except json.JSONDecodeError:
            ui.warning(f"~/.claude.json is not valid JSON — skipping CC import discovery")
            data = {}
        except (OSError, UnicodeDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}

        for name, cfg in iter_servers(data.get("mcpServers")):
            consider(name, cfg)
        # Project scope — any project whose path is the cwd or an ancestor
        try:
            cwd = Path.cwd().resolve()
        except OSError:
            cwd = None
        if cwd is not None:
            projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
            for raw_path, proj in projects.items():
                try:
                    proj_path = Path(raw_path).resolve()
                except (OSError, ValueError, TypeError):
                    continue
                if proj_path == cwd or cwd.is_relative_to(proj_path):
                    for name, cfg in iter_servers(
                        proj.get("mcpServers") if isinstance(proj, dict) else None
                    ):
                        consider(name, cfg)

    # Project-local .mcp.json
    try:
        local_mcp = Path.cwd() / ".mcp.json"
    except OSError:
        local_mcp = None
    if local_mcp is not None and local_mcp.exists():
        try:
            data = json.loads(local_mcp.read_text(encoding='utf-8', errors='replace'))
        except json.JSONDecodeError:
            ui.warning(f"{local_mcp} is not valid JSON — skipping local MCP import discovery")
            data = {}
        except (OSError, UnicodeDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        for name, cfg in iter_servers(data.get("mcpServers")):
            consider(name, cfg)

    return found


# ──────────────────────────────────────────────────────────────────────
# Config persistence
# ──────────────────────────────────────────────────────────────────────

def load_config() -> Optional[dict]:
    """Read the saved config, or None if the user has never run the wizard."""
    if not CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding='utf-8', errors='replace'))
    except json.JSONDecodeError:
        ui.warning(f"MCP config at {CONFIG_PATH} is not valid JSON — re-run `deep-report --setup`")
        return None
    except (OSError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        ui.warning(f"MCP config at {CONFIG_PATH} is not a JSON object — re-run `deep-report --setup`")
        return None
    return data


def save_config(enabled: list[str], imported: Optional[dict[str, dict]] = None) -> Path:
    """Atomically save the user's MCP selection. Uses temp + os.replace so an
    interrupted write can't corrupt the previously-saved selection.

    ``imported`` holds the full MCP config for any servers the user inherited
    from their Claude Code setup (keyed by server name). Those configs may
    embed plaintext API keys, so the file is chmod 0600 after the move.
    """
    # If ~/.deep-report somehow exists as a regular file (or symlink to one),
    # mkdir would raise NotADirectoryError with no actionable context. Detect
    # that up front and tell the user exactly what to do.
    parent = CONFIG_PATH.parent
    if parent.exists() and not parent.is_dir():
        ui.error(
            f"Cannot save MCP config: {parent} exists but is not a directory. "
            f"Move or remove it (e.g. `mv {parent} {parent}.bak`) and re-run `deep-report --setup`."
        )
        return CONFIG_PATH
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        ui.error(f"Cannot create {parent}: {exc}")
        return CONFIG_PATH
    payload: dict = {
        "version": 2,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "enabled": sorted(enabled),
    }
    if imported:
        payload["imported"] = imported
    # PID + monotonic_ns in the tmp suffix so two concurrent `--setup` runs
    # cannot collide on the same temp file (matches approval._atomic_write_json).
    # Open the tmp file with mode 0o600 atomically — imported.env may carry
    # plaintext API keys, so we must never have a window where the file is
    # world-readable at the default umask.
    tmp = CONFIG_PATH.with_suffix(f".tmp.{os.getpid()}.{time.monotonic_ns()}")
    body = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(body)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        os.replace(tmp, CONFIG_PATH)
    except OSError:
        # Disk full, permission denied, etc. — don't leave the tmp file behind.
        Path(tmp).unlink(missing_ok=True)
        raise
    try:
        CONFIG_PATH.chmod(0o600)  # belt-and-suspenders for non-POSIX FS
    except OSError:
        pass
    _warn_windows_chmod_once(CONFIG_PATH)
    return CONFIG_PATH


def enabled_keys() -> Optional[set[str]]:
    """Return the user's enabled set, or None if no config has been saved.

    Treats a config without an ``enabled`` key as unconfigured (returns None +
    warns) rather than silently disabling every server.
    """
    cfg = load_config()
    if cfg is None:
        return None
    if not isinstance(cfg, dict):
        return None
    if "enabled" not in cfg:
        ui.warning(f"MCP config at {CONFIG_PATH} has no 'enabled' key — re-run `deep-report --setup`")
        return None
    enabled = cfg["enabled"]
    if not isinstance(enabled, (list, set, tuple)):
        return None
    return {k for k in enabled if isinstance(k, str)}


def imported_servers() -> dict[str, dict]:
    """Return the persisted CC-imported server configs (full MCP blocks).

    Empty dict when there are no imports or no config. ``enabled_keys()`` still
    governs which of these the runtime actually registers.

    Defensive guards:
    - If the on-disk ``imported`` block isn't a dict (e.g. hand-edited to a list
      or string), return an empty dict rather than crashing downstream iteration.
    - Filter out any names now present in the catalog — a future catalog release
      adding a server with the same name as a saved import would otherwise
      register the user's stale import config alongside the canonical one.
    """
    cfg = load_config()
    if cfg is None:
        return {}
    if not isinstance(cfg, dict):
        return {}
    raw = cfg.get("imported")
    if not isinstance(raw, dict):
        return {}
    return {name: blk for name, blk in raw.items()
            if name not in BY_KEY and isinstance(blk, dict)}


# ──────────────────────────────────────────────────────────────────────
# API key persistence (keys.env)
# ──────────────────────────────────────────────────────────────────────

def _parse_keys_env_text(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines from a keys.env-style blob. Tolerates blank lines,
    ``# comment`` lines, and surrounding whitespace. Optional matching single or
    double quotes around the value are stripped. Malformed lines are warned
    about and skipped."""
    result: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            ui.warning(f"{KEYS_ENV_PATH}:{lineno}: ignoring malformed line (no '='): {raw!r}")
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            ui.warning(f"{KEYS_ENV_PATH}:{lineno}: ignoring line with empty key: {raw!r}")
            continue
        # Strip a matching pair of surrounding quotes if present.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


def load_keys_env() -> None:
    """Read ``KEYS_ENV_PATH`` (KEY=value lines, ``#`` comments OK) and apply each
    entry to ``os.environ`` *only* when the variable is not already set —
    existing environment values always win. Silently no-ops when the file is
    missing; malformed lines are warned and skipped."""
    if not KEYS_ENV_PATH.exists():
        return
    try:
        text = KEYS_ENV_PATH.read_text(encoding='utf-8', errors='replace')
    except (OSError, UnicodeDecodeError) as exc:
        ui.warning(f"Cannot read {KEYS_ENV_PATH}: {exc}")
        return
    for key, value in _parse_keys_env_text(text).items():
        if key not in os.environ:
            os.environ[key] = value


def persist_keys_to_env_file(keys: dict[str, str]) -> Path:
    """Atomically write/merge ``keys`` into ``KEYS_ENV_PATH`` with mode 0o600.

    If the file already exists, the existing keys are read and merged: any
    keys passed in replace existing values for the same name, other keys are
    preserved. Whitespace around keys and values is stripped, and entries with
    an empty value are dropped. Returns ``KEYS_ENV_PATH``."""
    parent = KEYS_ENV_PATH.parent
    if parent.exists() and not parent.is_dir():
        ui.error(
            f"Cannot save API keys: {parent} exists but is not a directory. "
            f"Move or remove it (e.g. `mv {parent} {parent}.bak`) and re-run `deep-report --setup`."
        )
        return KEYS_ENV_PATH
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        ui.error(f"Cannot create {parent}: {exc}")
        return KEYS_ENV_PATH

    # Start from existing on-disk keys so a merge preserves anything the user
    # already pasted, then overlay the incoming dict.
    merged: dict[str, str] = {}
    if KEYS_ENV_PATH.exists():
        try:
            merged.update(_parse_keys_env_text(KEYS_ENV_PATH.read_text(encoding='utf-8', errors='replace')))
        except (OSError, UnicodeDecodeError) as exc:
            ui.warning(f"Cannot read existing {KEYS_ENV_PATH}: {exc} — overwriting.")
            merged = {}

    for raw_key, raw_value in keys.items():
        if not isinstance(raw_key, str):
            continue
        key = raw_key.strip()
        if not key:
            continue
        value = (raw_value if isinstance(raw_value, str) else "").strip()
        if not value:
            continue
        merged[key] = value

    header = (
        "# deep-report API keys — sourced by `deep-report` on startup.\n"
        "# Existing environment variables always win; entries here only fill gaps.\n"
        "# Edit with care: this file is chmod 0600 and may contain secrets.\n"
    )
    body_lines = [f"{k}={v}" for k, v in sorted(merged.items())]
    body = (header + "\n".join(body_lines) + ("\n" if body_lines else "")).encode("utf-8")

    tmp = KEYS_ENV_PATH.with_suffix(f".tmp.{os.getpid()}.{time.monotonic_ns()}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(body)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        os.replace(tmp, KEYS_ENV_PATH)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise
    try:
        KEYS_ENV_PATH.chmod(0o600)
    except OSError:
        pass
    _warn_windows_chmod_once(KEYS_ENV_PATH)
    return KEYS_ENV_PATH


def prompt_for_missing_keys(enabled: set[str]) -> dict[str, str]:
    """Prompt the user (one ``questionary.text`` per var) for any required env
    vars that the enabled catalog specs need but ``os.environ`` lacks.

    Returns a ``{VAR: value}`` dict of *non-empty* pasted values (skipped vars
    are dropped). Silently returns ``{}`` when stdin/stdout aren't a TTY or
    when ``questionary`` isn't importable."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return {}
    try:
        import questionary as _q  # local import keeps headless paths free
    except ImportError:
        return {}

    # Deduplicate while preserving spec order so prompts come in catalog order.
    asked: list[tuple[str, MCPSpec]] = []
    seen: set[str] = set()
    for spec in CATALOG:
        if spec.key not in enabled:
            continue
        for var in spec.required_env:
            if var in os.environ or var in seen:
                continue
            seen.add(var)
            asked.append((var, spec))

    collected: dict[str, str] = {}
    for var, spec in asked:
        signup = spec.key_signup_url or "see provider docs"
        prompt = f"Paste {var} (or Enter to skip — sign up: {signup})"
        try:
            answer = _q.text(prompt).ask()
        except (KeyboardInterrupt, EOFError):
            break
        if answer is None:
            # User hit Ctrl+C inside questionary — stop prompting further keys.
            break
        value = answer.strip()
        if value:
            collected[var] = value
    return collected


# ──────────────────────────────────────────────────────────────────────
# Headless save + status
# ──────────────────────────────────────────────────────────────────────

def save_config_from_env() -> dict:
    """Auto-enable every catalog spec that is runnable on this host *and* has
    every ``required_env`` var already set in ``os.environ``. Calls
    :func:`save_config` with the resulting selection and returns the payload
    that was written."""
    prereqs = detect_prereqs()
    selected: list[str] = []
    for spec in CATALOG:
        runnable, _ = _spec_runnable(spec, prereqs)
        if not runnable:
            continue
        miss_req, _ = _spec_env_status(spec)
        if miss_req:
            continue
        selected.append(spec.key)
    save_config(sorted(selected))
    cfg = load_config() or {
        "version": 2,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "enabled": sorted(selected),
    }
    return cfg


def status_report() -> dict:
    """Snapshot the saved config plus computed runtime status for each enabled
    server. Returns sane defaults (empty lists, ``None`` fields) when no config
    has been saved yet — never raises."""
    cfg = load_config() or {}
    prereqs = detect_prereqs()
    enabled = enabled_keys() or set()
    imported = imported_servers()

    enabled_entries: list[dict] = []
    unset_keys: list[str] = []
    seen_unset: set[str] = set()
    for spec in CATALOG:
        if spec.key not in enabled:
            continue
        runnable, reason = _spec_runnable(spec, prereqs)
        miss_req, _ = _spec_env_status(spec)
        enabled_entries.append({
            "key": spec.key,
            "display_name": spec.display_name,
            "tier": spec.tier,
            "prereq_ok": runnable,
            "prereq_reason": reason or None,
            "env_ok": not miss_req,
            "missing_env": list(miss_req),
        })
        for var in miss_req:
            if var not in seen_unset:
                seen_unset.add(var)
                unset_keys.append(var)

    imported_entries: list[dict] = []
    for name, blk in sorted(imported.items()):
        imported_entries.append({
            "name": name,
            "type": blk.get("type", "stdio"),
            "summary": _summarize_cc_entry(blk),
        })

    return {
        "config_path": str(CONFIG_PATH),
        "config_version": cfg.get("version") if isinstance(cfg, dict) else None,
        "saved_at": cfg.get("saved_at") if isinstance(cfg, dict) else None,
        "enabled": enabled_entries,
        "imported": imported_entries,
        "unset_keys_for_enabled": unset_keys,
        "keys_env_path": str(KEYS_ENV_PATH) if KEYS_ENV_PATH.exists() else None,
    }


def print_status() -> int:
    """Render :func:`status_report` for humans (Rich panel) or for machines
    (single JSON line on stdout when ``ui`` is in machine mode). Always
    returns 0 — read the dict yourself if you need richer signalling."""
    report = status_report()
    if getattr(ui, "_machine_mode", False):
        print(json.dumps(report))
        return 0

    ui.header("deep-report MCP status")
    if not report["saved_at"]:
        ui.warning(
            f"No saved config at {report['config_path']} — run `deep-report --setup` "
            "(or set API keys + start a run; first-run auto-saves a config)."
        )
        return 0

    ui.info(f"Config:   {report['config_path']}")
    ui.info(f"Version:  {report['config_version']}")
    ui.info(f"Saved at: {report['saved_at']}")
    if report["keys_env_path"]:
        ui.info(f"Keys env: {report['keys_env_path']}  (chmod 0600)")
    else:
        ui.info("Keys env: (none — paste keys with --setup to create one)")

    ui.header("Enabled servers")
    if not report["enabled"]:
        ui.warning("No servers enabled — deep-report will fall back to built-in WebSearch/WebFetch.")
    else:
        for entry in report["enabled"]:
            tier_badge = TIER_BADGE.get(entry["tier"], entry["tier"].upper())
            if entry["env_ok"] and entry["prereq_ok"]:
                state = "ready"
            elif not entry["prereq_ok"]:
                state = f"blocked ({entry['prereq_reason']})"
            else:
                state = f"missing {', '.join(entry['missing_env'])}"
            ui.info(f"  • {entry['display_name']}  [{tier_badge}]  — {state}")

    if report["imported"]:
        ui.header("Imported from Claude Code")
        for entry in report["imported"]:
            ui.info(f"  • {entry['name']}  — {entry['summary']}")

    if report["unset_keys_for_enabled"]:
        ui.header("Action required")
        ui.warning("These environment variables are missing for enabled servers:")
        for var in report["unset_keys_for_enabled"]:
            ui.info(f"  - {var}")
        ui.info(
            "\nPaste them with `deep-report --setup`, or add them to your shell rc "
            f"or {KEYS_ENV_PATH}."
        )
    return 0


# ──────────────────────────────────────────────────────────────────────
# Wizard
# ──────────────────────────────────────────────────────────────────────

def _tier_color(tier: str) -> str:
    return {
        TIER_FREE: "green",
        TIER_FREE_TIER: "yellow",
        TIER_PAID: "red",
    }.get(tier, "white")


def _render_choice(spec: MCPSpec, runnable: bool, missing_reason: str) -> "Choice":
    badge = TIER_BADGE.get(spec.tier, spec.tier.upper())
    title = f"{spec.display_name}  [{badge}]  — {spec.summary}"
    if not runnable:
        title += f"   ({missing_reason})"
    # Nothing is pre-checked. The caller flips `checked` on re-runs to restore
    # the user's previous selection — first-run users opt in explicitly.
    return Choice(
        title=title,
        value=spec.key,
        checked=False,
        disabled=missing_reason if not runnable else False,
    )


def _print_summary(enabled: set[str]) -> None:
    """Print what's enabled vs what still needs keys."""
    by_cat: dict[str, list[MCPSpec]] = {}
    for s in CATALOG:
        if s.key in enabled:
            by_cat.setdefault(s.category, []).append(s)

    ui.header("Configured")
    if not by_cat:
        ui.warning("No servers enabled — deep-report will fall back to built-in WebSearch/WebFetch.")
        return

    missing_keys: list[tuple[MCPSpec, list[str]]] = []
    for cat, specs in by_cat.items():
        ui.info(f"\n{CATEGORY_LABEL.get(cat, cat).upper()}:")
        for s in specs:
            miss_req, miss_opt = _spec_env_status(s)
            status = "ready" if not miss_req else f"missing {', '.join(miss_req)}"
            ui.info(f"  • {s.display_name} — {status}")
            if miss_req:
                missing_keys.append((s, miss_req))

    if missing_keys:
        ui.header("Action required")
        ui.warning("Some enabled servers need API keys before they'll run.")
        for s, missing in missing_keys:
            ui.info(f"\n  {s.display_name}")
            ui.info(f"    Cost:       {s.cost_note or '(unspecified)'}")
            if s.key_signup_url:
                ui.info(f"    Sign up:    {s.key_signup_url}")
            ui.info(f"    Then set:   {' '.join(f'{v}=...' for v in missing)}")
        ui.info("\nAdd these to your shell rc (e.g. ~/.zshrc) or a project .env file.")


def run_wizard() -> int:
    """Run the interactive setup wizard. Returns process exit code."""
    if questionary is None:
        ui.error("questionary is not installed — required for the setup wizard.")
        ui.info("Run: pip install questionary")
        return 1

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        ui.error("--setup needs an interactive terminal (arrow keys + space toggles).")
        ui.info(f"To pre-seed without a TTY, write {CONFIG_PATH} yourself "
                "(see docs for schema).")
        return 2

    ui.header("deep-report MCP setup")
    ui.info(
        "Pick the MCP servers you want deep-report to use.\n"
        "Nothing is pre-selected — tick (Space) what you want, then Enter."
    )

    # Prereqs
    prereqs = detect_prereqs()
    ui.info("\nPrereqs detected:")
    ui.info(f"  Node + npx : {'yes' if prereqs['node'] else 'no'}")
    ui.info(f"  uv  / uvx  : {'yes' if prereqs['uv']   else 'no'}")
    ui.info(f"  Docker     : {'yes' if prereqs['docker'] else 'no'}")
    if not any(prereqs.values()):
        ui.warning(
            "No external runtimes detected. You'll only be able to use HTTP-based"
            " MCPs (Tavily, Exa) — install Node from https://nodejs.org for the rest."
        )

    # Existing config — pre-select what was enabled before, if any.
    existing = enabled_keys()
    if existing is not None:
        ui.info(f"\nFound existing config at {CONFIG_PATH} — pre-selecting previous picks.")

    # CC-imported servers — anything the user has configured in Claude Code that
    # we don't already cover with the catalog (paper-search is explicitly blocked).
    cc_imports = discover_cc_servers()
    if cc_imports:
        ui.info(f"\nDiscovered {len(cc_imports)} MCP(s) from your Claude Code config — "
                "you can opt these into deep-report below.")

    # Build checkbox list, grouped by category. questionary doesn't support
    # group headers natively, so we use Separator-style disabled choices.
    choices: list = []
    for cat in CATEGORY_LABEL:
        cat_specs = [s for s in CATALOG if s.category == cat]
        if not cat_specs:
            continue
        choices.append(questionary.Separator(f"\n── {CATEGORY_LABEL[cat]} ──"))
        for spec in cat_specs:
            runnable, reason = _spec_runnable(spec, prereqs)
            choice = _render_choice(spec, runnable, reason)
            # Override default-checked with the saved preference if present.
            if existing is not None:
                choice.checked = spec.key in existing and runnable
            choices.append(choice)

    # Append the CC-imports section if we found any.
    if cc_imports:
        choices.append(questionary.Separator("\n── Imported from Claude Code ──"))
        for name, cfg in sorted(cc_imports.items()):
            label = f"{name}  [IMPORTED]  — {_summarize_cc_entry(cfg)}"
            # Nothing pre-checked — user opts in (matches catalog-row behaviour).
            check = name in existing if existing is not None else False
            choices.append(Choice(title=label, value=name, checked=check))

    selection: Optional[list[str]] = questionary.checkbox(
        "Servers to enable (Space to toggle, Enter to confirm):",
        choices=choices,
    ).ask()

    if selection is None:
        ui.info("Cancelled.")
        return 1

    enabled = set(selection)
    # Persist the full config for any CC import the user kept ticked, so the
    # runtime doesn't need to re-read ~/.claude.json on every spawn.
    imported_to_save = {name: cfg for name, cfg in cc_imports.items() if name in enabled}
    path = save_config(sorted(enabled), imported=imported_to_save or None)
    ui.success(f"Saved to {path}")

    if sys.stdin.isatty() and sys.stdout.isatty():
        collected = prompt_for_missing_keys(enabled)
        if collected:
            persist_keys_to_env_file(collected)
            for k, v in collected.items():
                os.environ[k] = v  # apply for current process
            ui.success(f"Saved {len(collected)} API keys to {KEYS_ENV_PATH} (0o600).")

    _print_summary(enabled)
    if imported_to_save:
        ui.info(f"\nImported from Claude Code ({len(imported_to_save)}):")
        for name in sorted(imported_to_save):
            ui.info(f"  • {name}")
    return 0
