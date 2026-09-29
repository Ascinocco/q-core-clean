import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

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


def _ready_ticket(client, headers, board_id, title="t"):
    ticket = client.post(
        "/tickets",
        json={"board_id": board_id, "type": "task", "title": title, "actor": "owner"},
        headers=headers,
    ).json()
    client.post(
        f"/tickets/{ticket['id']}/transition",
        json={"to_status": "agent_ready", "actor": "owner", "note": "specced"},
        headers=headers,
    )
    return ticket["id"]


def test_claim_returns_the_ticket_and_marks_it_coding(client, headers, board_id):
    ticket_id = _ready_ticket(client, headers, board_id)
    response = client.post("/tickets/claim", json={"actor": "agent-1"}, headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == ticket_id
    assert body["status"] == "agent_coding"
    assert body["claimed_by"] == "agent-1"
    assert body["claimed_at"] is not None


def test_claim_writes_a_transition(client, headers, board_id):
    ticket_id = _ready_ticket(client, headers, board_id)
    client.post("/tickets/claim", json={"actor": "agent-1"}, headers=headers)
    entries = client.get(f"/tickets/{ticket_id}/transitions", headers=headers).json()[
        "items"
    ]
    assert entries[-1]["from_status"] == "agent_ready"
    assert entries[-1]["to_status"] == "agent_coding"
    assert entries[-1]["actor"] == "agent-1"


def test_second_claim_on_an_empty_column_returns_204(client, headers, board_id):
    _ready_ticket(client, headers, board_id)
    assert (
        client.post("/tickets/claim", json={"actor": "a"}, headers=headers).status_code
        == 200
    )
    assert (
        client.post("/tickets/claim", json={"actor": "b"}, headers=headers).status_code
        == 204
    )


def test_claim_ignores_blocked_and_backlog_tickets(client, headers, board_id):
    client.post(
        "/tickets",
        json={
            "board_id": board_id,
            "type": "task",
            "title": "backlog",
            "actor": "owner",
        },
        headers=headers,
    )
    response = client.post("/tickets/claim", json={"actor": "a"}, headers=headers)
    assert response.status_code == 204


def test_claim_respects_position_order(client, headers, board_id):
    _ready_ticket(client, headers, board_id, title="first")
    second = _ready_ticket(client, headers, board_id, title="second")
    client.patch(f"/tickets/{second}", json={"position": -1}, headers=headers)
    claimed = client.post("/tickets/claim", json={"actor": "a"}, headers=headers).json()
    assert claimed["id"] == second


def test_claim_can_be_scoped_to_one_board(client, headers, board_id):
    _ready_ticket(client, headers, board_id)
    other_entity = client.post(
        "/entities", json={"type": "project", "name": "other"}, headers=headers
    ).json()
    other_board = client.post(
        "/boards",
        json={"entity_id": other_entity["id"], "title": "Other"},
        headers=headers,
    ).json()["id"]
    response = client.post(
        "/tickets/claim", json={"actor": "a", "board_id": other_board}, headers=headers
    )
    assert response.status_code == 204


def test_conditional_update_lets_exactly_one_writer_win(
    client, headers, board_id, test_settings
):
    """The concurrency guarantee at the SQL level: two connections attempt the
    same conditional UPDATE and exactly one sees rowcount 1.

    Deterministic by construction — the second writer arrives after the first
    has committed, which is the case that must be impossible to get wrong.
    test_concurrent_claims_never_hand_out_the_same_ticket covers the genuinely
    simultaneous case.
    """
    ticket_id = _ready_ticket(client, headers, board_id)
    statement = (
        "UPDATE tickets SET status = 'agent_coding', claimed_by = ? "
        "WHERE id = ? AND status = 'agent_ready'"
    )
    first = sqlite3.connect(test_settings.db_path)
    second = sqlite3.connect(test_settings.db_path)
    try:
        rows_first = first.execute(statement, ("agent-1", ticket_id)).rowcount
        first.commit()
        rows_second = second.execute(statement, ("agent-2", ticket_id)).rowcount
        second.commit()
    finally:
        first.close()
        second.close()
    assert (rows_first, rows_second) == (1, 0)


def test_stale_claims_are_listable(client, headers, board_id, test_settings):
    ticket_id = _ready_ticket(client, headers, board_id)
    client.post("/tickets/claim", json={"actor": "agent-1"}, headers=headers)
    connection = sqlite3.connect(test_settings.db_path)
    try:
        connection.execute(
            "UPDATE tickets SET claimed_at = '2000-01-01 00:00:00' WHERE id = ?",
            (ticket_id,),
        )
        connection.commit()
    finally:
        connection.close()

    # Date-only cutoff: SQLite compares these as strings, and
    # '2000-01-01 00:00:00' < '2020-01-01' lexicographically.
    response = client.get(
        "/tickets?status=agent_coding&claimed_before=2020-01-01", headers=headers
    )
    assert response.json()["total"] == 1


# --- beyond the plan ---------------------------------------------------


def test_concurrent_claims_never_hand_out_the_same_ticket(client, headers, board_id):
    """The property the whole agent-loop model rests on, under real threads.

    The plan argues a threaded test would be flaky without proving more. It is
    not flaky as written here, because the assertion is about an invariant
    rather than about timing: however the eight callers interleave, the three
    tickets must be handed out at most once each and the rest must get 204.
    Timing changes which caller wins, never how many do.

    This is what the sequential SQL-level test cannot reach — real concurrent
    connections going through the route, WAL, and the busy timeout.
    """
    ticket_ids = {
        _ready_ticket(client, headers, board_id, title=f"t{n}") for n in range(3)
    }
    barrier = threading.Barrier(8)

    def claim(n: int):
        barrier.wait()
        response = client.post(
            "/tickets/claim", json={"actor": f"agent-{n}"}, headers=headers
        )
        return response.status_code, response.json() if response.content else None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(8)))

    won = [body for status, body in results if status == 200]
    empty = [status for status, _ in results if status == 204]

    assert len(won) == 3, f"expected 3 winners, got {len(won)}"
    assert len(empty) == 5
    assert {body["id"] for body in won} == ticket_ids, "a ticket was handed out twice"
    assert len({body["claimed_by"] for body in won}) == 3, "two agents share a claim"
    # Every winner is genuinely claimed, not merely returned.
    for body in won:
        assert body["status"] == "agent_coding"
        assert body["claimed_at"] is not None


