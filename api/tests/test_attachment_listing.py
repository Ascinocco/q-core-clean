"""list_attachments: size, content type, and no path (ticket T-27, ticket T-20).

The path hole here is the one worth naming. #130 dropped `file_path` from
`AttachmentResponse`, but this route never used that model -- it is
`SELECT *` through `paginate`, so the column flowed straight back out. A
test in #130 called "no attachment response carries a path" asserted it
of the POST and of get_ticket's embed and did not enumerate this one, so
its name was broader than its coverage.

It mattered because this route is what `list_attachments` exposes over
MCP: every attachment's absolute path, into a model's context, which is
exactly the bypass D107 closed and exactly the medium it was found in.
"""

from pathlib import Path

import pytest


@pytest.fixture()
def headers(test_settings):
    return {"Authorization": f"Bearer {test_settings.api_token}"}


@pytest.fixture()
def ticket_id(client, headers):
    entity = client.post(
        "/entities", json={"type": "project", "name": "p"}, headers=headers
    ).json()
    board = client.post(
        "/boards", json={"entity_id": entity["id"], "title": "B"}, headers=headers
    ).json()
    return client.post(
        "/tickets",
        json={"board_id": board["id"], "title": "T", "type": "task", "actor": "x"},
        headers=headers,
    ).json()["id"]


def _attach(client, headers, ticket_id, name, payload, content_type=None):
    files = {"upload": (name, payload, content_type) if content_type else (name, payload)}
    return client.post(
        f"/tickets/{ticket_id}/attachments", files=files, headers=headers
    ).json()


def test_no_response_that_carries_attachments_carries_a_path(
    client, headers, ticket_id
):
    """Enumerated, not sampled.

    Every response shape that can carry an attachment is listed here and
    checked, because the previous version of this assertion named three
    surfaces and covered two.
    """
    created = _attach(client, headers, ticket_id, "a.png", b"x", "image/png")

    surfaces = {
        "POST /tickets/{id}/attachments": [created],
        "GET /tickets/{id} (embed)": client.get(
            f"/tickets/{ticket_id}", headers=headers
        ).json()["attachments"],
        "GET /tickets/{id}/attachments": client.get(
            f"/tickets/{ticket_id}/attachments", headers=headers
        ).json()["items"],
    }

    for surface, rows in surfaces.items():
        assert rows, f"{surface} returned nothing -- the check would be vacuous"
        for row in rows:
            assert "file_path" not in row, f"{surface} leaks the stored path"


def test_size_and_content_type_come_back_on_every_surface(
    client, headers, ticket_id
):
    created = _attach(client, headers, ticket_id, "shot.png", b"12345", "image/png")
    listed = client.get(f"/tickets/{ticket_id}/attachments", headers=headers).json()

    assert created["size_bytes"] == 5
    assert created["content_type"] == "image/png"
    assert listed["items"][0]["size_bytes"] == 5
    assert listed["items"][0]["content_type"] == "image/png"


def test_the_list_and_the_content_route_agree_about_the_type(
    client, headers, ticket_id
):
    """The constraint team-lead set: a list and a fetch of the same
    attachment cannot disagree about what it is.

    Both must come from one derivation. Read from BOTH surfaces here
    rather than asserting each against a literal, because two literals
    that happen to match today is what drift looks like before it drifts.
    """
    for name, payload in (
        ("a.png", b"x"),
        ("notes.md", b"# hi"),
        ("run.log", b"line"),
        ("data.json", b"{}"),
        ("odd.xyz", b"zz"),
    ):
        created = _attach(client, headers, ticket_id, name, payload)

        listed = client.get(
            f"/tickets/{ticket_id}/attachments", headers=headers
        ).json()["items"]
        row = next(item for item in listed if item["id"] == created["id"])
        served = client.get(
            f"/attachments/{created['id']}/content", headers=headers
        )

        assert served.headers["content-type"].split(";")[0] == row["content_type"], name


def test_size_is_measured_from_the_stored_bytes(client, headers, ticket_id):
    """Not from a client-supplied length, and not recomputed per list.

    Deliberately not a run of digits: `b"0123456789" * 7` was the obvious
    70-byte payload and the upload refused it, correctly -- it is a
    70-digit run, which is account-shaped. The first draft of this test
    read that refusal as a missing field.
    """
    payload = b"abcdefghij" * 7
    created = _attach(client, headers, ticket_id, "a.txt", payload)

    assert created["size_bytes"] == 70


def test_an_unknown_suffix_is_stored_without_one(
    client, headers, ticket_id, test_settings
):
    """Ticket T-20, ruled for the allow-list.

    A suffix outside the allow-list is not carried to disk at all, so no
    byte of the client's string reaches the filesystem and the media type
    is a total function of a closed set.
    """
    import sqlite3

    created = _attach(client, headers, ticket_id, "..%2F..%2Fetc%2Fpasswd", b"x")

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

    assert stored.name == created["id"], stored.name
    assert created["content_type"] == "application/octet-stream"
    # The label is still kept for display -- it is only the DISK name that
    # the allow-list governs.
    assert created["filename"] == "%2F..%2Fetc%2Fpasswd"
