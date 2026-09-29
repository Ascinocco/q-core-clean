"""docs/SYSTEM.md's inventory must match the tree.

The doc has two halves. The prose is written by hand and reviewed like
prose. The inventory is generated, and this test is the reason it can be
trusted: a route, tool, table, migration or skill that lands without the
doc being regenerated turns this red.

That split exists because of a measured failure, not a worry. The MCP
tool descriptions went stale against `EntityType` when `project` was
added and nothing failed -- the tools kept working for every value they
did list. Prose is the part that rots silently, because nothing executes
it. An inventory is the part of a document most likely to be out of date
and least likely to be re-read, so it is the part that gets a test.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys

import pytest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "docs" / "SYSTEM.md"
SCRIPT = REPO_ROOT / "scripts" / "system_inventory.py"
BEGIN = "<!-- BEGIN GENERATED INVENTORY -->"
END = "<!-- END GENERATED INVENTORY -->"


def _embedded() -> str:
    text = DOC.read_text()
    assert BEGIN in text and END in text, (
        f"{DOC.name} has lost its generated-inventory markers, so this test "
        f"can no longer find the block it checks -- restore the markers "
        f"rather than deleting the test"
    )
    return text[text.index(BEGIN) : text.index(END) + len(END)]


def _generated() -> str:
    # PYTEST_CURRENT_TEST is stripped deliberately. The generator is a
    # script, not a test: it bootstraps a throwaway schema in its own
    # `tempfile.TemporaryDirectory` to read the table list. Inherited,
    # the variable makes `refuse_real_path_under_pytest` fire -- under
    # pytest the allowed root becomes pytest's basetemp, and the
    # script's temp dir is legitimately outside it.
    #
    # This weakens nothing. The guard exists to stop a TEST writing to
    # the real data/ paths; the script names temp paths only, and the
    # same reasoning is why the init_db benchmark must not run under
    # pytest either (decisions-log.md). If this line is ever deleted,
    # the symptom is a ProductionDatabaseInTestError from a subprocess,
    # not a silent pass.
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        f"scripts/system_inventory.py failed:\n{result.stderr}"
    )
    return result.stdout.strip()


def test_the_embedded_inventory_matches_the_tree():
    embedded, generated = _embedded().strip(), _generated()
    assert embedded == generated, (
        "docs/SYSTEM.md's inventory no longer matches the tree. Regenerate "
        "it with `python scripts/system_inventory.py --write` and commit the "
        "result. If the diff surprises you, the doc was stale rather than "
        "the code being wrong."
    )


# --------------------------------------------------------------------------
# Each section is cross-checked against an INDEPENDENT traversal that shares
# no code with the generator, and the counts must be EQUAL.
#
# This replaced floors, and the floors replaced two mixed totals. Each step
# was found by the next one failing:
#
#   1. Mixed totals (`"- `"` spanning routes AND migrations) let a whole
#      section render zero rows with every test green. review-1 found it,
#      and measured that the original flat-route-walk bug would have
#      cleared the mixed floor too once there were 14 migrations.
#   2. Floors fixed that and could still not see PARTIAL loss: a collector
#      dropping half its rows stays above a floor chosen loose enough not
#      to fail on legitimate growth. A floor is a number I chose; an
#      independent count is not.
#
# The operational form, since "name what it needs" felt satisfied by
# asserting the headings existed: EVERY COLLECTOR MUST BE INDEPENDENTLY
# FALSIFIABLE -- break each one alone, and something must fail that names
# it. Each cross-check below also asserts its own catch is non-empty, so a
# broken traversal cannot agree with a broken collector at zero.
# --------------------------------------------------------------------------

def _sections(generated: str) -> dict[str, list[str]]:
    """Split the generated block into its `### ` sections."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in generated.split("\n"):
        if line.startswith("### "):
            current = line[4:].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return sections


def test_every_inventory_section_is_present():
    sections = _sections(_generated())
    expected = {"HTTP routes", "MCP tools", "Schema", "Migrations", "Skills", "Tests"}
    missing = sorted(expected - set(sections))
    assert not missing, f"the generator produced no {missing} section(s)"


