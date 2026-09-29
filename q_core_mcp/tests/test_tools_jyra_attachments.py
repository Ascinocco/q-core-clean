"""Jyra attachment tools: attach_file, delete_attachment.

Deferred from the first pass until the client could send multipart at all
(#43). Paired deliberately: a surface that deletes attachments but cannot
create them is incoherent.
"""

import pathlib

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server


def _stored_path(test_settings, attachment_id):
    """The on-disk path, read from the row as the server reads it.

    The response no longer carries `file_path` (ticket T-44): a path handed
    to a caller is a path a caller hands back. These tests still assert
    what landed on disk, so they look it up rather than being handed it.
    """
    import sqlite3

    connection = sqlite3.connect(test_settings.db_path)
    try:
        row = connection.execute(
            "SELECT file_path FROM ticket_attachments WHERE id = ?",
            (attachment_id,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None, f"no attachment row for {attachment_id}"
    return pathlib.Path(row[0])


def _server(test_settings, handler):
    return build_server(
        QCoreClient(test_settings, transport=httpx.MockTransport(handler))
    )


def _real_server(test_settings, app):
    return build_server(
        QCoreClient(test_settings, transport=httpx.ASGITransport(app=app))
    )


def _ticket(server, call_tool):
    entity = call_tool(
        server, "create_entity", {"entity_type": "project", "name": "proj"}
    )
    board = call_tool(
        server, "create_board", {"entity_id": entity["id"], "title": "D"}
    )
    return call_tool(
        server,
        "create_ticket",
        {
            "board_id": board["id"],
            "ticket_type": "task",
            "title": "t",
            "actor": "owner",
        },
    )


# --- shape -------------------------------------------------------------


def test_attach_file_sends_multipart_with_the_file_name(
    test_settings, call_tool, tmp_path
):
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = request.content
        return httpx.Response(200, json={"id": "a1"})

    source = tmp_path / "screenshot.png"
    source.write_bytes(b"png-bytes")

    call_tool(
        _server(test_settings, handler),
        "attach_file",
        {"ticket_id": "t1", "path": str(source)},
    )

    assert (seen["method"], seen["path"]) == ("POST", "/tickets/t1/attachments")
    assert seen["content_type"].startswith("multipart/form-data")
    assert b"screenshot.png" in seen["body"]
    assert b"png-bytes" in seen["body"]


def test_delete_attachment_deletes(test_settings, call_tool):
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"deleted": True})

    call_tool(
        _server(test_settings, handler), "delete_attachment", {"attachment_id": "a1"}
    )
    assert (seen["method"], seen["path"]) == ("DELETE", "/attachments/a1")


def test_attach_file_reports_a_missing_local_file_clearly(
    test_settings, call_tool, tmp_path
):
    """The path comes from a model, so a wrong one is likely. Failing before
    the request with a message naming the path beats a confusing API error
    — or worse, an unhandled OSError, which would reach the model as
    "Error executing tool" with the real cause dropped."""
    missing = tmp_path / "nope.png"

    with pytest.raises(ToolError) as excinfo:
        call_tool(
            _server(test_settings, lambda r: httpx.Response(200, json={})),
            "attach_file",
            {"ticket_id": "t1", "path": str(missing)},
        )

    assert str(missing) in str(excinfo.value)


def test_attach_file_refuses_a_directory(test_settings, call_tool, tmp_path):
    """read_bytes on a directory raises IsADirectoryError, which is not a
    ToolError and so would lose its message."""
    with pytest.raises(ToolError) as excinfo:
        call_tool(
            _server(test_settings, lambda r: httpx.Response(200, json={})),
            "attach_file",
            {"ticket_id": "t1", "path": str(tmp_path)},
        )

    assert "file" in str(excinfo.value).lower()


# --- annotations and descriptions --------------------------------------


def test_delete_attachment_is_destructive_and_attach_is_not(
    test_settings, tools_by_name
):
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))
    assert tools["delete_attachment"].annotations.destructive_hint
    attach = tools["attach_file"].annotations
    assert not (attach and attach.destructive_hint)


