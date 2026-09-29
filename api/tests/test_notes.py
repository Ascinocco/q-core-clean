"""Notes and note_links over the API (ticket T-23). Invented data only."""

import sqlite3

import pytest

from api.notes import LIST_NOTES_ORDER, derive_title

AUTH = {"Authorization": "Bearer test-token"}


def _entity(client, name="Invented Car"):
    response = client.post("/entities", json={"type": "vehicle", "name": name}, headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _note(client, body, links=None):
    payload = {"body": body}
    if links is not None:
        payload["links"] = links
    response = client.post("/notes", json=payload, headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def test_create_get_revise_and_delete_a_standalone_note(client):
    note = _note(client, "# Home server build\n\n- pick a case")
    assert note["title"] == "Home server build"
    assert note["links"] == [] and note["updated_at"] is None

    for revision in ("# Home server build\n\n- pick a case\n- pick a CPU",
                     "# Home server build v2\n\n- case: fractal\n- cpu: 8 cores"):
        response = client.patch(f"/notes/{note['id']}", json={"body": revision}, headers=AUTH)
        assert response.status_code == 200, response.text
        assert response.json()["body"] == revision and response.json()["updated_at"] is not None

    fetched = client.get(f"/notes/{note['id']}", headers=AUTH).json()
    assert fetched["title"] == "Home server build v2" and "8 cores" in fetched["body"]

    gone = client.delete(f"/notes/{note['id']}", headers=AUTH)
    assert gone.status_code == 200 and gone.json()["deleted"] == note["id"]
    assert client.get(f"/notes/{note['id']}", headers=AUTH).status_code == 404


def test_link_find_by_target_unlink_and_delete_removes_links(client):
    car = _entity(client)
    other = _entity(client, "Invented House")
    note = _note(client, "Winter tire plan")
    linked = client.post(f"/notes/{note['id']}/links",
                         json={"target_type": "entity", "target_id": car}, headers=AUTH).json()
    again = client.post(f"/notes/{note['id']}/links",
                        json={"target_type": "entity", "target_id": car}, headers=AUTH).json()
    assert linked["link_id"] == again["link_id"] and len(again["links"]) == 1

    found = client.get("/notes", params={"target_type": "entity", "target_id": car}, headers=AUTH).json()
    assert [n["id"] for n in found["items"]] == [note["id"]] and found["items"][0]["link_count"] == 1
    none = client.get("/notes", params={"target_type": "entity", "target_id": other}, headers=AUTH).json()
    assert none["total"] == 0

    # Entity deletion is refused while the note still points at it (entities.py).
    assert client.delete(f"/entities/{car}", headers=AUTH).status_code == 409

    after = client.delete(f"/notes/{note['id']}/links/{linked['link_id']}", headers=AUTH).json()
    assert after["links"] == []
    assert client.delete(f"/notes/{note['id']}/links/{linked['link_id']}", headers=AUTH).status_code == 404

    relinked = _note(client, "Second", links=[{"target_type": "entity", "target_id": car}])
    deleted = client.delete(f"/notes/{relinked['id']}", headers=AUTH).json()
    assert deleted["links_removed"] == 1
    assert client.delete(f"/entities/{car}", headers=AUTH).status_code == 200


@pytest.mark.parametrize("target, status", [
    ({"target_type": "planet", "target_id": "x"}, 422),
    ({"target_type": "entity", "target_id": "no-such-entity"}, 400),
    ({"target_type": "document", "target_id": "no-such-doc"}, 400),
    ({"target_type": "ticket", "target_id": "no-such-ticket"}, 400),
])
def test_link_validation_refuses_bad_types_and_missing_targets(client, target, status):
    note = _note(client, "Validation")
    assert client.post(f"/notes/{note['id']}/links", json=target, headers=AUTH).status_code == status
    # On create, one bad link refuses the whole note: nothing half-written.
    before = client.get("/notes", headers=AUTH).json()["total"]
    assert client.post("/notes", json={"body": "x", "links": [target]}, headers=AUTH).status_code == status
    assert client.get("/notes", headers=AUTH).json()["total"] == before


def test_filters_are_independent_and_refuse_ids_that_name_nothing(client):
    car = _entity(client)
    note = _note(client, "Linked", links=[{"target_type": "entity", "target_id": car}])
    _note(client, "Standalone")
    by_id = client.get("/notes", params={"target_id": car}, headers=AUTH).json()
    by_type = client.get("/notes", params={"target_type": "entity"}, headers=AUTH).json()
    assert [n["id"] for n in by_id["items"]] == [note["id"]] == [n["id"] for n in by_type["items"]]
    assert client.get("/notes", params={"target_type": "ticket"}, headers=AUTH).json()["total"] == 0
    for params in ({"target_type": "entity", "target_id": "nope"}, {"target_id": "nope"}):
        refused = client.get("/notes", params=params, headers=AUTH)
        assert refused.status_code == 400 and "nope" in refused.json()["error"]["message"]


def test_text_search_is_a_literal_case_insensitive_substring(client):
    _note(client, "Budget: 100% of the bonus")
    _note(client, "Unrelated plan")
    assert client.get("/notes", params={"q": "BONUS"}, headers=AUTH).json()["total"] == 1
    # % and _ are literals, not wildcards.
    assert client.get("/notes", params={"q": "100%"}, headers=AUTH).json()["total"] == 1
    assert client.get("/notes", params={"q": "_"}, headers=AUTH).json()["total"] == 0


def test_list_is_paginated_ordered_as_stated_and_previews_only(client, test_settings):
    ids = [_note(client, f"Note {i}\n" + "long body " * 100)["id"] for i in range(3)]
    db = sqlite3.connect(test_settings.db_path)
    for i, note_id in enumerate(ids):
        db.execute("UPDATE notes SET created_at = ? WHERE id = ?", (f"2026-01-0{i + 1} 00:00:00", note_id))
    db.commit()
    db.close()
    client.patch(f"/notes/{ids[0]}", json={"body": "Note 0 edited"}, headers=AUTH)

    assert "most recently edited first" in LIST_NOTES_ORDER
    page = client.get("/notes", params={"limit": 2}, headers=AUTH).json()
    assert page["total"] == 3 and page["limit"] == 2 and [n["id"] for n in page["items"]] == [ids[0], ids[2]]
    rest = client.get("/notes", params={"limit": 2, "offset": 2}, headers=AUTH).json()
    assert [n["id"] for n in rest["items"]] == [ids[1]]
    item = rest["items"][0]
    assert "body" not in item and len(item["preview"]) <= 240 and item["title"] == "Note 1"
    assert client.get("/notes", params={"limit": 0}, headers=AUTH).status_code == 422


def test_bodies_are_privacy_screened_and_updates_cannot_be_empty(client):
    refused = client.post("/notes", json={"body": "card 4111111111111111"}, headers=AUTH)
    assert refused.status_code == 422 and "4111111111111111" not in refused.text
    note = _note(client, "ok")
    assert client.patch(f"/notes/{note['id']}", json={}, headers=AUTH).status_code == 422
    assert client.patch(f"/notes/{note['id']}", json={"body": ""}, headers=AUTH).status_code == 422
    assert client.patch("/notes/nope", json={"body": "x"}, headers=AUTH).status_code == 404


@pytest.mark.parametrize("body, title", [
    ("# Heading\nrest", "Heading"),
    ("\n\n  ## Indented  \nrest", "Indented"),
    ("plain first line\nsecond", "plain first line"),
    ("#\n\n", "(untitled)"),
    ("x" * 200, "x" * 119 + "…"),
])
def test_title_is_the_first_nonblank_line(body, title):
    assert derive_title(body) == title
