#!/usr/bin/env python3
"""Check that the MCP catalog table in README.md matches the generated catalog.

Usage:
    python3 scripts/check_catalog_drift.py            # exit 0 if in sync, 1 if drifted
    python3 scripts/check_catalog_drift.py --update   # rewrite README between markers

The script reads the text between the HTML-comment markers
``<!-- CATALOG-TABLE:START`` and ``<!-- CATALOG-TABLE:END -->`` in
``README.md`` and compares it with the output of
``deep_report.orchestrator.catalog_render.render_catalog_markdown``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README_PATH = REPO_ROOT / "README.md"

START_RE = re.compile(r"<!-- CATALOG-TABLE:START[^>]*-->")
END_MARKER = "<!-- CATALOG-TABLE:END -->"

BLOCK_RE = re.compile(
    r"(<!-- CATALOG-TABLE:START[^>]*-->)(.*?)(<!-- CATALOG-TABLE:END -->)",
    re.DOTALL,
)


def _normalize(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    out: list[str] = []
    blank = False
    for line in lines:
        if line == "":
            if blank:
                continue
            blank = True
        else:
            blank = False
        out.append(line)
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def _extract_block(readme: str) -> str:
    match = BLOCK_RE.search(readme)
    if not match:
        print(
            "ERROR: CATALOG-TABLE markers not found in README.md.",
            file=sys.stderr,
        )
        sys.exit(2)
    return match.group(2)


def _render_generated() -> str:
    src = REPO_ROOT / "src"
    if src.is_dir():
        sys.path.insert(0, str(src))
    sys.path.insert(0, str(REPO_ROOT))

    # Load ``deep_report.orchestrator.catalog_render`` without triggering
    # ``deep_report/__init__.py`` (which calls ``importlib.metadata.version``
    # and requires the package to be installed). The renderer only needs its
    # sibling ``mcp_catalog`` module, so we load both directly from source.
    import importlib.util

    pkg_root = REPO_ROOT / "src" / "deep_report" / "orchestrator"
    catalog_path = pkg_root / "mcp_catalog.py"
    render_path = pkg_root / "catalog_render.py"
    if not catalog_path.is_file() or not render_path.is_file():
        print(
            "ERROR: orchestrator sources not found; expected at "
            f"{pkg_root}.",
            file=sys.stderr,
        )
        sys.exit(2)

    def _load(name: str, path: Path):
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {name} from {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    try:
        _load("_dr_mcp_catalog", catalog_path)
        # ``catalog_render`` uses ``from .mcp_catalog import ...`` — patch
        # ``sys.modules`` so the relative import resolves to the module we
        # just loaded.
        sys.modules.setdefault(
            "deep_report", type(sys)("deep_report")  # type: ignore[arg-type]
        )
        orch_pkg = type(sys)("deep_report.orchestrator")  # type: ignore[arg-type]
        orch_pkg.__path__ = [str(pkg_root)]  # type: ignore[attr-defined]
        sys.modules["deep_report.orchestrator"] = orch_pkg
        sys.modules["deep_report.orchestrator.mcp_catalog"] = sys.modules[
            "_dr_mcp_catalog"
        ]
        render_mod = _load(
            "deep_report.orchestrator.catalog_render", render_path
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: could not load catalog_render: {exc}", file=sys.stderr)
        sys.exit(2)
    return render_mod.render_catalog_markdown()


def _rewrite(readme: str, generated: str) -> str:
    replacement = f"\n{generated.strip()}\n"
    return BLOCK_RE.sub(
        lambda m: f"{m.group(1)}{replacement}{m.group(3)}",
        readme,
        count=1,
    )


def main() -> int:
    update = "--update" in sys.argv[1:]
    readme = README_PATH.read_text(encoding="utf-8")
    current = _extract_block(readme)
    generated = _render_generated()

    if _normalize(current) == _normalize(generated):
        if not update:
            print("OK")
        return 0

    if update:
        new_readme = _rewrite(readme, generated)
        README_PATH.write_text(new_readme, encoding="utf-8")
        print("README catalog table updated.")
        return 0

    print(
        "README catalog table is out of date.\n"
        "Run: python3 scripts/check_catalog_drift.py --update",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
