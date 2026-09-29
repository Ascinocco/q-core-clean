from pathlib import Path

import pytest


@pytest.fixture()
def headers(test_settings):
    return {"Authorization": f"Bearer {test_settings.api_token}"}


@pytest.fixture()
def board_id(client, headers):
    entity = client.post(
        "/entities",
        json={"type": "project", "name": "sunny hill bakery"},
        headers=headers,
    ).json()
    return client.post(
        "/boards",
        json={"entity_id": entity["id"], "title": "Delivery"},
        headers=headers,
    ).json()["id"]


@pytest.fixture()
def client_other_board(client, headers):
    entity = client.post(
        "/entities", json={"type": "project", "name": "other"}, headers=headers
    ).json()
    return client.post(
        "/boards", json={"entity_id": entity["id"], "title": "Other"}, headers=headers
    ).json()["id"]


def _make(client, headers, board_id, **overrides):
    payload = {
        "board_id": board_id,
        "type": "task",
        "title": "Do the thing",
        "actor": "owner",
    }
    payload.update(overrides)
    return client.post("/tickets", json=payload, headers=headers)


def test_create_ticket_starts_in_backlog(client, headers, board_id):
    response = _make(client, headers, board_id)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "backlog"
    assert body["position"] == 0
    assert body["claimed_by"] is None


def test_create_ticket_writes_a_creation_transition(client, headers, board_id):
    ticket = _make(client, headers, board_id).json()
    row = client.get(f"/tickets/{ticket['id']}", headers=headers).json()
    assert row["title"] == "Do the thing"
    history = client.get(f"/tickets/{ticket['id']}/transitions", headers=headers)
    assert history.status_code == 200
    entries = history.json()["items"]
    assert len(entries) == 1
    assert entries[0]["from_status"] is None
    assert entries[0]["to_status"] == "backlog"
    assert entries[0]["actor"] == "owner"


def test_positions_increment_within_a_column(client, headers, board_id):
    first = _make(client, headers, board_id, title="one").json()
    second = _make(client, headers, board_id, title="two").json()
    assert first["position"] == 0
    assert second["position"] == 1


