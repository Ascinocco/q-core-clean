"""Generate the inventory embedded in docs/SYSTEM.md.

Derived, not typed. A hand-written inventory of routes and tools rots
silently -- the MCP tool descriptions went stale against `EntityType`
and nothing failed, because prose does not execute. So this reads the
live objects (the FastAPI app's route table, the MCP server's registered
tools, a bootstrapped schema) and the tree, and
`tests/test_system_doc.py` fails when the copy embedded in the doc no
longer matches what this produces.

Run it after any change that adds a route, a tool, a table, a migration
or a skill:

    python scripts/system_inventory.py --write

Without --write it prints to stdout, which is what the test compares.
"""

from __future__ import annotations

import argparse
import ast
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

BEGIN = "<!-- BEGIN GENERATED INVENTORY -->"
END = "<!-- END GENERATED INVENTORY -->"
DOC = REPO_ROOT / "docs" / "SYSTEM.md"


#: Abbreviations that end in a period and do not end a sentence. Without
#: these, create_relationship's description cuts at "Link two entities,
#: e.g." -- a purpose line that stops before it says anything.
_ABBREVIATIONS = ("e.g.", "i.e.", "etc.", "vs.", "cf.", "approx.")


def _first_sentence(text: str, limit: int = 110) -> str:
    """One line of purpose, from prose written for a different audience."""
    text = " ".join((text or "").split())
    search_from = 0
    while True:
        candidates = [
            text.index(stop, search_from)
            for stop in (". ", "! ", "? ")
            if stop in text[search_from:]
        ]
        if not candidates:
            break
        cut = min(candidates)
        if any(text[: cut + 1].endswith(abbr) for abbr in _ABBREVIATIONS):
            search_from = cut + 2
            continue
        text = text[: cut + 1]
        break
    # Markdown table cells: an unescaped pipe would split the column.
    text = text.replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _settings(tmp: Path):
    from api.config import Settings

    return Settings(
        _env_file=None,
        api_token="inventory",
        db_path=str(tmp / "q-core.db"),
        documents_dir=str(tmp / "documents"),
        jyra_dir=str(tmp / "jyra"),
        logs_dir=str(tmp / "logs"),
        intake_dir=str(tmp / "intake"),
        inbox_dir=str(tmp / "inbox"),
        schema_path=str(REPO_ROOT / "db" / "schema.sql"),
        seed_categories_path=str(REPO_ROOT / "db" / "seed_categories.sql"),
        migrations_dir=str(REPO_ROOT / "db" / "migrations"),
    )


def routes() -> list[str]:
    """Every HTTP route, grouped by the module whose router defines it.

    Walks each module's own `router` rather than `app.routes`. The app's
    route list wraps every `include_router` in a private
    `_IncludedRouter` whose nested routes are not reachable through any
    public attribute -- walking it flat finds `/health` and `/` and
    nothing else, which renders as a two-route API and looks plausible.
    Importing the routers is both more honest and version-proof.
    """
    import importlib

    by_module: dict[str, list[tuple[str, str]]] = {}
    for path in sorted((REPO_ROOT / "api").glob("*.py")):
        if path.name.startswith("_"):
            continue
        module = importlib.import_module(f"api.{path.stem}")
        router = getattr(module, "router", None) or getattr(module, "app", None)
        for route in getattr(router, "routes", []):
            route_path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if not route_path or not methods:
                continue
            # Only what this repo declares. `api/main.py` exposes the app
            # itself, whose route table also carries FastAPI's built-in
            # /docs, /redoc, /openapi.json and /docs/oauth2-redirect --
            # four routes nobody here wrote, which any FastAPI app has.
            # Listing them overstated the API surface by six rows against
            # two real ones, and an inventory that inflates itself is
            # worse than one that is merely incomplete: it reads as a
            # bigger system than exists. Found by the independent
            # cross-check in tests/test_system_doc.py disagreeing, 70
            # against 64.
            endpoint_module = getattr(
                getattr(route, "endpoint", None), "__module__", ""
            ) or ""
            if not endpoint_module.startswith("api."):
                continue
            verb = ",".join(sorted(m for m in methods if m != "HEAD"))
            by_module.setdefault(path.stem, []).append((route_path, verb))

    if not by_module:
        raise SystemExit(
            "no API routes discovered -- the route walk broke. An empty "
            "inventory section would otherwise render as 'this API has no "
            "routes' and the doc test would happily pin it."
        )
    lines = []
    for module in sorted(by_module):
        lines.append(f"\n**`api/{module}.py`**\n")
        for route_path, verb in sorted(set(by_module[module])):
            lines.append(f"- `{verb} {route_path}`")
    return lines


