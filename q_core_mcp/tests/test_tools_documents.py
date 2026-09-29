"""Tests for the document CRUD tools.

Bodies are asserted on the wire rather than inferred from a 200: a tool
that silently drops `entity_id` still returns a valid-looking record, and
the round trip would pass while the link was never made.
"""

import json

import httpx
import pytest

from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server


def _server(test_settings, handler):
    client = QCoreClient(test_settings, transport=httpx.MockTransport(handler))
    return build_server(client)


def _recorder(seen: dict, payload: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        raw = request.read()
        seen["body"] = json.loads(raw) if raw else None
        return httpx.Response(200, json=payload if payload is not None else {"id": "d1"})

    return handler


def test_register_document_posts_the_body(test_settings, call_tool):
    seen = {}

    call_tool(
        _server(test_settings, _recorder(seen)),
        "register_document",
        {"path": "scan.pdf", "title": "April statement", "doc_type": "statement"},
    )

    assert seen["method"] == "POST"
    assert seen["path"] == "/documents"
    assert seen["body"] == {
        "file_path": "scan.pdf",
        "title": "April statement",
        "doc_type": "statement",
    }


def test_register_document_omits_absent_optional_fields(test_settings, call_tool):
    """Omitted, not sent as null. The API forbids unknown keys, and an
    explicit null is a different statement from "not supplied"."""
    seen = {}

    call_tool(
        _server(test_settings, _recorder(seen)),
        "register_document",
        {"path": "scan.pdf", "title": "Receipt"},
    )

    assert seen["body"] == {"file_path": "scan.pdf", "title": "Receipt"}
    assert "doc_type" not in seen["body"]
    assert "entity_id" not in seen["body"]


def test_register_document_passes_the_entity_link(test_settings, call_tool):
    seen = {}

    call_tool(
        _server(test_settings, _recorder(seen)),
        "register_document",
        {"path": "s.pdf", "title": "Policy", "entity_id": "ent-1"},
    )

    assert seen["body"]["entity_id"] == "ent-1"


def test_get_document_uses_the_id_in_the_path(test_settings, call_tool):
    seen = {}

    call_tool(_server(test_settings, _recorder(seen)), "get_document", {"document_id": "abc"})

    assert seen["method"] == "GET"
    assert seen["path"] == "/documents/abc"


def test_list_documents_passes_its_filters(test_settings, call_tool):
    seen = {}

    call_tool(
        _server(test_settings, _recorder(seen, {"items": [], "total": 0})),
        "list_documents",
        {"entity_id": "ent-1", "doc_type": "receipt", "limit": 10},
    )

    assert seen["path"] == "/documents"
    assert seen["params"]["entity_id"] == "ent-1"
    assert seen["params"]["doc_type"] == "receipt"
    assert seen["params"]["limit"] == "10"


def test_list_documents_omits_absent_filters(test_settings, call_tool):
    seen = {}

    call_tool(
        _server(test_settings, _recorder(seen, {"items": [], "total": 0})),
        "list_documents",
        {},
    )

    assert "entity_id" not in seen["params"]
    assert "doc_type" not in seen["params"]


def test_delete_document_issues_a_delete(test_settings, call_tool):
    seen = {}

    call_tool(
        _server(test_settings, _recorder(seen, {"deleted": True})),
        "delete_document",
        {"document_id": "abc"},
    )

    assert seen["method"] == "DELETE"
    assert seen["path"] == "/documents/abc"


# --- descriptions ----------------------------------------------------------


@pytest.fixture()
def descriptions(test_settings, tools_by_name):
    return {
        name: tool.description
        for name, tool in tools_by_name(
            build_server(QCoreClient(test_settings, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
        ).items()
    }


def test_register_document_description_states_move_versus_copy(descriptions):
    """A model choosing this tool has to know that registering from inbox/
    destroys the source and registering from intake/ does not. That is not
    inferable from the name, and getting it wrong is irreversible."""
    text = descriptions["register_document"]

    assert "MOVED" in text
    assert "COPIED" in text
    assert "intake/" in text


def test_register_document_description_states_the_dedup_behaviour(descriptions):
    text = descriptions["register_document"]

    assert "moves nothing" in text


def test_delete_document_description_says_it_deletes_the_file(descriptions):
    text = descriptions["delete_document"]

    assert "file" in text
    assert "no undo" in text.lower() or "permanently" in text.lower()


# --- round trip against the real app ---------------------------------------


def test_personal_redaction_round_trip_through_real_api(test_settings, call_tool, tmp_path):
    from api.config import get_settings
    from api.main import app

    intake = tmp_path / 'intake'
    intake.mkdir()
    (intake / 'statement.csv').write_text(
        'Posted,Kind,Payee,Note,Value\n'
        '06/15/2026,DEBIT,CAFE,"Alex Example, 123 Example Lane",-19.99\n'
    )
    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        client = QCoreClient(test_settings, transport=httpx.ASGITransport(app=app))
        server = build_server(client)
        result = call_tool(server, 'extract_document_text', {'path': 'statement.csv'})
    finally:
        app.dependency_overrides.clear()
    assert 'Alex Example' not in json.dumps(result)
    assert '123 Example Lane' not in json.dumps(result)
    assert 'CAFE' in result['text']
    assert '-19.99' in result['text']


def test_documents_round_trip_through_the_real_api(test_settings, call_tool, tmp_path):
    """The stubbed tests above pin what the tools send; this pins that the
    real API accepts it and that a file actually moves."""
    from api.config import get_settings
    from api.main import app

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    source = inbox / "scan.pdf"
    source.write_bytes(b"%PDF round trip")
    settings = test_settings.model_copy(
        update={
            "inbox_dir": str(inbox),
            "documents_dir": str(tmp_path / "documents"),
        }
    )

    app.dependency_overrides[get_settings] = lambda: settings
    try:
        client = QCoreClient(test_settings, transport=httpx.ASGITransport(app=app))
        server = build_server(client)

        created = call_tool(
            server, "register_document", {"path": "scan.pdf", "title": "Round Trip"}
        )
        stored = next((tmp_path / "documents").iterdir())
        fetched = call_tool(server, "get_document", {"document_id": created["id"]})
        listed = call_tool(server, "list_documents", {})
        deleted = call_tool(server, "delete_document", {"document_id": created["id"]})
    finally:
        app.dependency_overrides.clear()

    assert not source.exists(), "an inbox source is moved, so it should be gone"
    assert fetched["id"] == created["id"]
    assert listed["total"] == 1
    assert deleted == {"deleted": True}
    assert "file_path" not in created
    assert not stored.exists(), "delete removes the file too"


def test_extract_registered_document_uses_id_and_omits_false_default(
    test_settings, call_tool
):
    seen = {}

    call_tool(
        _server(test_settings, _recorder(seen, {"text": "safe"})),
        "extract_registered_document_text",
        {"document_id": "doc-1"},
    )

    assert seen["method"] == "POST"
    assert seen["path"] == "/documents/doc-1/extract"
    assert seen["body"] == {}


def test_extract_document_text_omits_allow_partial_by_default(
    test_settings, call_tool
):
    """The default lives in the API, and only there.

    Sending `allow_partial: false` explicitly would restate the default in
    a second place, and two places holding one default is how they come to
    disagree. Omitted means "no opinion", so the endpoint's fail-closed
    behaviour governs — which is the point of the ticket.
    """
    seen = {}

    call_tool(
        _server(test_settings, _recorder(seen, {"text": "x", "pages": 1})),
        "extract_document_text",
        {"path": "statement.pdf"},
    )

    assert seen["body"] == {"path": "statement.pdf"}
    assert "allow_partial" not in seen["body"]


def test_extract_document_text_sends_allow_partial_when_asked(
    test_settings, call_tool
):
    """Asserted on the wire: a tool that accepted the argument and dropped
    it would still return a valid-looking record, and the caller would
    believe they had opted in."""
    seen = {}

    call_tool(
        _server(test_settings, _recorder(seen, {"text": "x", "pages": 2})),
        "extract_document_text",
        {"path": "statement.pdf", "allow_partial": True},
    )

    assert seen["body"] == {"path": "statement.pdf", "allow_partial": True}


def test_extract_description_states_the_partial_default_and_the_opt_in(
    test_settings, tools_by_name
):
    """A refusal a caller cannot anticipate reads as a malfunction.

    The model reads this description before it reads any runbook, so the
    description is where "some pages extracted, so it was refused" has to
    be predictable rather than surprising.
    """
    tools = tools_by_name(
        _server(test_settings, lambda r: httpx.Response(200, json={}))
    )
    description = tools["extract_document_text"].description.lower()

    assert "allow_partial" in description
    assert "partial" in description
    assert "statement" in description, "the case where opting in is wrong"


def test_extract_description_names_inbox_as_a_permitted_root(
    test_settings, tools_by_name
):
    """Stale since #81 made inbox/ extractable.

    The description told callers to pass paths inside intake/ or
    data/documents/, so a model following it would not attempt the inbox
    file it had just been asked to file — the description was the
    remaining half of the defect #81 fixed in the endpoint.
    """
    tools = tools_by_name(
        _server(test_settings, lambda r: httpx.Response(200, json={}))
    )

    assert "inbox" in tools["extract_document_text"].description.lower()
