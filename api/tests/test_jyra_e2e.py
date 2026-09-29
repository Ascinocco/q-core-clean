import pytest


@pytest.fixture()
def headers(test_settings):
    return {"Authorization": f"Bearer {test_settings.api_token}"}


def test_full_jyra_lifecycle(client, headers):
    """The flow from the spec: project -> board -> epic -> task -> spec it ->
    an agent claims it, hits a wall, and blocks it -> the history explains
    the whole thing."""
    project = client.post(
        "/entities",
        json={
            "type": "project",
            "name": "sunny hill bakery",
            "attributes": {"description": "the app"},
        },
        headers=headers,
    ).json()

    board = client.post(
        "/boards",
        json={"entity_id": project["id"], "title": "Delivery"},
        headers=headers,
    ).json()

    epic = client.post(
        "/tickets",
        json={
            "board_id": board["id"],
            "type": "epic",
            "title": "Sales stats",
            "actor": "owner",
        },
        headers=headers,
    ).json()

    task = client.post(
        "/tickets",
        json={
            "board_id": board["id"],
            "type": "task",
            "title": "Import order history",
            "description": "## Notes\n\nSee the attached screenshot.",
            "parent_id": epic["id"],
            "actor": "owner",
        },
        headers=headers,
    ).json()

    attachment = client.post(
        f"/tickets/{task['id']}/attachments",
        files={"upload": ("mock.png", b"bytes", "image/png")},
        headers=headers,
    ).json()
    # The media type the content route serves, not the suffix of a path
    # the response no longer carries.
    served = client.get(
        f"/attachments/{attachment['id']}/content", headers=headers
    )
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("image/png")

    # An epic never enters the agent lane. 422, not 400: validate_ticket_status
    # was settled at 422 in Tasks 3/5 — a status outside the ticket type's legal
    # set is wrong on its own terms, not a reference to something missing.
    rejected = client.post(
        f"/tickets/{epic['id']}/transition",
        json={"to_status": "agent_ready", "actor": "owner", "note": "nope"},
        headers=headers,
    )
    assert rejected.status_code == 422

    client.post(
        f"/tickets/{task['id']}/transition",
        json={"to_status": "agent_ready", "actor": "owner", "note": "specced"},
        headers=headers,
    )

    claimed = client.post(
        "/tickets/claim", json={"actor": "agent-1"}, headers=headers
    ).json()
    assert claimed["id"] == task["id"]
    assert claimed["status"] == "agent_coding"

    # The column is now drained.
    assert (
        client.post(
            "/tickets/claim", json={"actor": "agent-2"}, headers=headers
        ).status_code
        == 204
    )

    client.post(
        f"/tickets/{task['id']}/transition",
        json={
            "to_status": "blocked",
            "actor": "agent-1",
            "note": "order history format is ambiguous, need a decision",
        },
        headers=headers,
    )

    # Blocked takes it out of the loop's reach, so it is never retried.
    assert (
        client.post(
            "/tickets/claim", json={"actor": "agent-3"}, headers=headers
        ).status_code
        == 204
    )

    history = client.get(f"/tickets/{task['id']}/transitions", headers=headers).json()
    assert [
        (e["from_status"], e["to_status"], e["actor"]) for e in history["items"]
    ] == [
        (None, "backlog", "owner"),
        ("backlog", "agent_ready", "owner"),
        ("agent_ready", "agent_coding", "agent-1"),
        ("agent_coding", "blocked", "agent-1"),
    ]
    assert "ambiguous" in history["items"][-1]["note"]

    view = client.get(f"/boards/{board['id']}", headers=headers).json()
    assert view["columns"]["blocked"]["total"] == 1
    assert view["columns"]["backlog"]["total"] == 1  # the epic
    assert view["entity"]["name"] == "sunny hill bakery"


# --- beyond the plan ---------------------------------------------------