def mcp_tools() -> list[str]:
    import anyio
    import httpx

    from q_core_mcp.client import QCoreClient
    from q_core_mcp.server import build_server

    with tempfile.TemporaryDirectory() as raw:
        settings = _settings(Path(raw))
        client = QCoreClient(
            settings,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        )
        tools = anyio.run(build_server(client).list_tools)

    lines = ["", "| tool | purpose |", "|---|---|"]
    for tool in sorted(tools, key=lambda t: t.name):
        lines.append(f"| `{tool.name}` | {_first_sentence(tool.description)} |")
    return lines


def schema() -> list[str]:
    """Tables and views from a bootstrapped database.

    Bootstrapped rather than parsed out of schema.sql, so anything a
    migration adds appears here too -- the doc describes what a running
    system has, not what the baseline file declares.
    """
    from api.db import apply_startup_migrations, init_db

    with tempfile.TemporaryDirectory() as raw:
        settings = _settings(Path(raw))
        init_db(settings)
        apply_startup_migrations(settings)
        connection = sqlite3.connect(settings.db_path)
        try:
            rows = connection.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
                "ORDER BY type, name"
            ).fetchall()
        finally:
            connection.close()

    tables = [name for kind, name in rows if kind == "table"]
    views = [name for kind, name in rows if kind == "view"]
    return [
        "",
        f"**Tables ({len(tables)})** — " + ", ".join(f"`{t}`" for t in tables),
        "",
        f"**Views ({len(views)})** — " + ", ".join(f"`{v}`" for v in views),
    ]


def migrations() -> list[str]:
    files = sorted((REPO_ROOT / "db" / "migrations").glob("*.sql"))
    return [""] + [f"- `{path.name}`" for path in files]


def skills() -> list[str]:
    lines = ["", "| skill | what it does |", "|---|---|"]
    for path in sorted((REPO_ROOT / "plugin/skills").glob("*/SKILL.md")):
        text = path.read_text()
        match = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
        lines.append(
            f"| `{path.parent.name}` | "
            f"{_first_sentence(match.group(1) if match else '')} |"
        )
    return lines


def test_counts(root: Path = REPO_ROOT) -> list[str]:
    """Test FUNCTIONS per package, counted from the AST.

    Functions, not collected cases: a parametrized function is one entry
    here and many at collection. Counted this way on purpose -- deriving
    it from a pytest run would mean running pytest inside the test that
    checks this doc.

    Recursive, as pytest's collection is: a package's subdirectories
    (api/tests/browser/) hold tests too. A flat glob left them out of the
    counts and nothing noticed (ticket T-78).
    """
    lines = ["", "| package | test files | test functions |", "|---|---|---|"]
    total_files = total_fns = 0
    for package in ("api/tests", "q_core_mcp/tests", "tests"):
        files = sorted((root / package).rglob("test_*.py"))
        count = 0
        for path in files:
            tree = ast.parse(path.read_text())
            count += sum(
                1
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test_")
            )
        total_files += len(files)
        total_fns += count
        lines.append(f"| `{package}/` | {len(files)} | {count} |")
    lines.append(f"| **total** | **{total_files}** | **{total_fns}** |")
    return lines


def render() -> str:
    parts: list[str] = [
        BEGIN,
        "",
        "> Generated by `scripts/system_inventory.py`. Do not edit by hand —",
        "> `tests/test_system_doc.py` fails when this block and the tree",
        "> disagree. Regenerate with `python scripts/system_inventory.py --write`.",
        "",
        "### HTTP routes",
        *routes(),
        "",
        "### MCP tools",
        *mcp_tools(),
        "",
        "### Schema",
        *schema(),
        "",
        "### Migrations",
        *migrations(),
        "",
        "### Skills",
        *skills(),
        "",
        "### Tests",
        *test_counts(),
        "",
        END,
    ]
    return "\n".join(parts) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write", action="store_true", help="write the block into docs/SYSTEM.md"
    )
    args = parser.parse_args()
    block = render()
    if not args.write:
        sys.stdout.write(block)
        return 0

    text = DOC.read_text()
    if BEGIN not in text or END not in text:
        sys.stderr.write(f"{DOC} has no generated-inventory markers\n")
        return 1
    updated = text[: text.index(BEGIN)] + block + text[text.index(END) + len(END) + 1 :]
    DOC.write_text(updated)
    sys.stderr.write(f"wrote inventory into {DOC}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
