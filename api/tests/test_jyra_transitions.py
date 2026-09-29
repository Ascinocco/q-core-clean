import pytest


@pytest.fixture()
def headers(test_settings):
    return {"Authorization": f"Bearer {test_settings.api_token}"}


@pytest.fixture()
def board_id(client, headers):
    entity = client.post(
        "/entities", json={"type": "project", "name": "proj"}, headers=headers
    ).json()
    return client.post(
        "/boards",
        json={"entity_id": entity["id"], "title": "Delivery"},
        headers=headers,
    ).json()["id"]


def _ticket(client, headers, board_id, ticket_type="task"):
    return client.post(
        "/tickets",
        json={
            "board_id": board_id,
            "type": ticket_type,
            "title": "t",
            "actor": "owner",
        },
        headers=headers,
    ).json()


def _move(client, headers, ticket_id, to_status, note="because", actor="owner"):
    return client.post(
        f"/tickets/{ticket_id}/transition",
        json={"to_status": to_status, "actor": actor, "note": note},
        headers=headers,
    )


def test_transition_changes_status(client, headers, board_id):
    ticket = _ticket(client, headers, board_id)
    response = _move(client, headers, ticket["id"], "in_progress")
    assert response.status_code == 200
    assert response.json()["status"] == "in_progress"


def test_transition_requires_a_note(client, headers, board_id):
    ticket = _ticket(client, headers, board_id)
    response = client.post(
        f"/tickets/{ticket['id']}/transition",
        json={"to_status": "in_progress", "actor": "owner"},
        headers=headers,
    )
    assert response.status_code == 422


def test_transition_appends_history_with_from_and_to(client, headers, board_id):
    ticket = _ticket(client, headers, board_id)
    _move(client, headers, ticket["id"], "in_progress", note="starting")
    _move(client, headers, ticket["id"], "blocked", note="need a decision")

    entries = client.get(
        f"/tickets/{ticket['id']}/transitions", headers=headers
    ).json()["items"]
    assert [(e["from_status"], e["to_status"]) for e in entries] == [
        (None, "backlog"),
        ("backlog", "in_progress"),
        ("in_progress", "blocked"),
    ]
    assert entries[2]["note"] == "need a decision"


def test_epic_cannot_enter_agent_ready(client, headers, board_id):
    """422, not the 400 the plan's version of this test asserted.

    validate_ticket_status was deliberately moved to RequestValidationError in
    Task 3: a status outside a type's permitted set is a value wrong on its own
    terms, not a reference that fails because of another row. The plan's route
    already calls that validator, so the plan's own test contradicted its own
    implementation — see this PR's description.
    """
    epic = _ticket(client, headers, board_id, ticket_type="epic")
    response = _move(client, headers, epic["id"], "agent_ready")
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert "agent_ready" in str(body["error"])


def test_rejected_transition_points_at_to_status(client, headers, board_id):
    """The error's loc must name the field the caller sent — `to_status` here,
    not the `status` the create route sends. That is what the `field` argument
    on validate_ticket_status exists for."""
    epic = _ticket(client, headers, board_id, ticket_type="epic")
    body = _move(client, headers, epic["id"], "agent_ready").json()
    details = body["error"].get("details") or []
    assert any(detail.get("field") == "to_status" for detail in details), details


def test_epic_can_be_blocked(client, headers, board_id):
    epic = _ticket(client, headers, board_id, ticket_type="epic")
    assert _move(client, headers, epic["id"], "blocked").status_code == 200


def test_task_can_enter_agent_ready(client, headers, board_id):
    ticket = _ticket(client, headers, board_id)
    assert _move(client, headers, ticket["id"], "agent_ready").status_code == 200


def test_transition_to_unknown_status_is_422(client, headers, board_id):
    ticket = _ticket(client, headers, board_id)
    assert _move(client, headers, ticket["id"], "shipped").status_code == 422


def test_transition_repositions_into_the_destination_column(client, headers, board_id):
    first = _ticket(client, headers, board_id)
    second = _ticket(client, headers, board_id)
    _move(client, headers, first["id"], "review")
    moved = _move(client, headers, second["id"], "review").json()
    assert moved["position"] == 1


def test_failed_ticket_moves_to_blocked_not_agent_ready(client, headers, board_id):
    """The failure path: blocked takes a ticket out of the claim query's reach."""
    ticket = _ticket(client, headers, board_id)
    _move(client, headers, ticket["id"], "agent_ready")
    _move(client, headers, ticket["id"], "blocked", note="spec is ambiguous")
    listed = client.get("/tickets?status=agent_ready", headers=headers).json()
    assert listed["total"] == 0


# --- beyond the plan ---------------------------------------------------


def test_transition_to_the_same_status_keeps_position_and_still_logs(
    client, headers, board_id
):
    """A no-op move is still a real event with a reason, so it must appear in
    history — but it must not shuffle the ticket to the bottom of the column
    it is already in."""
    first = _ticket(client, headers, board_id)
    second = _ticket(client, headers, board_id)
    assert second["position"] == 1

    response = _move(client, headers, second["id"], "backlog", note="re-triaged")

    assert response.status_code == 200
    assert response.json()["position"] == 1
    entries = client.get(
        f"/tickets/{second['id']}/transitions", headers=headers
    ).json()["items"]
    assert [(e["from_status"], e["to_status"]) for e in entries] == [
        (None, "backlog"),
        ("backlog", "backlog"),
    ]