SKILLS_DIR = REPO_ROOT / "plugin/skills"
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"


def _rows(section: str, prefix: str = "- `") -> list[str]:
    lines = _sections(_generated()).get(section, [])
    return [line for line in lines if line.strip().startswith(prefix)]


def _reimplemented_route_rows() -> set[str]:
    """A CHECKSUM, not an independent source -- labelled so nobody reads
    it as one.

    This walks the module routers, which is the generator's own
    algorithm written a second time. It catches a typo on either side and
    is blind to a mistake in the shared idea.
    `test_route_rows_match_the_declared_decorators` is the check that can
    disagree about the idea; this one confirms the rendering.

    Returns the RENDERED ROWS, not a count. review-1 measured why that
    matters: comparing sizes lets two different sets agree by arithmetic
    accident, and their own decorator-based walk disagreed 70 against 74
    on count while the sets were equal, because the generator dedupes a
    path declared twice. A size is a property OF the thing; comparing
    sizes is not comparing the things. The schema cross-check already
    compares names for the same reason.
    """
    import importlib

    seen = set()
    for path in sorted((REPO_ROOT / "api").glob("*.py")):
        if path.name.startswith("_"):
            continue
        module = importlib.import_module(f"api.{path.stem}")
        router = getattr(module, "router", None) or getattr(module, "app", None)
        for route in getattr(router, "routes", []):
            methods = getattr(route, "methods", None) or set()
            verbs = sorted(m for m in methods if m != "HEAD")
            declared_here = (
                getattr(getattr(route, "endpoint", None), "__module__", "") or ""
            ).startswith("api.")
            if getattr(route, "path", None) and verbs and declared_here:
                seen.add(f"- `{','.join(verbs)} {route.path}`")
    return seen


#: The HTTP verbs a route decorator can name.
_ROUTE_VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}


