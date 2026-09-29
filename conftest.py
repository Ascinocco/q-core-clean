"""Repo-root test configuration.

Deliberately at the root rather than in `api/tests/`: it has to apply to
every test package, including `tests/` and `q_core_mcp/tests/`. Covering
only `api/tests` is what made the previous guard insufficient.
"""

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _publish_pytest_temp_root(tmp_path_factory):
    """Tell library code where a test is allowed to write.

    `api.db` refuses to migrate a database outside this root while
    `PYTEST_CURRENT_TEST` is set. It cannot ask pytest directly —
    `tmp_path_factory` exists only inside a test run — and
    `tempfile.gettempdir()` is the wrong answer under `--basetemp`, which
    can point anywhere, including inside the repo. So the harness
    publishes the authoritative value and the library reads it.

    Session-scoped and autouse: the value is constant for a run, and
    nothing should have to opt in to being unable to touch real data.
    """
    os.environ["Q_CORE_TEST_TMP_ROOT"] = str(tmp_path_factory.getbasetemp())
    yield
    os.environ.pop("Q_CORE_TEST_TMP_ROOT", None)


@pytest.fixture(autouse=True)
def _never_the_keychain_or_google(monkeypatch):
    """No test may read this machine's Keychain or call Google.

    Autouse and without an opt-out, for the same reason as the settings
    guards: a test that forgets is the one that does the damage. It happened:
    a test that left the token store unset read the real OAuth client from
    the Keychain and sent it to Google's token endpoint (q-core #12). Tests
    that exercise the Keychain path patch `subprocess.run` themselves, which
    overrides this proxy's `run`; everything else here raises.
    """
    import subprocess
    import types

    from api import google_calendar

    def no_network(*args, **kwargs):
        raise AssertionError("a test tried to call Google")

    def guarded_run(args, *rest, **kwargs):
        if args and str(args[0]).rsplit("/", 1)[-1] == "security":
            raise AssertionError("a test tried to read the Keychain")
        return subprocess.run(args, *rest, **kwargs)

    proxy = types.SimpleNamespace(run=guarded_run, CalledProcessError=subprocess.CalledProcessError)
    monkeypatch.setattr(google_calendar, "urlopen", no_network)
    monkeypatch.setattr(google_calendar, "subprocess", proxy)


# --------------------------------------------------------------------------
# Every directory holding tests must be declared in `testpaths` (ticket T-57).
#
# THIS LIVES IN THE ROOT CONFTEST ON PURPOSE. It was a test inside
# `tests/`, which cannot detect its own package being dropped from
# `testpaths` -- the guard goes out of scope with the thing it guards.
# Measured on main: removing `tests/` gives a GREEN suite with 143 tests
# silently not run. A suite that stops running tests and still passes is
# the most expensive kind of wrong, because nothing about the output
# distinguishes it from success.
#
# pytest loads the rootdir conftest regardless of which paths it collects,
# so a check here survives the deletion it is checking for. That is the
# whole reason it is not a test.
# --------------------------------------------------------------------------

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

#: Directories excluded from the walk: not source, or another checkout.
_NOT_SOURCE = {".venv", ".git", "__pycache__", ".claude", "node_modules", ".worktrees"}


def _declared_testpaths() -> list[str]:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return config["tool"]["pytest"]["ini_options"]["testpaths"]


def _directories_holding_tests() -> list[str]:
    found = set()
    # Prune independent repositories before descending, rather than walking
    # all their dependencies and filtering afterwards. Only root projects/
    # is excluded: api/tests/projects still belongs to this repository.
    for directory, children, files in os.walk(REPO_ROOT):
        relative = Path(directory).relative_to(REPO_ROOT)
        children[:] = [name for name in children if name not in _NOT_SOURCE
                       and not (relative == Path(".") and name == "projects")]
        if any(name.startswith("test_") and name.endswith(".py") for name in files):
            found.add(str(relative))
    return sorted(found)


def pytest_configure(config):
    """Fail the RUN, not a test, when a test directory is undeclared.

    DELIBERATELY NOT A TEST, and it must stay that way. It cannot be run
    in isolation and has no name in the report, because **nothing that
    can be deselected can cover the case where its own package is gone**.
    A test here would be the bug it exists to catch. If you are reading
    this while tidying it into a test file, that is the move this comment
    exists to stop.

    `pytest.UsageError`, not a bare `assert`. An assert in this hook
    fires before collection and pytest renders it as
    `INTERNALERROR> AssertionError` with a traceback -- loud and correct,
    and it reads as a bug in the harness, which is the reading most
    likely to get it "fixed" by deletion. A UsageError reads as a
    deliberate refusal and exits 4.
    """
    walked = _directories_holding_tests()
    # The vacuity check, and a UsageError for the same reason: a broken
    # walk must fail the way an undeclared directory does, not pass over
    # an empty set. Measured before this existed -- breaking the rglob
    # gave `exit=0, 1863 tests collected`, the guard reporting everything
    # covered while reading nothing.
    if not walked:
        raise pytest.UsageError(
            "the test-directory walk found nothing at all, so testpath "
            "coverage was not actually checked. Fix the walk in "
            "conftest.py rather than ignoring this."
        )
    # A directory is covered if it IS a declared testpath or sits UNDER
    # one -- pytest collects recursively. Exact matching reported
    # `api/tests/nested` as "not being RUN" when pytest runs it happily,
    # which is the same wrong-message bug review-2 flagged in
    # `_test_modules`'s glob, reproduced here in the fix for it.
    declared = [Path(p) for p in _declared_testpaths()]
    undeclared = [
        directory
        for directory in walked
        if not any(
            Path(directory) == root or root in Path(directory).parents
            for root in declared
        )
    ]
    if undeclared:
        raise pytest.UsageError(
            f"directories holding test files that no pytest testpath covers: "
            f"{undeclared}. Their tests are NOT BEING RUN, and the suite will "
            f"pass without them. Add them to `testpaths` in pyproject.toml."
        )