def test_every_claim_leaves_exactly_one_transition(client, headers, board_id):
    """A claim and its history row commit together, so concurrency must not
    produce a ticket that is claimed without a matching audit entry — or two
    entries for one claim."""
    ticket_ids = [
        _ready_ticket(client, headers, board_id, title=f"t{n}") for n in range(3)
    ]
    barrier = threading.Barrier(6)

    def claim(n: int):
        barrier.wait()
        return client.post(
            "/tickets/claim", json={"actor": f"agent-{n}"}, headers=headers
        ).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(claim, range(6)))

    for ticket_id in ticket_ids:
        entries = client.get(
            f"/tickets/{ticket_id}/transitions", headers=headers
        ).json()["items"]
        claims = [e for e in entries if e["to_status"] == "agent_coding"]
        assert len(claims) == 1, f"{ticket_id} has {len(claims)} claim transitions"
        assert claims[0]["from_status"] == "agent_ready"


def test_claim_route_is_not_shadowed_by_the_ticket_id_route(client, headers, board_id):
    """`/tickets/claim` path-matches `/tickets/{ticket_id}`. Verified rather
    than reasoned about: if the literal route ever lost, this would 404 or 422
    with `claim` parsed as an id."""
    _ready_ticket(client, headers, board_id)
    response = client.post("/tickets/claim", json={"actor": "a"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["claimed_by"] == "a"


def test_claimed_before_ignores_unclaimed_tickets(client, headers, board_id):
    """claimed_at IS NULL must not satisfy a `claimed_before` cutoff — in
    SQLite, NULL comparisons are NULL rather than true, but relying on that
    implicitly would break if the clause were ever rewritten."""
    _ready_ticket(client, headers, board_id)
    response = client.get(
        "/tickets?status=agent_ready&claimed_before=2100-01-01", headers=headers
    )
    assert response.json()["total"] == 0