def _decorated_route_rows() -> set[str]:
    """Routes read from `@router.<verb>("/path")` DECORATORS in the AST.

    The genuinely independent source, and review-1's finding is why it
    exists. `_reimplemented_route_rows` below walks the module routers --
    which is what the GENERATOR does, with the same glob, the same `_`
    skip, the same router-or-app fallback, the same HEAD filter and the
    same module-prefix test. Two implementations of one algorithm catch a
    typo in either and are blind to an error in the shared idea, which is
    exactly what the FastAPI built-ins bug was. That was caught only
    because the cross-check predated the filter; it would not have been
    caught a day later.

    This reads a different artifact: what the repo DECLARES. It is immune
    to the built-ins by construction rather than by filter -- FastAPI's
    `/docs` and `/openapi.json` have no decorator anywhere in this
    codebase, so they cannot appear here however the app is assembled.
    """
    rows: set[str] = set()
    for path in sorted((REPO_ROOT / "api").glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                func = decorator.func
                if not isinstance(func, ast.Attribute):
                    continue
                if func.attr not in _ROUTE_VERBS:
                    continue
                if getattr(func.value, "id", None) not in {"router", "app"}:
                    continue
                if not decorator.args or not isinstance(
                    decorator.args[0], ast.Constant
                ):
                    continue
                rows.add(f"- `{func.attr.upper()} {decorator.args[0].value}`")
    return rows


def test_route_rows_match_the_declared_decorators():
    """The primary route cross-check: what the repo declares."""
    expected = _decorated_route_rows()
    assert len(expected) > 20, "no route decorators found -- the AST walk broke"
    listed = {row.strip() for row in _rows("HTTP routes")}
    assert listed == expected, (
        f"missing from the doc: {sorted(expected - listed)}; "
        f"listed but not declared by any decorator in api/: "
        f"{sorted(listed - expected)}"
    )


def test_route_rows_match_the_reimplemented_walk():
    expected = _reimplemented_route_rows()
    assert len(expected) > 20, "the router walk found almost nothing"
    listed = {row.strip() for row in _rows("HTTP routes")}
    assert listed == expected, (
        f"missing from the doc: {sorted(expected - listed)}; "
        f"listed but not declared in api/: {sorted(listed - expected)}"
    )


def test_migration_rows_match_the_directory():
    expected = len(list(MIGRATIONS_DIR.glob("*.sql")))
    assert expected, "no migration files found by the independent glob"
    assert len(_rows("Migrations")) == expected


def test_skill_rows_match_the_directory():
    expected = len(list(SKILLS_DIR.glob("*/SKILL.md")))
    assert expected, "no skills found by the independent glob"
    assert len(_rows("Skills", "| `")) == expected


def test_tool_rows_match_the_registered_tool_set():
    """Counted through the SDK, not through the generator's renderer."""
    import tempfile

    import anyio
    import httpx

    from api.config import Settings
    from q_core_mcp.client import QCoreClient
    from q_core_mcp.server import build_server

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        settings = Settings(
            _env_file=None, api_token="t", db_path=str(tmp / "d.db"),
            documents_dir=str(tmp / "doc"), jyra_dir=str(tmp / "j"),
            logs_dir=str(tmp / "l"), intake_dir=str(tmp / "i"),
            inbox_dir=str(tmp / "in"),
            schema_path=str(REPO_ROOT / "db" / "schema.sql"),
            seed_categories_path=str(REPO_ROOT / "db" / "seed_categories.sql"),
            migrations_dir=str(MIGRATIONS_DIR),
        )
        client = QCoreClient(
            settings,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        )
        expected = len(anyio.run(build_server(client).list_tools))

    assert expected > 20, "the SDK reported almost no tools"
    assert len(_rows("MCP tools", "| `")) == expected


def test_schema_names_match_a_bootstrapped_database(tmp_path):
    """Names, not counts. A collector that listed the right NUMBER of
    wrong tables would pass a count and fail this."""
    import sqlite3

    from api.config import Settings
    from api.db import apply_startup_migrations, init_db

    # pytest's tmp_path, not a tempdir of our own: init_db refuses to
    # write anywhere outside the pytest temp root when PYTEST_CURRENT_TEST
    # is set, which is correct and which a self-made tempdir trips.
    if True:
        tmp = tmp_path
        settings = Settings(
            _env_file=None, api_token="t", db_path=str(tmp / "d.db"),
            documents_dir=str(tmp / "doc"), jyra_dir=str(tmp / "j"),
            logs_dir=str(tmp / "l"), intake_dir=str(tmp / "i"),
            inbox_dir=str(tmp / "in"),
            schema_path=str(REPO_ROOT / "db" / "schema.sql"),
            seed_categories_path=str(REPO_ROOT / "db" / "seed_categories.sql"),
            migrations_dir=str(MIGRATIONS_DIR),
        )
        init_db(settings)
        apply_startup_migrations(settings)
        connection = sqlite3.connect(settings.db_path)
        try:
            expected = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }
        finally:
            connection.close()

    assert expected, "the bootstrapped database reported no tables"
    section = "\n".join(_sections(_generated())["Schema"])
    listed = set(re.findall(r"`([a-z_]+)`", section))
    assert listed == expected, (
        f"missing from the doc: {sorted(expected - listed)}; "
        f"listed but not in the database: {sorted(listed - expected)}"
    )


def _independent_recount(root: Path, package: str) -> tuple[int, int]:
    """(test files, test functions) for one package, by a line-anchored scan.

    Recursive, like the generator and pytest's collection (q-core
    ticket T-78): a flat glob here disagreed with the generator as soon as a
    package gained a subdirectory (api/tests/browser/).
    """
    files = sorted((root / package).rglob("test_*.py"))
    counted = 0
    for path in files:
        source = path.read_text()
        tree = ast.parse(source)
        # Case 1: nothing nested. If `ast.walk` sees more test defs
        # than the module's top level does, the generator is counting
        # something this recount structurally cannot.
        walked = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        )
        top_level = sum(
            1
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        )
        assert walked == top_level, (
            f"{path.name} defines a test function inside another scope "
            f"({walked} via ast.walk vs {top_level} at the top level). "
            f"The generator counts it and this line-anchored recount "
            f"cannot, so the two are no longer independent answers to "
            f"the same question."
        )
        # Case 3: no `test`-without-underscore names, which a widened
        # generator prefix would silently start counting.
        loose = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test")
            and not node.name.startswith("test_")
        ]
        assert not loose, (
            f"{path.name} defines {loose}, which begins 'test' without "
            f"the underscore. Neither side counts it today, so a "
            f"generator widened to startswith('test') would diverge "
            f"with nothing to notice."
        )
        for line in source.split("\n"):
            # Case 2.
            assert not line.startswith("    def test_"), (
                f"{path} defines a test method inside a class, so this "
                f"line-anchored recount is no longer equivalent to the "
                f"generator's AST walk -- switch it or the cross-check "
                f"is checking nothing"
            )
            if line.startswith("def test_") or line.startswith(
                "async def test_"
            ):
                counted += 1
    return len(files), counted


