"""The root conftest publishes the pytest temp root for library code.

Lives here rather than under `api/tests/` because the fixture it checks is
repo-wide: `tests/`, `api/tests/` and `q_core_mcp/tests/` all depend on
it, and covering only one package is what made the previous guard
insufficient.
"""

import os
from pathlib import Path


def test_the_temp_root_is_published_to_the_environment(tmp_path_factory):
    """Without this, deleting the publisher fails nothing.

    `api.db` falls back to `tempfile.gettempdir()`, which is correct in the
    default configuration — so the whole suite stays green while the
    guard quietly depends on a fallback that is wrong under `--basetemp`.
    Mutation-checked: removing the publisher fails this and nothing else.
    """
    published = os.environ.get("Q_CORE_TEST_TMP_ROOT")

    assert published, "the root conftest fixture did not run"
    assert Path(published).resolve() == Path(
        tmp_path_factory.getbasetemp()
    ).resolve()


def test_the_published_root_is_what_the_guard_actually_reads(tmp_path):
    """Ties the published value to the consumer, not just to the environment.

    A published variable the guard ignores would satisfy the test above
    and protect nothing.
    """
    from api.db import _test_writable_root

    assert tmp_path.resolve().is_relative_to(_test_writable_root())