def test_attach_file_description_says_it_takes_a_local_path(
    test_settings, tools_by_name
):
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))
    description = tools["attach_file"].description.lower()
    assert "path" in description
    # The returned path is the point: an agent opens it directly.
    assert "absolute" in description or "read" in description


def test_attach_file_description_carries_the_filename_consumer_rule(
    test_settings, tools_by_name
):
    """The stored filename is a display name and never a path component. It
    is kept exactly as sent, so a percent-encoded separator survives
    literally — harmless here because the on-disk name is a UUID, but a
    consumer that decodes it and joins it onto a path rebuilds a traversal."""
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))
    description = tools["attach_file"].description.lower()
    assert "display" in description
    assert "never" in description


# --- round trips -------------------------------------------------------


def test_round_trip_attach_then_read_the_file_off_disk(
    test_settings, call_tool, tmp_path
):
    """The whole point of the feature: the API hands back a path an agent
    can open with no fetch step."""
    from api.config import get_settings
    from api.main import app

    source = tmp_path / "shot.png"
    source.write_bytes(b"screenshot-bytes")

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        ticket = _ticket(server, call_tool)
        attachment = call_tool(
            server,
            "attach_file",
            {"ticket_id": ticket["id"], "path": str(source)},
        )
        # get_ticket embeds attachments now (a todo), so this is the
        # one call an agent makes rather than two.
        detail = call_tool(server, "get_ticket", {"ticket_id": ticket["id"]})
    finally:
        app.dependency_overrides.clear()

    assert attachment["filename"] == "shot.png"
    # Copied, not moved. Pinned here as well as in the delete round trip:
    # it was only asserted there, so a tool that moved the file would have
    # been caught by a test about deletion, whose failure message would
    # have pointed at the wrong thing entirely.
    assert source.read_bytes() == b"screenshot-bytes"
    assert "file_path" not in attachment, "content is reached by id"
    written = _stored_path(test_settings, attachment["id"])
    assert written.is_absolute()
    assert written.read_bytes() == b"screenshot-bytes"
    assert written.is_relative_to(pathlib.Path(test_settings.jyra_dir))
    assert [a["id"] for a in detail["attachments"]] == [attachment["id"]]


def test_round_trip_a_hostile_filename_stays_inside_jyra_dir(
    test_settings, call_tool, tmp_path
):
    """The API sanitises, and this proves the tool does not defeat it by
    sending something else — the filename it sends is whatever the local
    file is called, so a hostile local name must still land safely."""
    from api.config import get_settings
    from api.main import app

    hostile = tmp_path / "..evil.png"
    hostile.write_bytes(b"x")

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        ticket = _ticket(server, call_tool)
        attachment = call_tool(
            server, "attach_file", {"ticket_id": ticket["id"], "path": str(hostile)}
        )
    finally:
        app.dependency_overrides.clear()

    written = _stored_path(test_settings, attachment["id"]).resolve()
    assert written.is_relative_to(pathlib.Path(test_settings.jyra_dir).resolve())
    assert "/" not in attachment["filename"]


def test_round_trip_delete_attachment_removes_the_file(
    test_settings, call_tool, tmp_path
):
    from api.config import get_settings
    from api.main import app

    source = tmp_path / "a.png"
    source.write_bytes(b"a")

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        ticket = _ticket(server, call_tool)
        attachment = call_tool(
            server, "attach_file", {"ticket_id": ticket["id"], "path": str(source)}
        )
        written = _stored_path(test_settings, attachment["id"])
        assert written.exists()

        call_tool(server, "delete_attachment", {"attachment_id": attachment["id"]})
        detail = call_tool(server, "get_ticket", {"ticket_id": ticket["id"]})
    finally:
        app.dependency_overrides.clear()

    assert not written.exists()
    assert detail["attachments"] == []
    # The local file the caller attached is untouched — the tool copies.
    assert source.read_bytes() == b"a"


# --- read_attachment: content by id (ticket T-44) -------------------------