def test_create_ticket_rejects_unknown_board(client, headers):
    response = client.post(
        "/tickets",
        json={"board_id": "nope", "type": "task", "title": "x", "actor": "owner"},
        headers=headers,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_reference"


def test_parent_must_outrank_child(client, headers, board_id):
    parent = _make(client, headers, board_id, type="bug", title="a bug").json()
    response = _make(client, headers, board_id, type="epic", parent_id=parent["id"])
    assert response.status_code == 400
    assert "cannot be the parent" in response.json()["error"]["message"]


def test_parent_must_be_on_the_same_board(
    client, headers, board_id, client_other_board
):
    parent = _make(client, headers, client_other_board, type="epic").json()
    response = _make(client, headers, board_id, type="task", parent_id=parent["id"])
    assert response.status_code == 400


def test_patch_cannot_change_status(client, headers, board_id):
    ticket = _make(client, headers, board_id).json()
    response = client.patch(
        f"/tickets/{ticket['id']}", json={"status": "done"}, headers=headers
    )
    assert response.status_code == 422
    unchanged = client.get(f"/tickets/{ticket['id']}", headers=headers).json()
    assert unchanged["status"] == "backlog"


def test_patch_updates_title_and_description(client, headers, board_id):
    ticket = _make(client, headers, board_id).json()
    response = client.patch(
        f"/tickets/{ticket['id']}",
        json={"title": "Renamed", "description": "# Heading\n\nbody"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["title"] == "Renamed"
    assert response.json()["description"].startswith("# Heading")


def test_list_tickets_filters_by_status_and_type(client, headers, board_id):
    _make(client, headers, board_id, type="task", title="t")
    _make(client, headers, board_id, type="bug", title="b")
    response = client.get(f"/tickets?board_id={board_id}&type=bug", headers=headers)
    assert response.json()["total"] == 1
    response = client.get(
        f"/tickets?board_id={board_id}&status=backlog", headers=headers
    )
    assert response.json()["total"] == 2


def test_delete_ticket_with_children_is_rejected(client, headers, board_id):
    parent = _make(client, headers, board_id, type="epic").json()
    _make(client, headers, board_id, type="task", parent_id=parent["id"])
    response = client.delete(f"/tickets/{parent['id']}", headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_board_view_places_ticket_in_backlog_column(client, headers, board_id):
    _make(client, headers, board_id)
    board = client.get(f"/boards/{board_id}", headers=headers).json()
    assert board["columns"]["backlog"]["total"] == 1
    assert board["columns"]["done"] == {"items": [], "total": 0}


# --- beyond the plan ---------------------------------------------------


def test_delete_ticket_returns_the_standard_delete_body(client, headers, board_id):
    ticket = _make(client, headers, board_id).json()
    response = client.delete(f"/tickets/{ticket['id']}", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"deleted": True}
    assert client.get(f"/tickets/{ticket['id']}", headers=headers).status_code == 404


def test_list_tickets_rejects_non_positive_limit(client, headers):
    # paginate() raises ValueError on limit <= 0 -> unhandled 500.
    assert client.get("/tickets?limit=0", headers=headers).status_code == 422


def test_list_transitions_rejects_non_positive_limit(client, headers, board_id):
    ticket = _make(client, headers, board_id).json()
    assert (
        client.get(
            f"/tickets/{ticket['id']}/transitions?limit=0", headers=headers
        ).status_code
        == 422
    )


def test_ticket_cannot_be_its_own_parent(client, headers, board_id):
    """Self-parenting is blocked by the strict-rank rule rather than an
    explicit check — a ticket never outranks itself. Pinned because the design
    relies on that to argue cycles are impossible, so if rank ever loosened to
    allow equal ranks this would have to be caught some other way."""
    ticket = _make(client, headers, board_id, type="epic").json()
    response = client.patch(
        f"/tickets/{ticket['id']}",
        json={"parent_id": ticket["id"]},
        headers=headers,
    )
    assert response.status_code == 400


def test_deleting_a_ticket_removes_its_transition_history(client, headers, board_id):
    """The FK from ticket_transitions requires this, so it is not optional —
    but it does mean a delete destroys audit history, which is worth pinning
    as a deliberate behaviour rather than leaving implicit."""
    ticket = _make(client, headers, board_id).json()
    assert (
        len(
            client.get(
                f"/tickets/{ticket['id']}/transitions", headers=headers
            ).json()["items"]
        )
        == 1
    )
    client.delete(f"/tickets/{ticket['id']}", headers=headers)
    assert (
        client.get(
            f"/tickets/{ticket['id']}/transitions", headers=headers
        ).status_code
        == 404
    )


# --- get_ticket embeds its context (a todo) --------------------------


def test_get_ticket_embeds_its_attachments(client, headers, board_id):
    """The design says GET /tickets/{id} "includes attachments and
    parent/children refs"; it returned the bare row, so a client needed a
    second call. The agent case is "show me this ticket and its
    screenshots", which should be one round trip."""
    ticket = _make(client, headers, board_id).json()
    client.post(
        f"/tickets/{ticket['id']}/attachments",
        files={"upload": ("shot.png", b"bytes", "image/png")},
        headers=headers,
    )

    fetched = client.get(f"/tickets/{ticket['id']}", headers=headers).json()

    assert len(fetched["attachments"]) == 1
    attachment = fetched["attachments"][0]
    assert attachment["filename"] == "shot.png"
    assert "file_path" not in attachment, "content is reached by id (ticket T-44)"
    # Asserted THROUGH the content route rather than off disk: reading the
    # bytes by id is the capability that replaced the path, so this now
    # exercises it instead of merely relocating the old assertion.
    served = client.get(
        f"/attachments/{attachment['id']}/content", headers=headers
    )
    assert served.status_code == 200
    assert served.content == b"bytes"


def test_get_ticket_embeds_an_empty_attachment_list(client, headers, board_id):
    """Present and empty, not absent. A client that has to check whether the
    key exists before iterating is a client that will forget to."""
    ticket = _make(client, headers, board_id).json()

    fetched = client.get(f"/tickets/{ticket['id']}", headers=headers).json()

    assert fetched["attachments"] == []


def test_get_ticket_embeds_child_refs(client, headers, board_id):
    """Refs, not whole rows: enough to show the hierarchy and fetch a child,
    without the response growing without bound on a large epic."""
    epic = _make(client, headers, board_id, type="epic", title="E").json()
    first = _make(
        client, headers, board_id, type="task", title="one", parent_id=epic["id"]
    ).json()
    second = _make(
        client, headers, board_id, type="bug", title="two", parent_id=epic["id"]
    ).json()

    fetched = client.get(f"/tickets/{epic['id']}", headers=headers).json()

    assert [child["id"] for child in fetched["children"]] == [
        first["id"],
        second["id"],
    ]
    assert fetched["children"][0]["title"] == "one"
    assert fetched["children"][1]["type"] == "bug"
    assert "description" not in fetched["children"][0]


def test_get_ticket_embeds_an_empty_children_list(client, headers, board_id):
    ticket = _make(client, headers, board_id).json()
    assert client.get(f"/tickets/{ticket['id']}", headers=headers).json()["children"] == []


def test_a_child_ticket_still_reports_its_parent_id(client, headers, board_id):
    """parent_id is the parent ref and was already present; embedding
    children must not have displaced it."""
    epic = _make(client, headers, board_id, type="epic", title="E").json()
    child = _make(
        client, headers, board_id, type="task", parent_id=epic["id"]
    ).json()

    fetched = client.get(f"/tickets/{child['id']}", headers=headers).json()

    assert fetched["parent_id"] == epic["id"]
    assert fetched["children"] == []


def test_list_tickets_does_not_embed_attachments(client, headers, board_id):
    """Only the single-ticket read embeds. A listing that expanded every
    ticket's attachments would turn one query into N, which is the cost the
    board view deliberately avoids."""
    ticket = _make(client, headers, board_id).json()
    client.post(
        f"/tickets/{ticket['id']}/attachments",
        files={"upload": ("a.png", b"a", "image/png")},
        headers=headers,
    )

    listed = client.get(f"/tickets?board_id={board_id}", headers=headers).json()

    assert "attachments" not in listed["items"][0]
