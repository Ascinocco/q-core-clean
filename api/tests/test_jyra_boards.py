import pytest


@pytest.fixture()
def headers(test_settings):
    return {"Authorization": f"Bearer {test_settings.api_token}"}


@pytest.fixture()
def project_id(client, headers):
    return client.post(
        "/entities",
        json={"type": "project", "name": "sunny hill bakery"},
        headers=headers,
    ).json()["id"]


def test_create_board(client, headers, project_id):
    response = client.post(
        "/boards", json={"entity_id": project_id, "title": "Delivery"}, headers=headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Delivery"
    assert body["entity_id"] == project_id
    assert body["id"]


def test_create_board_rejects_unknown_entity(client, headers):
    response = client.post(
        "/boards", json={"entity_id": "nope", "title": "Delivery"}, headers=headers
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_reference"


def test_board_view_returns_seven_empty_columns(client, headers, project_id):
    board = client.post(
        "/boards", json={"entity_id": project_id, "title": "Delivery"}, headers=headers
    ).json()
    response = client.get(f"/boards/{board['id']}", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert list(body["columns"]) == [
        "backlog",
        "in_progress",
        "agent_ready",
        "agent_coding",
        "review",
        "blocked",
        "done",
    ]
    # Columns are {"items", "total"} now — the board embeds summaries so
    # its size does not scale with description length (a todo).
    assert all(
        column == {"items": [], "total": 0} for column in body["columns"].values()
    )
    assert body["entity"]["name"] == "sunny hill bakery"


def test_get_missing_board_is_404(client, headers):
    response = client.get("/boards/nope", headers=headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_list_boards_filters_by_entity(client, headers, project_id):
    other = client.post(
        "/entities", json={"type": "pet", "name": "Rex"}, headers=headers
    ).json()["id"]
    client.post(
        "/boards", json={"entity_id": project_id, "title": "Delivery"}, headers=headers
    )
    client.post("/boards", json={"entity_id": other, "title": "Vet"}, headers=headers)

    response = client.get(f"/boards?entity_id={project_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["total"] == 1


def test_rename_board(client, headers, project_id):
    board = client.post(
        "/boards", json={"entity_id": project_id, "title": "Delivery"}, headers=headers
    ).json()
    response = client.patch(
        f"/boards/{board['id']}", json={"title": "Renamed"}, headers=headers
    )
    assert response.status_code == 200
    assert response.json()["title"] == "Renamed"


def test_delete_board(client, headers, project_id):
    board = client.post(
        "/boards", json={"entity_id": project_id, "title": "Delivery"}, headers=headers
    ).json()
    response = client.delete(f"/boards/{board['id']}", headers=headers)
    # 200 + {"deleted": true}, matching DELETE /entities and /relationships.
    # One verb, one response contract across the whole API.
    assert response.status_code == 200
    assert response.json() == {"deleted": True}
    assert client.get(f"/boards/{board['id']}", headers=headers).status_code == 404


def test_boards_require_auth(client):
    assert client.get("/boards").status_code == 401


def test_list_boards_rejects_non_positive_limit(client, headers):
    # paginate() raises ValueError on limit <= 0, which surfaces as an
    # unhandled 500. Every other listing in this API guards it with ge=1;
    # this asserts /boards does too rather than repeating the Phase 2 bug.
    assert client.get("/boards?limit=0", headers=headers).status_code == 422


def test_board_view_rejects_a_ticket_with_an_unknown_status(
    client, headers, project_id, test_settings
):
    """A status outside ALL_STATUS_ORDER used to be a bare KeyError, which
    escapes as a plain-text traceback and breaks the error envelope.

    Deliberately an error rather than skipping the row: omitting a ticket from
    its board would hide work silently, which is worse than a loud failure in
    a system meant to be a trustworthy record.
    """
    import sqlite3

    board = client.post(
        "/boards", json={"entity_id": project_id, "title": "Delivery"}, headers=headers
    ).json()
    connection = sqlite3.connect(test_settings.db_path)
    try:
        connection.execute(
            "INSERT INTO tickets (id, board_id, type, title, status) "
            "VALUES ('t1', ?, 'task', 'x', 'not_a_column')",
            (board["id"],),
        )
        connection.commit()
    finally:
        connection.close()

    response = client.get(f"/boards/{board['id']}", headers=headers)

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "data_integrity"
    assert "not_a_column" in body["error"]["message"]


def test_delete_board_with_tickets_is_refused(client, headers, project_id):
    """This branch had NO test until the 409 move surfaced it.

    Moving `ConflictError` to 409 failed eight tests — one per covered
    raise site plus the envelope — and `delete_board` was not among
    them, which is how a raise site with no behavioural coverage
    announces itself. A status change is a cheap census of what is
    actually exercised: every site that did not fail was either
    untested or asserting something weaker than a code.
    """
    board = client.post(
        "/boards", json={"entity_id": project_id, "title": "Delivery"}, headers=headers
    ).json()
    client.post(
        "/tickets",
        json={
            "board_id": board["id"],
            "type": "task",
            "title": "Still here",
            "actor": "impl-3",
        },
        headers=headers,
    )

    response = client.delete(f"/boards/{board['id']}", headers=headers)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "conflict"
    assert "ticket" in response.json()["error"]["message"]
