"""The board embeds summaries, not whole tickets (a todo).

`get_board` is the operating-mode entry point and has to stay one call, so
the payload cannot grow with the length of every description. Measured on
a working board: descriptions were most of the payload, and it grew by more
than half in a day — the ticket's own number went stale within a day.

Summaries plus per-column totals keep the one-call property while making
truncation impossible to hide.
"""

import pytest

from api.jyra import BOARD_COLUMN_CAP, TICKET_ATTACHMENT_CAP

SUMMARY_FIELDS = {
    "id",
    "key",
    "type",
    "title",
    "status",
    "position",
    "parent_id",
    "claimed_by",
}


def _auth(test_settings):
    return {"Authorization": f"Bearer {test_settings.api_token}"}


def _board(client, test_settings):
    entity = client.post(
        "/entities",
        json={"type": "project", "name": "Board size"},
        headers=_auth(test_settings),
    ).json()
    return client.post(
        "/boards",
        json={"entity_id": entity["id"], "title": "Sizes"},
        headers=_auth(test_settings),
    ).json()


def _ticket(client, test_settings, board_id, title, description="x" * 500):
    return client.post(
        "/tickets",
        json={
            "board_id": board_id,
            "type": "task",
            "title": title,
            "description": description,
            "actor": "tests",
        },
        headers=_auth(test_settings),
    ).json()


def test_the_board_embeds_summaries_without_descriptions(client, test_settings):
    board = _board(client, test_settings)
    _ticket(client, test_settings, board["id"], "One")

    columns = client.get(
        f"/boards/{board['id']}", headers=_auth(test_settings)
    ).json()["columns"]
    embedded = columns["backlog"]["items"][0]

    assert set(embedded) == SUMMARY_FIELDS
    assert "description" not in embedded


def test_the_description_is_still_reachable_through_get_ticket(
    client, test_settings
):
    """The contract change is *where* the description lives, not whether it
    exists. A model that cannot find it on the board must be able to get
    it in one more call, or this is data loss rather than a shape change.
    """
    board = _board(client, test_settings)
    created = _ticket(client, test_settings, board["id"], "One", description="hello")

    fetched = client.get(
        f"/tickets/{created['id']}", headers=_auth(test_settings)
    ).json()

    assert fetched["description"] == "hello"


def test_every_column_carries_its_total(client, test_settings):
    board = _board(client, test_settings)
    _ticket(client, test_settings, board["id"], "One")
    _ticket(client, test_settings, board["id"], "Two")

    columns = client.get(
        f"/boards/{board['id']}", headers=_auth(test_settings)
    ).json()["columns"]

    assert columns["backlog"]["total"] == 2
    # Present on empty columns too — a consumer should not have to handle
    # two shapes.
    assert columns["done"] == {"items": [], "total": 0}


@pytest.mark.parametrize("cap", [2])
def test_a_capped_column_says_so(client, test_settings, cap, monkeypatch):
    """The cap is 100 in production and would never fire in a test, so it
    is parametrized down to 2 — an untested cap is the same as no cap, and
    worse, because it reads as protection.
    """
    monkeypatch.setattr("api.jyra.BOARD_COLUMN_CAP", cap)
    board = _board(client, test_settings)
    for index in range(cap + 2):
        _ticket(client, test_settings, board["id"], f"Ticket {index}")

    backlog = client.get(
        f"/boards/{board['id']}", headers=_auth(test_settings)
    ).json()["columns"]["backlog"]

    assert len(backlog["items"]) == cap
    assert backlog["total"] == cap + 2
    assert backlog["total"] > len(backlog["items"]), "truncation must be visible"


def test_the_production_cap_is_generous_enough_not_to_fire_on_a_human_board(
    client, test_settings
):
    """A cap that engages on an ordinary board would be a silent data cut
    dressed as a safety feature."""
    assert BOARD_COLUMN_CAP >= 100


def test_attachments_are_capped_and_counted(client, test_settings, monkeypatch):
    """review-1's second instance: the get_ticket attachments embed had no
    paged path after list_attachments was retired."""
    monkeypatch.setattr("api.jyra.TICKET_ATTACHMENT_CAP", 2)
    board = _board(client, test_settings)
    ticket = _ticket(client, test_settings, board["id"], "With attachments")

    for index in range(4):
        client.post(
            f"/tickets/{ticket['id']}/attachments",
            files={"upload": (f"shot{index}.png", b"bytes", "image/png")},
            headers=_auth(test_settings),
        )

    fetched = client.get(
        f"/tickets/{ticket['id']}", headers=_auth(test_settings)
    ).json()

    assert len(fetched["attachments"]) == 2
    assert fetched["attachment_count"] == 4
    assert fetched["attachment_count"] > len(fetched["attachments"])


def test_the_board_payload_is_dominated_by_summaries_not_prose(
    client, test_settings
):
    """The property the change exists for, asserted rather than described.

    Twenty tickets with 2 KB descriptions would be ~40 KB embedded whole;
    as summaries the board stays a small fraction of that regardless of
    how long the descriptions are.
    """
    import json

    board = _board(client, test_settings)
    for index in range(20):
        _ticket(client, test_settings, board["id"], f"T{index}", description="x" * 2000)

    payload = client.get(f"/boards/{board['id']}", headers=_auth(test_settings)).text

    assert len(payload) < 8000, f"board payload is {len(payload)} bytes"
    assert json.loads(payload)["columns"]["backlog"]["total"] == 20
