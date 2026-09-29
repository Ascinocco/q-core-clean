"""No test module binds the same top-level name twice (ticket T-49).

Python keeps the LAST definition silently. In a test module that means a
helper can be shadowed by a later one with the same name, and every
earlier call site quietly starts using the newer body -- or, when the
signatures differ, fails with an error that points nowhere near the
cause.

MEASURED, THREE TIMES THIS RUN. The one that produced this ticket: a
`_coverage` helper appended to `api/tests/test_financial.py` shadowed an
existing `_coverage` 400 lines above it. Eight unrelated tests broke with
`TypeError: Header value must be str or bytes, not tuple` -- an error
about HTTP headers, from a duplicate function name, in tests that had not
been touched. Twice before that, a conflict resolution spliced a test
function in twice and pytest collected only the survivor, leaving a green
suite with one test silently gone.

WHY `--collect-only | uniq -d` CANNOT SEE THIS. That trick lists
collected TEST IDs, so it catches two tests with the same name -- but the
shadowed helper is not collected at all, and the shadowed *test* does not
appear twice either: it appears ONCE, because the duplicate replaced it
before collection. The thing to detect is a name bound twice in the
SOURCE, which only the AST shows.

Module-level only. A method named the same in two classes is fine, and a
name rebound inside a function is a local.
"""

from __future__ import annotations

import ast
import collections
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _test_packages() -> list[str]:
    """pytest's own `testpaths`, not a list maintained here.

    A hand-kept list would have to be updated by whoever adds a test
    package, which is the person who does not know this file exists.
    """
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    paths = config["tool"]["pytest"]["ini_options"]["testpaths"]
    assert paths, "pyproject declares no testpaths"
    return paths


#: Modules a test package can bind names in. `conftest.py` matters most
#: -- two fixtures with one name is the same silent shadowing, and the
#: later one wins for every test in the package.
_MODULE_GLOBS = ("test_*.py", "conftest.py", "_*.py", "helpers*.py")


def _test_modules() -> list[Path]:
    """Every module in a declared test package, found RECURSIVELY.

    `rglob`, not `glob`: a nested directory (`api/tests/financial/`) is
    inside a declared testpath and IS collected by pytest, but a
    non-recursive glob misses it. That produced two wrong answers at
    once -- the coverage check reported it as "not being RUN" when it is,
    and the duplicate check skipped it entirely. A bug that reports the
    wrong thing while hiding the right one.
    """
    modules: list[Path] = []
    for package in _test_packages():
        for pattern in _MODULE_GLOBS:
            modules.extend(sorted((REPO_ROOT / package).rglob(pattern)))
    return sorted(set(modules))


def test_the_discovery_finds_test_modules():
    """Guards the guard.

    An empty module list makes every parametrized case below vanish, and
    pytest turns an empty parametrize into a SKIP -- which reads as
    deliberate rather than as a broken check.
    """
    modules = _test_modules()
    assert len(modules) > 40, f"only {len(modules)} test modules found"

    # The "every test directory is declared" check MOVED to the root
    # conftest.py (ticket T-57). It cannot live here: a guard inside
    # `tests/` cannot detect `tests/` being dropped from `testpaths` --
    # it goes out of scope with the thing it guards, and the suite passes
    # with 143 tests silently not run. `pytest_configure` in the rootdir
    # conftest runs whatever is collected, so it survives its own
    # deletion.


@pytest.mark.parametrize("module", _test_modules(), ids=lambda p: p.name)
def test_no_test_module_binds_a_name_twice(module: Path):
    tree = ast.parse(module.read_text())
    names = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    duplicated = sorted(
        name for name, count in collections.Counter(names).items() if count > 1
    )

    assert not duplicated, (
        f"{module.name} defines {duplicated} more than once at module level. "
        f"Python keeps the LAST one silently: earlier call sites switch to "
        f"the new body, or fail with an error pointing nowhere near the "
        f"cause. Rename one, or delete it if they are the same."
    )


def test_the_walk_actually_reads_definitions():
    """The other vacuity: a walk that found no names anywhere would
    report every module clean while reading none of them."""
    total = 0
    for module in _test_modules():
        tree = ast.parse(module.read_text())
        total += sum(
            1
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        )
    assert total > 500, f"only {total} module-level definitions seen"
