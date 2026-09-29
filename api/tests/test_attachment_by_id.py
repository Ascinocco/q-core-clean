"""Attachment content is reached by id. The server resolves the path.

review-1 wrote in 2026 that returning `file_path` "sets up a predictable
future bypass... the obvious design for a future download route is to
accept the path the API already handed back -- consulting the id-based
storage scheme not at all."

The consumer arrived, and not as a route: two tool descriptions told a
model to read "an absolute file_path you can read straight off disk". The
prediction landed in the medium nobody was watching, which is why this
closes the field rather than documenting it.
"""

from pathlib import Path
from urllib.parse import quote, unquote

import pytest

TRAVERSAL = "../../etc/passwd"


def _ticket(client, headers):
    entity = client.post(
        "/entities", json={"type": "project", "name": "P"}, headers=headers
    ).json()
    board = client.post(
        "/boards", json={"entity_id": entity["id"], "title": "B"}, headers=headers
    ).json()
    return client.post(
        "/tickets",
        json={"board_id": board["id"], "title": "T", "type": "task", "actor": "x"},
        headers=headers,
    ).json()["id"]


@pytest.fixture()
def headers(test_settings):
    return {"Authorization": f"Bearer {test_settings.api_token}"}


def test_no_attachment_response_carries_a_path(client, headers):
    ticket_id = _ticket(client, headers)

    created = client.post(
        f"/tickets/{ticket_id}/attachments",
        files={"upload": ("notes.txt", b"hello")},
        headers=headers,
    ).json()
    fetched = client.get(f"/tickets/{ticket_id}", headers=headers).json()

    assert "file_path" not in created
    assert "file_path" not in fetched["attachments"][0]


def test_content_is_served_by_id(client, headers):
    ticket_id = _ticket(client, headers)
    created = client.post(
        f"/tickets/{ticket_id}/attachments",
        files={"upload": ("notes.txt", b"the log line")},
        headers=headers,
    ).json()

    served = client.get(f"/attachments/{created['id']}/content", headers=headers)

    assert served.status_code == 200
    assert served.content == b"the log line"
    assert served.headers["content-type"].startswith("text/plain")


def test_a_percent_encoded_traversal_filename_survives_as_a_label_only(
    client, headers, test_settings
):
    """The ticket's second finding, round-tripped.

    `..%2F..%2Fetc%2Fpasswd` has no literal separators, so basename
    reduction leaves it intact — correct, because decoding it would
    silently rename the user's file. It is a DISPLAY LABEL. What must
    never happen is it reaching disk, and what must still work is reading
    the attachment back by id.
    """
    ticket_id = _ticket(client, headers)
    hostile = quote(TRAVERSAL, safe="")  # ..%2F..%2Fetc%2Fpasswd

    created = client.post(
        f"/tickets/{ticket_id}/attachments",
        files={"upload": (hostile, b"harmless")},
        headers=headers,
    ).json()

    # `_safe_filename` reduces to a basename and strips leading dots, but
    # decodes nothing -- so the label still decodes to a traversal. Pinned
    # as the exact stored string: an earlier draft of the model docstring
    # claimed this input "survives intact", and writing the assertion is
    # what showed the leading ".." does not.
    label = created["filename"]
    assert label == "%2F..%2Fetc%2Fpasswd"
    assert unquote(label) == "/../etc/passwd", "still a traversal once decoded"
    # ...and the file on disk is named from the id, not from any of it.
    import sqlite3

    connection = sqlite3.connect(test_settings.db_path)
    try:
        stored = Path(
            connection.execute(
                "SELECT file_path FROM ticket_attachments WHERE id = ?",
                (created["id"],),
            ).fetchone()[0]
        )
    finally:
        connection.close()
    # Named from the id. The bounded suffix is carried over, so part of
    # the client's string does reach the name -- what must not survive is
    # a SEPARATOR, which is what would make it more than one component.
    assert stored.name.startswith(created["id"]), stored.name
    assert "/" not in stored.name and "\\" not in stored.name
    assert stored.parent == Path(test_settings.jyra_dir).resolve() / ticket_id
    assert stored.is_file()

    # And it is still readable by id, which is the whole replacement.
    served = client.get(f"/attachments/{created['id']}/content", headers=headers)
    assert served.status_code == 200
    assert served.content == b"harmless"


def test_a_missing_attachment_is_a_404_by_id(client, headers):
    response = client.get("/attachments/no-such-id/content", headers=headers)

    assert response.status_code == 404


def test_the_route_takes_an_id_and_has_no_path_parameter(client, headers):
    """The mutation this design has to survive: "the route reads the
    client-supplied path".

    It cannot be written, and that is asserted rather than assumed. The
    signature accepts one value, an id; there is no query or body
    parameter a path could arrive in, so a caller has nothing to hand
    back. Asserted on the OpenAPI schema, which is the contract a client
    actually sees.
    """
    schema = client.get("/openapi.json", headers=headers).json()
    route = schema["paths"]["/attachments/{attachment_id}/content"]["get"]

    parameters = route.get("parameters", [])
    # Headers excluded deliberately: the auth dependency declares
    # `authorization`, and it is not a channel a path could arrive in.
    # Everything else is: a query or body value is caller-controlled.
    addressable = {p["name"] for p in parameters if p["in"] != "header"}
    assert addressable == {"attachment_id"}, addressable
    assert [p["in"] for p in parameters if p["name"] == "attachment_id"] == ["path"]
    assert "requestBody" not in route, "a body could carry a path"
