"""Eight tool descriptions state behaviour. Each is pinned to its code.

Part 2 of ticket T-31, the same shape as the matcher contract (#107) and the
ordering contracts (#111): a phrase beside the code that makes it true, an
EXACT pin on it, the rendered description asserted to carry it, and a
behavioural test exercising the claim.

Exact rather than "contains", from the start. review-1's finding on #107
is the reason: a lower bound catches a dropped clause and a changed
behaviour, and misses an ADDED one — and an addition is the edit that
reaches a description without passing through anything, because
interpolation propagates it for free.
"""

from tests.source_import_support import seed_source_batch

import sqlite3

import pytest

from api.entities import RELATIONSHIP_DEDUP_CONTRACT
from api.financial import NULL_BUCKET_CONTRACT, TRANSFERS_CONTRACT
from api.jyra import DELETE_TICKET_CONTRACT, TRANSITION_ATOMICITY_CONTRACT
from api.models import (
    AMOUNT_CENTS_CONTRACT,
    TICKET_UPDATE_CONTRACT,
    TRANSITION_NOTE_CONTRACT,
)

EXPECTED = {
    "TRANSITION_NOTE_CONTRACT": (
        TRANSITION_NOTE_CONTRACT, "note and actor are required and must not be blank"),
    "TICKET_UPDATE_CONTRACT": (TICKET_UPDATE_CONTRACT, "cannot change status"),
    "AMOUNT_CENTS_CONTRACT": (
        AMOUNT_CENTS_CONTRACT,
        "integer cents; a fractional value is rejected, not rounded"),
    "TRANSITION_ATOMICITY_CONTRACT": (
        TRANSITION_ATOMICITY_CONTRACT,
        "the history row is written in the same transaction as the move"),
    "DELETE_TICKET_CONTRACT": (
        DELETE_TICKET_CONTRACT, "a ticket with children is refused"),
    "RELATIONSHIP_DEDUP_CONTRACT": (
        RELATIONSHIP_DEDUP_CONTRACT,
        "an identical duplicate returns the existing relationship and creates "
        "nothing, and one differing in dates or attributes is refused"),
    "TRANSFERS_CONTRACT": (
        TRANSFERS_CONTRACT,
        "transfers between the owner's own accounts are excluded by default"),
    "NULL_BUCKET_CONTRACT": (
        NULL_BUCKET_CONTRACT,
        "the null-bucket total is uncategorized_cents when grouping by "
        "category and unattributed_cents when grouping by entity; exactly "
        "one appears"),
}


def _auth(settings):
    return {"Authorization": f"Bearer {settings.api_token}"}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_claim_contract_is_pinned_exactly(name):
    actual, expected = EXPECTED[name]

    assert actual == expected, (
        f"{name} changed. Update this pin only after checking the "
        "behavioural test below still says what the new phrase means."
    )


def test_every_claim_contract_reaches_its_description(test_settings):
    """The rendered descriptions, not the source."""
    import asyncio

    import httpx

    from q_core_mcp.client import QCoreClient
    from q_core_mcp.server import build_server

    server = build_server(
        QCoreClient(
            test_settings,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        )
    )
    tools = {t.name: (t.description or "") for t in asyncio.run(server.list_tools())}

    pairs = {
        "transition_ticket": [TRANSITION_NOTE_CONTRACT, TRANSITION_ATOMICITY_CONTRACT],
        "update_ticket": [TICKET_UPDATE_CONTRACT],
        "delete_ticket": [DELETE_TICKET_CONTRACT],
        "preview_source_import": [AMOUNT_CENTS_CONTRACT],
        "spending_summary": [TRANSFERS_CONTRACT, NULL_BUCKET_CONTRACT],
        "create_relationship": [RELATIONSHIP_DEDUP_CONTRACT],
    }
    missing = {
        tool: [c for c in contracts if c not in tools[tool]]
        for tool, contracts in pairs.items()
        if any(c not in tools[tool] for c in contracts)
    }

    assert missing == {}, f"descriptions missing their contract: {missing}"


# --- behaviour ------------------------------------------------------------