def _attached(test_settings, call_tool, tmp_path, name, payload):
    """Attach a real file through the real API and hand back (server, id)."""
    from api.config import get_settings
    from api.main import app

    source = tmp_path / name
    source.write_bytes(payload)

    app.dependency_overrides[get_settings] = lambda: test_settings
    server = _real_server(test_settings, app)
    ticket = _ticket(server, call_tool)
    attachment = call_tool(
        server, "attach_file", {"ticket_id": ticket["id"], "path": str(source)}
    )
    return server, attachment["id"]


def test_read_attachment_returns_the_text_by_id(test_settings, call_tool, tmp_path):
    """The capability that replaced the path.

    Two tool descriptions used to tell a model to read `file_path` off
    disk. This is what they say instead, so it has to actually work.
    """
    try:
        server, attachment_id = _attached(
            test_settings, call_tool, tmp_path, "notes.txt", b"line one\nline two\n"
        )
        result = call_tool(server, "read_attachment", {"attachment_id": attachment_id})
    finally:
        from api.main import app

        app.dependency_overrides.clear()

    assert result["text"] == "line one\nline two\n"
    assert result["attachment_id"] == attachment_id
    assert result["bytes"] == 18
    assert result["media_type"] == "text/plain"


def test_read_attachment_refuses_bytes_that_are_not_utf8(
    test_settings, call_tool, tmp_path
):
    """Strict decode then refuse, rather than a lossy fallback.

    `errors="replace"` would hand a model a screenful of U+FFFD and let it
    reason about a PNG as if it were text. The refusal names the media
    type and the size so the caller learns what it actually has.
    """
    try:
        server, attachment_id = _attached(
            test_settings, call_tool, tmp_path, "shot.png", b"\x89PNG\r\n\x1a\n\xff\xfe"
        )
        with pytest.raises(ToolError) as raised:
            call_tool(server, "read_attachment", {"attachment_id": attachment_id})
    finally:
        from api.main import app

        app.dependency_overrides.clear()

    message = str(raised.value)
    assert "image/png" in message
    assert "10 bytes" in message


def test_the_refusal_does_not_leak_the_server_side_path(
    test_settings, call_tool, tmp_path
):
    """The refusal is the one place a path could escape by accident.

    Dropping `file_path` from the response models is pointless if the
    error raised on the way back quotes the absolute path instead. Both
    halves are asserted: the storage root, and the generated on-disk name.
    """
    try:
        server, attachment_id = _attached(
            test_settings, call_tool, tmp_path, "shot.png", b"\x89PNG\r\n\x1a\n\xff\xfe"
        )
        with pytest.raises(ToolError) as raised:
            call_tool(server, "read_attachment", {"attachment_id": attachment_id})
        on_disk = _stored_path(test_settings, attachment_id)
    finally:
        from api.main import app

        app.dependency_overrides.clear()

    message = str(raised.value)
    assert "data/" not in message
    assert on_disk.name not in message
    assert str(on_disk) not in message
    # The id is not a leak -- it is the handle the caller already holds,
    # and naming it is what makes the refusal actionable.
    assert attachment_id in message


# --- the redaction rule, closed at both ends ---------------------------
#
# CLAUDE.md: account numbers are "never sent to any LLM and never stored
# in the DB", redacted "before any model reads the document".
# read_attachment is the tool that puts stored file content into a
# model's context, so the rule governs it directly.
#
# Every digit run below is invented. No real statement is read here.

FAKE_RUN = "412345678901"


def test_upload_checks_a_log_file_although_its_suffix_is_not_text(
    test_settings, call_tool, tmp_path
):
    """`.log` is not in TEXT_SUFFIXES, and was therefore stored unchecked.

    The suffix is a CLAIM by the caller; whether the bytes are text is a
    property of the bytes. Checking the claim let an account number
    through under any suffix not on the list -- and read_attachment would
    then decode and return exactly that text to a model.
    """
    try:
        with pytest.raises(ToolError) as raised:
            _attached(
                test_settings,
                call_tool,
                tmp_path,
                "run.log",
                f"connected\nacct {FAKE_RUN}\ndone\n".encode(),
            )
    finally:
        from api.main import app

        app.dependency_overrides.clear()

    message = str(raised.value)
    assert "register_document" in message, "the refusal needs a right answer"
    assert FAKE_RUN not in message, "an error body is model context too"