def test_test_counts_match_an_independent_recount():
    """A line-anchored recount, not a second AST walk.

    The two mechanisms agree today, and there are exactly THREE ways they
    could stop agreeing. All three are asserted, because a cross-check
    whose equivalence conditions are unexercised is correct for reasons
    nothing pins:

    1. a test function nested inside another function -- `ast.walk`
       descends, a line-anchored scan does not;
    2. a test method inside a class -- same, via indentation;
    3. a name beginning `test` without the underscore -- neither counts
       it today, and a generator widened to `startswith("test")` would.

    review-2 measured that 4 of 6 generator mutations red this by name,
    so it IS a witness -- but the two it missed both come from this tree
    having zero instances of cases 1 and 3, which is the
    fixture-cannot-distinguish shape rather than the checksum shape. The
    docstring previously claimed the equivalence was guarded while
    guarding only case 2.
    """
    rows = _rows("Tests", "| `")
    assert rows, "the Tests section rendered no rows"

    for package in ("api/tests", "q_core_mcp/tests", "tests"):
        file_count, counted = _independent_recount(REPO_ROOT, package)
        assert file_count, f"no test files found in {package}"
        row = next(r for r in rows if f"`{package}/`" in r)
        cells = [c.strip() for c in row.strip("| ").split("|")]
        assert int(cells[1]) == file_count, (package, row)
        assert int(cells[2]) == counted, (package, row)


def test_the_doc_says_the_inventory_is_generated():
    """A reader who does not know the block is generated will hand-edit
    it, and the next regeneration will silently revert their work."""
    text = DOC.read_text()
    assert "scripts/system_inventory.py" in text
    assert "Do not edit by hand" in _embedded()


def _inventory_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("system_inventory_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_test_counts_include_nested_test_directories(tmp_path):
    """A test file in a package's subdirectory is counted (ticket T-78).

    The first nested directory (api/tests/browser/) was silently left out of
    the counts, and the whole-doc comparison above could not notice: the doc
    and the script agreed with each other, both wrong.
    """
    def write(relative, *names):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"def {name}():\n    pass\n\n" for name in names))

    write("api/tests/test_flat.py", "test_one")
    write("api/tests/browser/test_nested.py", "test_two", "test_three", "helper")
    write("api/tests/browser/deeper/test_deepest.py", "test_four")
    write("api/tests/browser/conftest.py", "test_not_a_test_file")
    write("q_core_mcp/tests/test_tool.py", "test_five")
    (tmp_path / "tests").mkdir()

    rows = _inventory_module().test_counts(tmp_path)

    assert "| `api/tests/` | 3 | 4 |" in rows
    assert "| `q_core_mcp/tests/` | 1 | 1 |" in rows
    assert "| `tests/` | 0 | 0 |" in rows
    assert "| **total** | **4** | **5** |" in rows


def test_the_recount_agrees_with_the_generator_on_nested_directories(tmp_path):
    """The recount is recursive too, so the cross-check still holds once a
    package has a subdirectory: both answer the same question."""
    def write(relative, *names):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"def {name}():\n    pass\n\n" for name in names))

    write("api/tests/test_flat.py", "test_one")
    write("api/tests/browser/test_nested.py", "test_two", "test_three")
    write("api/tests/browser/deeper/test_deepest.py", "test_four")

    assert _independent_recount(tmp_path, "api/tests") == (3, 4)
    assert "| `api/tests/` | 3 | 4 |" in _inventory_module().test_counts(tmp_path)