@pytest.fixture()
def ticket(client, test_settings):
    headers = _auth(test_settings)
    entity = client.post(
        "/entities", json={"type": "project", "name": "P"}, headers=headers
    ).json()
    board = client.post(
        "/boards", json={"entity_id": entity["id"], "title": "B"}, headers=headers
    ).json()
    # An `epic`, not a `task`: a parent must strictly outrank its child,
    # so a task cannot parent anything. The first version of this fixture
    # made a task and the child POST failed silently -- `children` was 0,
    # the delete succeeded, and the test failed for a reason that had
    # nothing to do with the rule it was checking.
    response = client.post(
        "/tickets",
        json={"board_id": board["id"], "title": "T", "type": "epic", "actor": "impl-4"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return {"headers": headers, "board": board, "ticket": response.json()}


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_note_or_actor_is_refused(client, ticket, blank):
    for field in ("note", "actor"):
        body = {"to_status": "in_progress", "actor": "impl-4", "note": "why"}
        body[field] = blank
        response = client.post(
            f"/tickets/{ticket['ticket']['id']}/transition",
            json=body, headers=ticket["headers"],
        )
        assert response.status_code == 422, f"{field}={blank!r} was accepted"


def test_update_ticket_cannot_change_status(client, ticket):
    response = client.patch(
        f"/tickets/{ticket['ticket']['id']}",
        json={"status": "done"}, headers=ticket["headers"],
    )

    assert response.status_code == 422, "a status field must be rejected, not ignored"


def test_a_transition_and_its_history_row_cannot_come_apart(
    client, ticket, monkeypatch
):
    """Force the history write to fail; the move must roll back."""
    import api.jyra as jyra

    monkeypatch.setattr(
        jyra, "_record_transition",
        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("boom")),
    )

    client.post(
        f"/tickets/{ticket['ticket']['id']}/transition",
        json={"to_status": "in_progress", "actor": "impl-4", "note": "why"},
        headers=ticket["headers"],
    )

    after = client.get(
        f"/tickets/{ticket['ticket']['id']}", headers=ticket["headers"]
    ).json()
    assert after["status"] == "backlog", "the move survived its failed history row"


def test_a_ticket_with_children_is_refused(client, ticket):
    headers = ticket["headers"]
    child = client.post(
        "/tickets",
        json={
            "board_id": ticket["board"]["id"], "title": "child", "type": "task",
            "parent_id": ticket["ticket"]["id"], "actor": "impl-4",
        },
        headers=headers,
    )
    # Asserted, not assumed: an unchecked setup POST is how this test
    # first passed its delete for the wrong reason.
    assert child.status_code == 200, child.text

    response = client.delete(f"/tickets/{ticket['ticket']['id']}", headers=headers)

    # 400 with code "conflict", not 409 — I assumed 409 and was wrong about
    # the API, not the rule. Asserting the CODE as well as the status, so
    # this cannot pass on some other 400 (a bad id, a missing field) and
    # report the child rule as enforced.
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "conflict"
    assert "child" in response.json()["error"]["message"]


@pytest.mark.parametrize("value", [2.5, 2.0, "250"])
def test_a_non_integer_amount_is_rejected(client, test_settings, value):
    """2.0 and "250" too, not just 2.5 — StrictInt refuses a lossless float
    and a numeric string, which a plain int would silently accept."""
    headers = _auth(test_settings)
    account = client.post(
        "/entities", json={"type": "account", "name": "A"}, headers=headers
    ).json()

    response = seed_source_batch(client, json={
            "account_id": account["id"], "period_start": "2026-07-01",
            "period_end": "2026-07-31",
            "transactions": [
                {"txn_date": "2026-07-02", "description": "X", "amount_cents": value}
            ],
        }, headers=headers)

    assert response.status_code == 422, f"amount_cents={value!r} was accepted"


def test_a_duplicate_relationship_returns_the_existing_one(client, test_settings):
    headers = _auth(test_settings)
    a = client.post(
        "/entities", json={"type": "person", "name": "A"}, headers=headers
    ).json()
    b = client.post(
        "/entities", json={"type": "property", "name": "B"}, headers=headers
    ).json()
    payload = {"to_entity_id": b["id"], "relationship_type": "owns"}

    first = client.post(
        f"/entities/{a['id']}/relationships", json=payload, headers=headers
    ).json()
    second = client.post(
        f"/entities/{a['id']}/relationships", json=payload, headers=headers
    ).json()

    assert second["id"] == first["id"], "a second row was created"
    listed = client.get(
        f"/entities/{a['id']}/relationships", headers=headers
    ).json()
    assert listed["total"] == 1


def test_the_null_bucket_field_tracks_group_by(client, test_settings):
    """Exactly one, and its name follows what "not grouped" means."""
    headers = _auth(test_settings)

    by_category = client.get("/spending_summary", headers=headers).json()
    by_entity = client.get(
        "/spending_summary", params={"group_by": "entity"}, headers=headers
    ).json()

    assert "uncategorized_cents" in by_category
    assert "unattributed_cents" not in by_category
    assert "unattributed_cents" in by_entity
    assert "uncategorized_cents" not in by_entity, (
        "two different nulls in one response is the regression #101 fixed"
    )