def test_a_clean_log_file_is_still_accepted(test_settings, call_tool, tmp_path):
    """The other direction, so the check above is not passing by refusing
    everything with a .log suffix -- which would look identical."""
    try:
        server, attachment_id = _attached(
            test_settings, call_tool, tmp_path, "clean.log", b"started\nfinished\n"
        )
        result = call_tool(server, "read_attachment", {"attachment_id": attachment_id})
    finally:
        from api.main import app

        app.dependency_overrides.clear()

    assert result["text"] == "started\nfinished\n"


def test_read_attachment_refuses_a_file_stored_before_the_upload_check(
    test_settings, call_tool, tmp_path
):
    """The belt for files that predate the braces.

    The upload check cannot have examined anything stored before it
    existed, and read_attachment is the tool that would hand one to a
    model. Written straight to disk and to the row, deliberately
    bypassing upload -- going through upload would prove nothing, since
    upload now refuses it.
    """
    import sqlite3
    import uuid

    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        ticket = _ticket(server, call_tool)

        attachment_id = str(uuid.uuid4())
        directory = pathlib.Path(test_settings.jyra_dir) / ticket["id"]
        directory.mkdir(parents=True, exist_ok=True)
        stored = directory / f"{attachment_id}.log"
        stored.write_text(f"legacy line\nacct {FAKE_RUN}\n")

        connection = sqlite3.connect(test_settings.db_path)
        try:
            connection.execute(
                "INSERT INTO ticket_attachments "
                "(id, ticket_id, filename, file_path, content_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (attachment_id, ticket["id"], "legacy.log", str(stored), "x" * 64),
            )
            connection.commit()
        finally:
            connection.close()

        with pytest.raises(ToolError) as raised:
            call_tool(server, "read_attachment", {"attachment_id": attachment_id})
    finally:
        app.dependency_overrides.clear()

    message = str(raised.value)
    # Names the reason...
    assert "account-shaped" in message
    assert attachment_id in message
    # ...without echoing what it found. An error body is model context
    # exactly like a successful response.
    assert FAKE_RUN not in message
    assert "412345" not in message


# --- list_attachments (ticket T-27) ---------------------------------------


def test_list_attachments_returns_ids_size_and_type_and_no_path(
    test_settings, call_tool, tmp_path
):
    """The read that makes the other two tools usable.

    `delete_attachment` and `read_attachment` both take an
    attachment_id, and before this there was no tool that produced one
    except the upload's own response -- so from any later session they
    were unreachable.

    The absent path is asserted, not assumed: this is the route that
    would have put every attachment's absolute path into a model's
    context, which is the medium D107's bypass was actually found in.
    """
    from api.main import app

    try:
        server, first_id = _attached(
            test_settings, call_tool, tmp_path, "one.png", b"12345"
        )
        ticket_id = call_tool(server, "list_tickets", {})["items"][0]["id"]
        call_tool(
            server,
            "attach_file",
            {"ticket_id": ticket_id, "path": str(_write(tmp_path, "two.md", b"# hi"))},
        )
        listed = call_tool(server, "list_attachments", {"ticket_id": ticket_id})
    finally:
        app.dependency_overrides.clear()

    assert listed["total"] == 2
    names = {item["filename"] for item in listed["items"]}
    assert names == {"one.png", "two.md"}

    by_name = {item["filename"]: item for item in listed["items"]}
    assert by_name["one.png"]["size_bytes"] == 5
    assert by_name["one.png"]["content_type"] == "image/png"
    assert by_name["two.md"]["content_type"] == "text/markdown"

    for item in listed["items"]:
        assert "file_path" not in item, "a listing must not hand back a path"
        # The id is the point of this tool -- it is what the other two take.
        assert item["id"]
    assert first_id in {item["id"] for item in listed["items"]}


def _write(directory, name, payload):
    path = directory / name
    path.write_bytes(payload)
    return path