def test_a_rejected_transition_leaves_no_history_and_no_status_change(
    client, headers, board_id
):
    """The status change and its history row are committed together, so a
    rejected move must leave neither behind."""
    epic = _ticket(client, headers, board_id, ticket_type="epic")
    _move(client, headers, epic["id"], "agent_ready")

    assert (
        client.get(f"/tickets/{epic['id']}", headers=headers).json()["status"]
        == "backlog"
    )
    entries = client.get(
        f"/tickets/{epic['id']}/transitions", headers=headers
    ).json()["items"]
    assert len(entries) == 1


def test_transition_on_unknown_ticket_is_404(client, headers):
    assert _move(client, headers, "nope", "done").status_code == 404


def test_history_is_chronological_even_when_timestamps_tie(client, headers, board_id):
    """created_at has one-second resolution, so a burst of transitions shares a
    timestamp. History must still read oldest-first.

    Regression: ordering by (created_at, id) scrambled these, because id is a
    random uuid4 — a tiebreak that is fine for an arbitrary listing and wrong
    for an append-only log. Seven events make a wrong order essentially certain
    to be caught, where three would pass by chance one time in six.
    """
    ticket = _ticket(client, headers, board_id)
    # The read-back below is a single call, which is safe here and only
    # here: this fixture is the literal list under it -- eight transitions
    # including the create -- so it cannot cross a page. A history read
    # against a real ticket must drain, because an audit log grows without
    # bound and a capped read of it silently reports a partial history as
    # a complete one.
    sequence = [
        "in_progress",
        "agent_ready",
        "agent_coding",
        "review",
        "blocked",
        "in_progress",
        "done",
    ]
    for status in sequence:
        assert _move(client, headers, ticket["id"], status).status_code == 200

    entries = client.get(
        f"/tickets/{ticket['id']}/transitions?limit=200", headers=headers
    ).json()["items"]

    assert [entry["to_status"] for entry in entries] == ["backlog", *sequence]
    # Each row's from_status must be the previous row's to_status, or the log
    # describes a history that never happened.
    for earlier, later in zip(entries, entries[1:]):
        assert later["from_status"] == earlier["to_status"]


# --- a note has to say something (a todo) ----------------------------


@pytest.mark.parametrize("note", ["", "   ", "\t", "\n  \n"])
def test_transition_rejects_a_blank_note(client, headers, board_id, note):
    """The note was required in PRESENCE only.

    The design calls requiring it "what makes the audit trail trustworthy
    rather than aspirational" — but note="" returned 200 and wrote a history
    row that explains nothing, which is the aspirational version. A caller
    that does not want to explain itself could satisfy the rule and defeat
    the purpose in the same call.
    """
    ticket = _ticket(client, headers, board_id)
    response = client.post(
        f"/tickets/{ticket['id']}/transition",
        json={"to_status": "review", "actor": "owner", "note": note},
        headers=headers,
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert any(
        detail.get("field") == "note" for detail in body["error"].get("details", [])
    ), body["error"]


@pytest.mark.parametrize("actor", ["", "   "])
def test_transition_rejects_a_blank_actor(client, headers, board_id, actor):
    """Same reasoning. An unattributed history row is barely better than a
    missing one — "who moved this" is half of what the trail is for."""
    ticket = _ticket(client, headers, board_id)
    response = client.post(
        f"/tickets/{ticket['id']}/transition",
        json={"to_status": "review", "actor": actor, "note": "a real reason"},
        headers=headers,
    )

    assert response.status_code == 422


def test_a_blank_note_leaves_no_history_row(client, headers, board_id):
    """Rejected at validation, so the move never happens — neither the
    status change nor a half-written history row."""
    ticket = _ticket(client, headers, board_id)
    client.post(
        f"/tickets/{ticket['id']}/transition",
        json={"to_status": "review", "actor": "owner", "note": "  "},
        headers=headers,
    )

    history = client.get(
        f"/tickets/{ticket['id']}/transitions", headers=headers
    ).json()
    assert history["total"] == 1  # just the creation row
    assert (
        client.get(f"/tickets/{ticket['id']}", headers=headers).json()["status"]
        == "backlog"
    )


def test_a_note_is_stored_with_its_surrounding_whitespace_trimmed(
    client, headers, board_id
):
    """Trimmed rather than stored raw: the value that passed validation and
    the value that is stored should be the same string, or a later reader
    sees padding the check never considered."""
    ticket = _ticket(client, headers, board_id)
    client.post(
        f"/tickets/{ticket['id']}/transition",
        json={
            "to_status": "review",
            "actor": "  owner  ",
            "note": "  needs a second opinion  ",
        },
        headers=headers,
    )

    latest = client.get(
        f"/tickets/{ticket['id']}/transitions", headers=headers
    ).json()["items"][-1]
    assert latest["note"] == "needs a second opinion"
    assert latest["actor"] == "owner"


def test_a_one_character_note_is_still_accepted(client, headers, board_id):
    """The rule is "say something", not "say enough" — judging length would
    be the API deciding what counts as a good reason, which it cannot."""
    ticket = _ticket(client, headers, board_id)
    response = client.post(
        f"/tickets/{ticket['id']}/transition",
        json={"to_status": "review", "actor": "t", "note": "x"},
        headers=headers,
    )
    assert response.status_code == 200
