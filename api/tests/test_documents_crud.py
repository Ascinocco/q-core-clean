"""Tests for the documents CRUD surface: POST/GET/list/DELETE /documents.

Two behaviours carry most of the risk and get the most attention here:

**Move versus copy.** The spec says a document is *moved* into
`data/documents/`, because `inbox/` is transient staging that should empty
as it is processed. But `intake/` holds statements awaiting import and is under
a standing do-not-modify constraint, so a source there must be
*copied*. Getting this backwards destroys an original, and does it quietly.

**Hash de-duplication.** A re-import of identical content returns the
existing record and must move nothing at all. That is the case where a move
would be both destructive and silent — the caller gets a valid-looking
record back while the source file has vanished.
"""

import hashlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def dirs(tmp_path: Path) -> dict:
    inbox = tmp_path / "inbox"
    intake = tmp_path / "intake"
    documents = tmp_path / "documents"
    for directory in (inbox, intake, documents):
        directory.mkdir(parents=True, exist_ok=True)
    return {"inbox": inbox, "intake": intake, "documents": documents}


@pytest.fixture()
def client_with_dirs(test_settings, dirs):
    from api.config import get_settings
    from api.main import app

    settings = test_settings.model_copy(
        update={
            "inbox_dir": str(dirs["inbox"]),
            "intake_dir": str(dirs["intake"]),
            "documents_dir": str(dirs["documents"]),
        }
    )
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app), settings
    app.dependency_overrides.clear()


def _auth(settings) -> dict:
    return {"Authorization": f"Bearer {settings.api_token}"}


def _register(client, settings, path: str, **kwargs):
    body = {"file_path": path, "title": "April statement", "doc_type": "statement"}
    body.update(kwargs)
    return client.post("/documents", json=body, headers=_auth(settings))