def test_a_project_inherits_the_existing_entity_plumbing(client, headers):
    """The concrete payoff of project-as-entity-type rather than its own
    table: a project is an ordinary entity, so everything keyed on entity_id
    works on it with no new code. If this ever fails, the design's central
    trade-off stopped paying for itself."""
    project = client.post(
        "/entities",
        json={"type": "project", "name": "inherits", "attributes": {"description": "d"}},
        headers=headers,
    ).json()

    listed = client.get("/entities?type=project", headers=headers).json()
    assert project["id"] in [item["id"] for item in listed["items"]]

    fetched = client.get(f"/entities/{project['id']}", headers=headers).json()
    assert fetched["attributes"] == {"description": "d"}

    archived = client.patch(
        f"/entities/{project['id']}", json={"status": "archived"}, headers=headers
    ).json()
    assert archived["status"] == "archived"

    # A relationship to a project is an ordinary entity_relationships row.
    person = client.post(
        "/entities", json={"type": "person", "name": "Alex"}, headers=headers
    ).json()
    relationship = client.post(
        f"/entities/{person['id']}/relationships",
        json={"to_entity_id": project["id"], "relationship_type": "maintains"},
        headers=headers,
    )
    assert relationship.status_code == 200


def test_stale_claim_recovery_round_trip(client, headers, test_settings):
    """The documented recovery path: an agent dies mid-ticket, the ticket is
    surfaced by the claimed_before query, and recovery is an ordinary
    transition — so the abandonment stays visible in the history rather than a
    reaper silently undoing it."""
    import sqlite3

    entity = client.post(
        "/entities", json={"type": "project", "name": "p"}, headers=headers
    ).json()
    board = client.post(
        "/boards", json={"entity_id": entity["id"], "title": "B"}, headers=headers
    ).json()
    ticket = client.post(
        "/tickets",
        json={"board_id": board["id"], "type": "task", "title": "t", "actor": "owner"},
        headers=headers,
    ).json()
    client.post(
        f"/tickets/{ticket['id']}/transition",
        json={"to_status": "agent_ready", "actor": "owner", "note": "specced"},
        headers=headers,
    )
    client.post("/tickets/claim", json={"actor": "agent-1"}, headers=headers)

    # The agent dies; its claim ages.
    connection = sqlite3.connect(test_settings.db_path)
    try:
        connection.execute(
            "UPDATE tickets SET claimed_at = '2000-01-01 00:00:00' WHERE id = ?",
            (ticket["id"],),
        )
        connection.commit()
    finally:
        connection.close()

    stale = client.get(
        "/tickets?status=agent_coding&claimed_before=2020-01-01", headers=headers
    ).json()
    assert [item["id"] for item in stale["items"]] == [ticket["id"]]

    recovered = client.post(
        f"/tickets/{ticket['id']}/transition",
        json={
            "to_status": "agent_ready",
            "actor": "owner",
            "note": "agent-1 died, returning to the queue",
        },
        headers=headers,
    )
    assert recovered.status_code == 200

    # Back in the loop's reach, and the abandonment is in the history.
    reclaimed = client.post(
        "/tickets/claim", json={"actor": "agent-2"}, headers=headers
    ).json()
    assert reclaimed["id"] == ticket["id"]
    history = client.get(
        f"/tickets/{ticket['id']}/transitions", headers=headers
    ).json()["items"]
    assert any("died" in (entry["note"] or "") for entry in history)


def test_every_status_change_is_in_the_history(client, headers):
    """The design's core audit claim, stated as an invariant: a ticket's
    history length must equal its number of status changes plus the creation
    row — including the claim, which is the one status change that does not go
    through the transition endpoint."""
    entity = client.post(
        "/entities", json={"type": "project", "name": "p"}, headers=headers
    ).json()
    board = client.post(
        "/boards", json={"entity_id": entity["id"], "title": "B"}, headers=headers
    ).json()
    ticket = client.post(
        "/tickets",
        json={"board_id": board["id"], "type": "task", "title": "t", "actor": "owner"},
        headers=headers,
    ).json()

    moves = ["in_progress", "agent_ready"]
    for status in moves:
        client.post(
            f"/tickets/{ticket['id']}/transition",
            json={"to_status": status, "actor": "owner", "note": "n"},
            headers=headers,
        )
    client.post("/tickets/claim", json={"actor": "agent-1"}, headers=headers)  # +1

    history = client.get(
        f"/tickets/{ticket['id']}/transitions", headers=headers
    ).json()["items"]
    assert len(history) == 1 + len(moves) + 1

    # And the chain is unbroken: each row starts where the previous ended.
    for earlier, later in zip(history, history[1:]):
        assert later["from_status"] == earlier["to_status"]
