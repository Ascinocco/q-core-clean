"""The archive tools, and the warning the model has to see.

A description test rather than a wire test alone, because the danger here
is not that the call fails — it is that the model archives a statement,
re-imports the corrected file, and never mentions that a page of hand
categorisation was silently dropped on the way. The model cannot pass on
a consequence nobody told it about.
"""

import httpx

from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server


def _server(test_settings, handler):
    return build_server(
        QCoreClient(test_settings, transport=httpx.MockTransport(handler))
    )


def _recorder(seen: dict, payload: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json=payload or {"id": "s1", "archived": True})

    return handler


def test_archive_statement_posts_to_the_nested_path(test_settings, call_tool):
    seen = {}

    call_tool(
        _server(test_settings, _recorder(seen)), "archive_statement",
        {"statement_id": "s1"},
    )

    assert seen["method"] == "POST"
    assert seen["path"] == "/statements/s1/archive"


def test_unarchive_statement_posts_to_its_own_path(test_settings, call_tool):
    seen = {}

    call_tool(
        _server(test_settings, _recorder(seen)), "unarchive_statement",
        {"statement_id": "s1"},
    )

    assert seen["path"] == "/statements/s1/unarchive"


def test_archive_transaction_posts_to_the_nested_path(test_settings, call_tool):
    seen = {}

    call_tool(
        _server(test_settings, _recorder(seen)), "archive_transaction",
        {"transaction_id": "t1"},
    )

    assert seen["path"] == "/transactions/t1/archive"


def test_archive_descriptions_state_that_totals_change(test_settings, tools_by_name):
    """The property the ticket calls easy to get wrong.

    If the description does not say archiving changes every total, a model
    reading only the tool surface has no reason to expect a summary to
    move, and will report the change as having done nothing visible.
    """
    tools = tools_by_name(
        _server(test_settings, lambda r: httpx.Response(200, json={}))
    )

    for name in ("archive_statement", "archive_transaction"):
        description = tools[name].description.lower()
        assert "total" in description, name
        assert "de-duplication" in description or "dedup" in description, name
        assert "nothing is deleted" in description, name


def test_archive_statement_warns_about_losing_hand_edits(test_settings, tools_by_name):
    """The silent cost, which is the only one that needs a warning.

    Archiving loses nothing. Re-importing afterwards loses the hand
    categorisation, and reports no error while doing it — so the warning
    has to arrive before the re-import, in the description of the call
    that precedes it.
    """
    tools = tools_by_name(
        _server(test_settings, lambda r: httpx.Response(200, json={}))
    )
    description = tools["archive_statement"].description.lower()

    assert "hand_edited" in description
    assert "by hand" in description
    assert "does not" in description, "must say the work is NOT carried over"


def test_the_reversals_are_not_marked_destructive(test_settings, tools_by_name):
    """Putting rows back into a total takes nothing away.

    Marking them destructive would train a reader to ignore the hint on
    the two calls where it means something.
    """
    tools = tools_by_name(
        _server(test_settings, lambda r: httpx.Response(200, json={}))
    )

    for name in ("unarchive_statement", "unarchive_transaction"):
        annotations = tools[name].annotations
        assert not (annotations and annotations.destructive_hint), name