def _stored_path(settings, document_id: str) -> Path:
    """Inspect server storage without making the path part of an API result."""
    import sqlite3

    connection = sqlite3.connect(settings.db_path)
    try:
        row = connection.execute(
            "SELECT file_path FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        assert row is not None
        return Path(row[0])
    finally:
        connection.close()


def _entity(client, settings) -> str:
    response = client.post(
        "/entities",
        json={"type": "account", "name": "Chequing"},
        headers=_auth(settings),
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


# --- move vs copy ----------------------------------------------------------


def test_a_document_from_inbox_is_moved(client_with_dirs, dirs):
    """inbox/ is transient staging: processing it should empty it."""
    client, settings = client_with_dirs
    source = dirs["inbox"] / "scan.pdf"
    source.write_bytes(b"%PDF-1.4 statement bytes")

    response = _register(client, settings, "scan.pdf")

    assert response.status_code == 200, response.text
    assert not source.exists(), "inbox source should have been moved, not copied"
    assert _stored_path(settings, response.json()["id"]).is_file()


def test_a_document_from_intake_is_copied(client_with_dirs, dirs):
    """intake/ holds the real statements and is never modified during this
    run. Copying is a standing constraint, not a preference — if it is ever
    relaxed, this test is the thing that should have to change
    deliberately."""
    client, settings = client_with_dirs
    source = dirs["intake"] / "bank-card.pdf"
    source.write_bytes(b"%PDF-1.4 real statement")

    response = _register(client, settings, str(source))

    assert response.status_code == 200, response.text
    assert source.exists(), "intake source must NOT be moved or removed"
    assert source.read_bytes() == b"%PDF-1.4 real statement"
    assert _stored_path(settings, response.json()["id"]).is_file()


def test_the_stored_copy_is_byte_identical_to_the_source(client_with_dirs, dirs):
    client, settings = client_with_dirs
    payload = b"%PDF-1.4 exact bytes \x00\xff binary"
    (dirs["intake"] / "doc.pdf").write_bytes(payload)

    response = _register(client, settings, str(dirs["intake"] / "doc.pdf"))

    assert _stored_path(settings, response.json()["id"]).read_bytes() == payload


# --- hash de-duplication ---------------------------------------------------


def test_identical_content_returns_the_existing_record(client_with_dirs, dirs):
    client, settings = client_with_dirs
    (dirs["inbox"] / "first.pdf").write_bytes(b"same bytes")
    first = _register(client, settings, "first.pdf").json()

    (dirs["inbox"] / "second.pdf").write_bytes(b"same bytes")
    second = _register(client, settings, "second.pdf").json()

    assert second["id"] == first["id"]
    assert _stored_path(settings, second["id"]) == _stored_path(settings, first["id"])


def test_a_duplicate_moves_nothing(client_with_dirs, dirs):
    """The case where a move would be destructive AND silent: the caller
    gets a valid-looking record while its source has vanished."""
    client, settings = client_with_dirs
    (dirs["inbox"] / "first.pdf").write_bytes(b"same bytes")
    _register(client, settings, "first.pdf")

    duplicate = dirs["inbox"] / "second.pdf"
    duplicate.write_bytes(b"same bytes")
    _register(client, settings, "second.pdf")

    assert duplicate.exists(), "a duplicate must leave its source untouched"


def test_a_duplicate_creates_no_second_row(client_with_dirs, dirs):
    client, settings = client_with_dirs
    (dirs["inbox"] / "a.pdf").write_bytes(b"same bytes")
    _register(client, settings, "a.pdf")
    (dirs["inbox"] / "b.pdf").write_bytes(b"same bytes")
    _register(client, settings, "b.pdf")

    listing = client.get("/documents", headers=_auth(settings)).json()

    assert listing["total"] == 1


def test_different_content_creates_a_second_row(client_with_dirs, dirs):
    client, settings = client_with_dirs
    (dirs["inbox"] / "a.pdf").write_bytes(b"first")
    (dirs["inbox"] / "b.pdf").write_bytes(b"second")
    _register(client, settings, "a.pdf")
    _register(client, settings, "b.pdf")

    assert client.get("/documents", headers=_auth(settings)).json()["total"] == 2


def test_the_content_hash_is_sha256_of_the_bytes(client_with_dirs, dirs):
    client, settings = client_with_dirs
    payload = b"hash me"
    (dirs["inbox"] / "h.pdf").write_bytes(payload)

    body = _register(client, settings, "h.pdf").json()

    assert body["content_hash"] == hashlib.sha256(payload).hexdigest()


# --- the stored filename ---------------------------------------------------


def test_the_stored_filename_carries_slug_timestamp_and_id(client_with_dirs, dirs):
    client, settings = client_with_dirs
    (dirs["inbox"] / "s.pdf").write_bytes(b"x")

    body = _register(
        client, settings, "s.pdf", title="Bank Card — April 2026 Statement!"
    ).json()

    name = _stored_path(settings, body["id"]).name
    assert name.endswith(".pdf")
    stem = name[: -len(".pdf")]
    slug, timestamp, identifier = stem.rsplit("_", 2)
    assert identifier == body["id"], "the uuid in the filename is the document id"
    assert re.fullmatch(r"\d{8}-\d{6}", timestamp), timestamp
    assert slug == "bank-card-april-2026-statement"


def test_a_long_title_is_truncated_in_the_slug(client_with_dirs, dirs):
    client, settings = client_with_dirs
    (dirs["inbox"] / "l.pdf").write_bytes(b"x")

    body = _register(client, settings, "l.pdf", title="word " * 40).json()

    slug = _stored_path(settings, body["id"]).name.rsplit("_", 2)[0]
    assert len(slug) <= 60


def test_the_original_extension_is_preserved(client_with_dirs, dirs):
    client, settings = client_with_dirs
    (dirs["inbox"] / "photo.JPEG").write_bytes(b"x")

    body = _register(client, settings, "photo.JPEG").json()

    assert _stored_path(settings, body["id"]).suffix.lower() == ".jpeg"


def test_the_raw_path_is_withheld_while_server_storage_stays_contained(
    client_with_dirs, dirs
):
    """A returned raw path invites a model to bypass server-side redaction."""
    client, settings = client_with_dirs
    (dirs["inbox"] / "p.pdf").write_bytes(b"x")

    body = _register(client, settings, "p.pdf").json()
    stored = _stored_path(settings, body["id"])

    assert "file_path" not in body
    assert stored.is_absolute()
    assert stored.is_relative_to(dirs["documents"])


# --- validation ------------------------------------------------------------


def test_a_source_outside_the_allowed_roots_is_rejected(client_with_dirs, tmp_path):
    client, settings = client_with_dirs
    outside = tmp_path / "elsewhere.pdf"
    outside.write_bytes(b"x")

    response = _register(client, settings, str(outside))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_reference"


def test_a_missing_source_is_a_not_found(client_with_dirs):
    client, settings = client_with_dirs

    assert _register(client, settings, "nope.pdf").status_code == 404


def test_an_unknown_doc_type_is_rejected(client_with_dirs, dirs):
    client, settings = client_with_dirs
    (dirs["inbox"] / "d.pdf").write_bytes(b"x")

    response = _register(client, settings, "d.pdf", doc_type="invoice")

    assert response.status_code == 422


def test_an_unknown_entity_is_rejected(client_with_dirs, dirs):
    client, settings = client_with_dirs
    (dirs["inbox"] / "e.pdf").write_bytes(b"x")

    response = _register(client, settings, "e.pdf", entity_id="no-such-entity")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_reference"


def test_a_document_can_be_linked_to_an_entity(client_with_dirs, dirs):
    client, settings = client_with_dirs
    entity_id = _entity(client, settings)
    (dirs["inbox"] / "linked.pdf").write_bytes(b"x")

    body = _register(client, settings, "linked.pdf", entity_id=entity_id).json()

    assert body["entity_id"] == entity_id


# --- read ------------------------------------------------------------------


def test_get_returns_the_document(client_with_dirs, dirs):
    client, settings = client_with_dirs
    (dirs["inbox"] / "g.pdf").write_bytes(b"x")
    created = _register(client, settings, "g.pdf").json()

    fetched = client.get(
        f"/documents/{created['id']}", headers=_auth(settings)
    ).json()

    assert fetched["id"] == created["id"]
    assert "file_path" not in fetched


def test_registered_document_is_extracted_by_id_without_disclosing_path(
    client_with_dirs, dirs
):
    from api.tests.pdf_fixture import make_pdf

    client, settings = client_with_dirs
    secret = "4532015112830366"
    (dirs["inbox"] / "g.pdf").write_bytes(
        make_pdf([f"Account {secret}\nSAFE MERCHANT 12.34"])
    )
    created = _register(client, settings, "g.pdf").json()

    response = client.post(
        f"/documents/{created['id']}/extract", json={}, headers=_auth(settings)
    )

    assert response.status_code == 200, response.text
    assert secret not in response.text
    assert "SAFE MERCHANT" in response.json()["text"]
    assert "file_path" not in created


def test_get_an_unknown_document_is_a_not_found(client_with_dirs):
    client, settings = client_with_dirs

    response = client.get("/documents/nope", headers=_auth(settings))

    assert response.status_code == 404


def test_list_filters_by_doc_type(client_with_dirs, dirs):
    client, settings = client_with_dirs
    (dirs["inbox"] / "a.pdf").write_bytes(b"a")
    (dirs["inbox"] / "b.pdf").write_bytes(b"b")
    _register(client, settings, "a.pdf", doc_type="statement")
    _register(client, settings, "b.pdf", doc_type="receipt")

    listing = client.get(
        "/documents", params={"doc_type": "receipt"}, headers=_auth(settings)
    ).json()

    assert listing["total"] == 1
    assert listing["items"][0]["doc_type"] == "receipt"


def test_list_filters_by_entity(client_with_dirs, dirs):
    client, settings = client_with_dirs
    entity_id = _entity(client, settings)
    (dirs["inbox"] / "a.pdf").write_bytes(b"a")
    (dirs["inbox"] / "b.pdf").write_bytes(b"b")
    _register(client, settings, "a.pdf", entity_id=entity_id)
    _register(client, settings, "b.pdf")

    listing = client.get(
        "/documents", params={"entity_id": entity_id}, headers=_auth(settings)
    ).json()

    assert listing["total"] == 1


def test_list_is_paginated(client_with_dirs, dirs):
    client, settings = client_with_dirs
    for index in range(3):
        (dirs["inbox"] / f"{index}.pdf").write_bytes(f"doc {index}".encode())
        _register(client, settings, f"{index}.pdf")

    listing = client.get(
        "/documents", params={"limit": 2}, headers=_auth(settings)
    ).json()

    assert listing["total"] == 3
    assert len(listing["items"]) == 2


# --- delete ----------------------------------------------------------------


def test_delete_removes_the_row_and_the_file(client_with_dirs, dirs):
    """No orphans in either direction: a row without a file is a broken
    reference, a file without a row is invisible storage that grows."""
    client, settings = client_with_dirs
    (dirs["inbox"] / "d.pdf").write_bytes(b"x")
    created = _register(client, settings, "d.pdf").json()
    stored = _stored_path(settings, created["id"])
    assert stored.is_file()

    response = client.delete(
        f"/documents/{created['id']}", headers=_auth(settings)
    )

    assert response.status_code == 200
    assert not stored.exists()
    assert (
        client.get(f"/documents/{created['id']}", headers=_auth(settings)).status_code
        == 404
    )


def test_delete_an_unknown_document_is_a_not_found(client_with_dirs):
    client, settings = client_with_dirs

    assert (
        client.delete("/documents/nope", headers=_auth(settings)).status_code == 404
    )


def test_delete_refuses_a_row_whose_file_escapes_the_documents_dir(
    client_with_dirs, dirs, tmp_path
):
    """A row's stored path is data, and data can be wrong. Deleting whatever
    it points at would turn a bad row into an arbitrary file deletion, so
    the path is re-checked at delete time rather than trusted because the
    API wrote it."""
    import sqlite3

    client, settings = client_with_dirs
    (dirs["inbox"] / "d.pdf").write_bytes(b"x")
    created = _register(client, settings, "d.pdf").json()

    outside = tmp_path / "precious.txt"
    outside.write_text("must survive")
    connection = sqlite3.connect(settings.db_path)
    connection.execute(
        "UPDATE documents SET file_path = ? WHERE id = ?", (str(outside), created["id"])
    )
    connection.commit()
    connection.close()

    response = client.delete(
        f"/documents/{created['id']}", headers=_auth(settings)
    )

    assert response.status_code == 400
    assert outside.exists(), "a file outside data/documents/ must never be deleted"
