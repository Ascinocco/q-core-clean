from pathlib import Path

import pytest


@pytest.fixture()
def headers(test_settings):
    return {"Authorization": f"Bearer {test_settings.api_token}"}


@pytest.fixture()
def ticket_id(client, headers):
    entity = client.post(
        "/entities", json={"type": "project", "name": "proj"}, headers=headers
    ).json()
    board = client.post(
        "/boards",
        json={"entity_id": entity["id"], "title": "Delivery"},
        headers=headers,
    ).json()
    return client.post(
        "/tickets",
        json={"board_id": board["id"], "type": "task", "title": "t", "actor": "owner"},
        headers=headers,
    ).json()["id"]


def _stored_path(test_settings, attachment_id: str) -> Path:
    """The on-disk path for an attachment, read from the DB.

    The response no longer carries `file_path` — that is the point of
    ticket T-44: a path handed to a caller is a path a caller hands back.
    These tests still need the real path to assert what landed on disk,
    so they read it the way the SERVER does, from the row, rather than
    from a field the API deliberately stopped publishing.
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
    return Path(row[0])


def test_upload_returns_an_absolute_readable_path(client, headers, ticket_id, test_settings):
    response = client.post(
        f"/tickets/{ticket_id}/attachments",
        files={"upload": ("shot.png", b"fake-png-bytes", "image/png")},
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["filename"] == "shot.png"
    path = _stored_path(test_settings, body["id"])
    assert path.is_absolute()
    assert path.read_bytes() == b"fake-png-bytes"


def test_upload_is_stored_under_jyra_dir(client, headers, ticket_id, test_settings):
    body = client.post(
        f"/tickets/{ticket_id}/attachments",
        files={"upload": ("shot.png", b"x", "image/png")},
        headers=headers,
    ).json()
    assert _stored_path(test_settings, body["id"]).is_relative_to(
        Path(test_settings.jyra_dir)
    )


def test_path_traversal_filename_stays_inside_jyra_dir(
    client, headers, ticket_id, test_settings
):
    body = client.post(
        f"/tickets/{ticket_id}/attachments",
        files={"upload": ("../../../etc/passwd", b"x", "text/plain")},
        headers=headers,
    ).json()
    assert body["filename"] == "passwd"
    assert _stored_path(test_settings, body["id"]).is_relative_to(
        Path(test_settings.jyra_dir)
    )


def test_list_attachments(client, headers, ticket_id):
    client.post(
        f"/tickets/{ticket_id}/attachments",
        files={"upload": ("a.png", b"a", "image/png")},
        headers=headers,
    )
    client.post(
        f"/tickets/{ticket_id}/attachments",
        files={"upload": ("b.png", b"b", "image/png")},
        headers=headers,
    )
    response = client.get(f"/tickets/{ticket_id}/attachments", headers=headers)
    assert response.json()["total"] == 2


def test_delete_attachment_removes_row_and_file(client, headers, ticket_id, test_settings):
    body = client.post(
        f"/tickets/{ticket_id}/attachments",
        files={"upload": ("a.png", b"a", "image/png")},
        headers=headers,
    ).json()
    path = _stored_path(test_settings, body["id"])
    response = client.delete(f"/attachments/{body['id']}", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"deleted": True}
    assert not path.exists()
    assert (
        client.get(f"/tickets/{ticket_id}/attachments", headers=headers).json()["total"]
        == 0
    )


def test_upload_to_unknown_ticket_is_404(client, headers):
    response = client.post(
        "/tickets/nope/attachments",
        files={"upload": ("a.png", b"a", "image/png")},
        headers=headers,
    )
    assert response.status_code == 404


# --- beyond the plan: adversarial filenames ----------------------------

# The plan tests one traversal shape. This is the first endpoint in q-core
# that lets a caller influence a filesystem path, so the guard is worth
# probing from several directions rather than one.
ADVERSARIAL_FILENAMES = [
    "../../../etc/passwd",           # classic relative traversal
    "....//....//etc/passwd",        # doubled-up, survives a naive ".." strip
    "/etc/passwd",                   # absolute path
    "/../../etc/shadow",             # absolute plus traversal
    "..",                            # bare parent
    "...",                           # all dots
    ".",                             # bare current
    "",                              # empty
    "   ",                           # whitespace only
    ".bashrc",                       # leading-dot hidden file
    "C:\\Windows\\system32\\evil.dll",  # Windows separators
    "..\\..\\..\\windows\\win.ini",     # Windows traversal
    "%2e%2e%2fetc%2fpasswd",         # percent-encoded (must NOT be decoded)
    "a/b/c/d.png",                   # nested relative
    "~/.ssh/authorized_keys",        # home-relative
    "shot.png\x00.txt",              # embedded null byte
]


@pytest.mark.parametrize("raw_name", ADVERSARIAL_FILENAMES)
def test_adversarial_filenames_never_escape_jyra_dir(
    client, headers, ticket_id, test_settings, raw_name
):
    """Whatever the client calls the file, the bytes must land under
    jyra_dir/<ticket_id>/ and nowhere else.

    Asserts on the resolved path, so a symlink or `..` that survived
    sanitisation would still be caught.
    """
    response = client.post(
        f"/tickets/{ticket_id}/attachments",
        files={"upload": (raw_name, b"payload", "application/octet-stream")},
        headers=headers,
    )

    # Rejecting is acceptable; escaping is not.
    if response.status_code != 200:
        assert response.status_code in (400, 422), response.status_code
        return

    body = response.json()
    root = Path(test_settings.jyra_dir).resolve()
    written = _stored_path(test_settings, body["id"]).resolve()
    assert written.is_relative_to(root), f"{raw_name!r} escaped to {written}"
    assert written.parent == root / ticket_id
    assert written.read_bytes() == b"payload"
    # The stored display name must not carry separators onward to any client
    # that later joins it onto a path of its own.
    assert "/" not in body["filename"]
    assert "\\" not in body["filename"]
    assert body["filename"] not in ("", ".", "..")


def test_nothing_was_written_outside_jyra_dir(client, headers, ticket_id, test_settings):
    """Belt and braces for the parametrised set: after uploading every hostile
    name, the only files anywhere under the temp root are inside jyra_dir."""
    jyra_root = Path(test_settings.jyra_dir).resolve()
    sandbox = jyra_root.parent  # tmp_path — also holds the db and documents dir

    before = {p for p in sandbox.rglob("*") if p.is_file()}
    for raw_name in ADVERSARIAL_FILENAMES:
        client.post(
            f"/tickets/{ticket_id}/attachments",
            files={"upload": (raw_name, b"x", "application/octet-stream")},
            headers=headers,
        )
    new_files = {p for p in sandbox.rglob("*") if p.is_file()} - before

    escaped = [p for p in new_files if not p.resolve().is_relative_to(jyra_root)]
    assert not escaped, f"files written outside jyra_dir: {escaped}"


def test_two_uploads_with_the_same_name_do_not_collide(client, headers, ticket_id, test_settings):
    """Stored under the attachment UUID, so the second upload must not
    overwrite the first — they are different files with different content."""
    first = client.post(
        f"/tickets/{ticket_id}/attachments",
        files={"upload": ("screenshot.png", b"first", "image/png")},
        headers=headers,
    ).json()
    second = client.post(
        f"/tickets/{ticket_id}/attachments",
        files={"upload": ("screenshot.png", b"second", "image/png")},
        headers=headers,
    ).json()

    assert first["id"] != second["id"]
    first_path = _stored_path(test_settings, first["id"])
    second_path = _stored_path(test_settings, second["id"])
    assert first_path != second_path
    assert first_path.read_bytes() == b"first"
    assert second_path.read_bytes() == b"second"
    assert first["filename"] == second["filename"] == "screenshot.png"


def test_content_hash_is_the_sha256_of_the_bytes(client, headers, ticket_id):
    import hashlib

    payload = b"some bytes to hash"
    body = client.post(
        f"/tickets/{ticket_id}/attachments",
        files={"upload": ("a.bin", payload, "application/octet-stream")},
        headers=headers,
    ).json()
    assert body["content_hash"] == hashlib.sha256(payload).hexdigest()


def test_deleting_a_ticket_removes_its_attachment_files(client, headers, ticket_id, test_settings):
    """A ticket's files must not outlive it — otherwise data/jyra/ accumulates
    orphans no row points at."""
    body = client.post(
        f"/tickets/{ticket_id}/attachments",
        files={"upload": ("a.png", b"a", "image/png")},
        headers=headers,
    ).json()
    path = _stored_path(test_settings, body["id"])
    assert path.exists()

    assert client.delete(f"/tickets/{ticket_id}", headers=headers).status_code == 200

    assert not path.exists()


def test_list_attachments_rejects_non_positive_limit(client, headers, ticket_id):
    assert (
        client.get(
            f"/tickets/{ticket_id}/attachments?limit=0", headers=headers
        ).status_code
        == 422
    )


# --- long-suffix bound (found in post-merge review of PR #29) -----------


@pytest.mark.parametrize("suffix_length", [1, 10, 200, 219, 221, 500, 4000])
def test_long_filename_suffix_never_crashes_the_upload(
    client, headers, ticket_id, test_settings, suffix_length
):
    """A caller-supplied suffix must not reach open() unbounded.

    The on-disk name is <attachment-uuid><suffix>, so a suffix past roughly
    219 chars pushed the component over the filesystem's 255-byte limit and
    raised OSError: File name too long — escaping as a plain-text 500 with no
    error envelope, which is the same failure shape as an unguarded KeyError
    or a bare ValueError from paginate.

    Not a security issue: the file never escaped jyra_dir. Just trivially
    triggerable by any caller.
    """
    name = "shot." + ("a" * suffix_length)
    response = client.post(
        f"/tickets/{ticket_id}/attachments",
        files={"upload": (name, b"payload", "application/octet-stream")},
        headers=headers,
    )

    # Either it works, or it is refused in the standard envelope. Never a
    # plain-text 500.
    if response.status_code == 200:
        written = _stored_path(test_settings, response.json()["id"])
        assert written.read_bytes() == b"payload"
        assert written.is_relative_to(Path(test_settings.jyra_dir))
        assert len(written.name.encode()) <= 255
    else:
        assert response.status_code in (400, 413, 422), response.status_code
        assert response.json()["error"]["code"], "error envelope missing"


def test_long_suffix_is_truncated_on_disk_but_kept_for_display(
    client, headers, ticket_id, test_settings
):
    """The suffix is cosmetic on disk — the real name lives in the `filename`
    column, so bounding one must not damage the other."""
    long_suffix = "a" * 400
    body = client.post(
        f"/tickets/{ticket_id}/attachments",
        files={"upload": (f"report.{long_suffix}", b"x", "application/octet-stream")},
        headers=headers,
    ).json()

    # Display name survives in full.
    assert body["filename"] == f"report.{long_suffix}"
    # On-disk name is bounded and still carries a recognisable suffix.
    on_disk = _stored_path(test_settings, body["id"]).name
    assert len(on_disk.encode()) <= 255
    assert on_disk.startswith(body["id"])


def test_a_normal_suffix_is_left_alone(client, headers, ticket_id, test_settings):
    """The bound must not disturb ordinary filenames."""
    body = client.post(
        f"/tickets/{ticket_id}/attachments",
        files={"upload": ("screenshot.png", b"x", "image/png")},
        headers=headers,
    ).json()
    assert _stored_path(test_settings, body["id"]).name == f"{body['id']}.png"
