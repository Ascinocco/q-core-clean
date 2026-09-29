"""Human-readable ticket keys such as KA-12 (ticket T-19).

A key is an alias: board key_prefix, a hyphen, the ticket's per-board
number. The UUID stays the primary id. These tests pin assignment, the
prefix rules, lookup by either form in any case on every ticket-taking
route, and that concurrent creates never share a number. The migration's
backfill is in test_jyra_keys_migration.py.
"""

import sqlite3
import threading

import pytest

from api.jyra import derive_key_prefix, resolve_ticket_id

TOKEN = {"Authorization": "Bearer test-token"}


def _entity(client, name="sunny hill bakery"):
    return client.post(
        "/entities", json={"type": "project", "name": name}, headers=TOKEN
    ).json()["id"]


def _board(client, name="sunny hill bakery", **extra):
    response = client.post(
        "/boards",
        json={"entity_id": _entity(client, name), "title": "Delivery", **extra},
        headers=TOKEN,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _ticket(client, board_id, **extra):
    payload = {"board_id": board_id, "type": "task", "title": "Do it", "actor": "owner"}
    payload.update(extra)
    response = client.post("/tickets", json=payload, headers=TOKEN)
    assert response.status_code == 200, response.text
    return response.json()


# --- prefixes ---------------------------------------------------------


def test_create_board_takes_a_prefix_and_upper_cases_it(client):
    board = _board(client, key_prefix="ka")
    assert board["key_prefix"] == "KA"
    assert client.get(f"/boards/{board['id']}", headers=TOKEN).json()["key_prefix"] == "KA"
    listed = client.get("/boards", headers=TOKEN).json()["items"]
    assert [b["key_prefix"] for b in listed] == ["KA"]


@pytest.mark.parametrize("bad", ["K", "1KA", "K-A", "KITEATLAS", "", "K A"])
def test_a_malformed_prefix_is_refused(client, bad):
    response = client.post(
        "/boards",
        json={"entity_id": _entity(client), "title": "D", "key_prefix": bad},
        headers=TOKEN,
    )
    assert response.status_code == 422


def test_a_prefix_already_in_use_is_a_conflict_in_any_case(client):
    _board(client, key_prefix="KA")
    response = client.post(
        "/boards",
        json={"entity_id": _entity(client), "title": "D", "key_prefix": "ka"},
        headers=TOKEN,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_an_absent_prefix_is_derived_from_the_entity_name(client):
    assert _board(client, "sunny hill bakery")["key_prefix"] == "SHB"
    assert _board(client, "Canvas")["key_prefix"] == "CAN"
    # Taken: numbered rather than refused.
    assert _board(client, "Canvas")["key_prefix"] == "CAN2"
    assert _board(client, "canvas")["key_prefix"] == "CAN3"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Kite Atlas", "KA"),
        ("Kite Foundry", "KF"),
        ("Toyota Corolla", "TC"),
        ("Canvas", "CAN"),
        ("workshop", "WOR"),
        ("KD", "KD"),
        ("X", "XB"),
        ("", "BB"),
        ("---", "BB"),
        ("2024 taxes", "B2T"),
        ("42", "B42"),
        ("a b c d e f g h", "ABCDEF"),
        ("café au lait", "CAL"),  # é is not ASCII, so it separates words
        ("Straße", "SE"),  # likewise ß: STRA, E
    ],
)
def test_derivation_rule(name, expected):
    assert derive_key_prefix(name, set()) == expected


def test_derivation_numbers_within_six_characters():
    taken = {"ABCDEF"} | {f"ABCDE{n}" for n in range(2, 10)}
    assert derive_key_prefix("a b c d e f", taken) == "ABCD10"


def test_the_prefix_changes_only_while_the_board_is_empty(client):
    board = _board(client)
    renamed = client.patch(
        f"/boards/{board['id']}", json={"key_prefix": "sh"}, headers=TOKEN
    )
    assert renamed.status_code == 200
    assert renamed.json()["key_prefix"] == "SH"

    _ticket(client, board["id"])
    refused = client.patch(
        f"/boards/{board['id']}", json={"key_prefix": "ZZ"}, headers=TOKEN
    )
    assert refused.status_code == 409
    assert "never had a ticket" in refused.json()["error"]["message"]
    # The title still changes on a board with tickets.
    assert client.patch(
        f"/boards/{board['id']}", json={"title": "New"}, headers=TOKEN
    ).status_code == 200


def test_update_board_refuses_another_boards_prefix_but_accepts_its_own(client):
    _board(client, key_prefix="KA")
    board = _board(client, "other", key_prefix="OT")
    taken = client.patch(f"/boards/{board['id']}", json={"key_prefix": "KA"}, headers=TOKEN)
    assert taken.status_code == 409
    same = client.patch(f"/boards/{board['id']}", json={"key_prefix": "ot"}, headers=TOKEN)
    assert same.status_code == 200


def test_a_prefix_cannot_be_cleared(client):
    board = _board(client)
    response = client.patch(
        f"/boards/{board['id']}", json={"key_prefix": None}, headers=TOKEN
    )
    assert response.status_code == 422


# --- assignment -------------------------------------------------------


def test_tickets_are_numbered_per_board_in_creation_order(client):
    ka = _board(client, key_prefix="KA")["id"]
    hl = _board(client, "workshop", key_prefix="KD")["id"]
    assert _ticket(client, ka)["key"] == "KA-1"
    assert _ticket(client, ka)["key"] == "KA-2"
    assert _ticket(client, hl)["key"] == "KD-1"
    third = _ticket(client, ka)
    assert (third["key"], third["number"]) == ("KA-3", 3)


def test_a_deleted_tickets_number_is_never_reused(client):
    board = _board(client, key_prefix="KA")["id"]
    _ticket(client, board)
    last = _ticket(client, board)
    assert client.delete(f"/tickets/{last['id']}", headers=TOKEN).status_code == 200
    assert _ticket(client, board)["key"] == "KA-3"


def test_a_refused_create_does_not_consume_a_number(client):
    board = _board(client, key_prefix="KA")["id"]
    refused = client.post(
        "/tickets",
        json={"board_id": board, "type": "task", "title": "x", "actor": "t",
              "parent_id": "KA-99"},
        headers=TOKEN,
    )
    assert refused.status_code == 400
    assert _ticket(client, board)["key"] == "KA-1"


def test_concurrent_creates_never_share_a_number(client, test_settings):
    """Eight connections creating at once, as eight requests would.

    Driven through the route function with a real connection each, so
    the transactions genuinely overlap rather than being serialized by a
    test client. Every create must succeed with a distinct number and the
    set must have no holes: nothing failed, so nothing is missing.
    """
    from api.jyra import create_ticket
    from api.models import TicketCreate

    board = _board(client, key_prefix="KA")["id"]
    workers, per_worker = 8, 10
    keys: list[str] = []
    errors: list[BaseException] = []
    start = threading.Barrier(workers)

    def work():
        connection = sqlite3.connect(
            test_settings.db_path, timeout=30, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            start.wait()
            for _ in range(per_worker):
                body = TicketCreate(board_id=board, type="task", title="t", actor="a")
                keys.append(create_ticket(body, connection=connection)["key"])
        except BaseException as exc:  # surfaced below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=work) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(keys, key=lambda k: int(k.split("-")[1])) == [
        f"KA-{n}" for n in range(1, workers * per_worker + 1)
    ]


# --- the key on every read -------------------------------------------


def test_every_ticket_read_carries_the_key(client):
    board = _board(client, key_prefix="KA")["id"]
    epic = _ticket(client, board, type="epic", title="Epic")
    child = _ticket(client, board, parent_id="ka-1")
    assert epic["key"] == "KA-1" and child["key"] == "KA-2"
    assert child["parent_id"] == epic["id"]

    detail = client.get("/tickets/KA-1", headers=TOKEN).json()
    assert detail["key"] == "KA-1"
    assert detail["children"] == [
        {"id": child["id"], "key": "KA-2", "type": "task", "title": "Do it", "status": "backlog"}
    ]

    listed = client.get(f"/tickets?board_id={board}", headers=TOKEN).json()["items"]
    assert {t["key"] for t in listed} == {"KA-1", "KA-2"}

    column = client.get(f"/boards/{board}", headers=TOKEN).json()["columns"]["backlog"]
    assert {t["key"] for t in column["items"]} == {"KA-1", "KA-2"}

    moved = client.post(
        "/tickets/ka-2/transition",
        json={"to_status": "agent_ready", "actor": "t", "note": "ready"},
        headers=TOKEN,
    ).json()
    assert moved["key"] == "KA-2"

    claimed = client.post("/tickets/claim", json={"actor": "a", "board_id": board}, headers=TOKEN)
    assert claimed.json()["key"] == "KA-2"

    history = client.get("/tickets/KA-2/transitions", headers=TOKEN).json()
    assert history["total"] == 3
    assert {entry["ticket_key"] for entry in history["items"]} == {"KA-2"}

    patched = client.patch("/tickets/Ka-2", json={"title": "Renamed"}, headers=TOKEN).json()
    assert (patched["key"], patched["title"]) == ("KA-2", "Renamed")


# --- lookup ------------------------------------------------------------


@pytest.mark.parametrize("ref", ["KA-1", "ka-1", "Ka-01", "{uuid}"])
def test_every_ticket_route_accepts_a_uuid_or_a_key_in_any_case(client, ref):
    board = _board(client, key_prefix="KA")["id"]
    ticket = _ticket(client, board)
    ref = ref.format(uuid=ticket["id"])

    assert client.get(f"/tickets/{ref}", headers=TOKEN).json()["id"] == ticket["id"]
    assert client.get(f"/tickets/{ref}/transitions", headers=TOKEN).status_code == 200
    assert client.patch(
        f"/tickets/{ref}", json={"description": "d"}, headers=TOKEN
    ).json()["id"] == ticket["id"]
    assert client.post(
        f"/tickets/{ref}/transition",
        json={"to_status": "in_progress", "actor": "t", "note": "go"},
        headers=TOKEN,
    ).json()["status"] == "in_progress"
    assert client.get(f"/tickets/{ref}/attachments", headers=TOKEN).json()["total"] == 0
    assert client.delete(f"/tickets/{ref}", headers=TOKEN).json() == {"deleted": True}
    assert client.get(f"/tickets/{ticket['id']}", headers=TOKEN).status_code == 404


@pytest.mark.parametrize("ref", ["KA-99", "ZZ-1", "ka-0", "KA-1234567890", "not-a-ticket"])
def test_an_unknown_reference_is_a_clean_not_found(client, ref):
    board = _board(client, key_prefix="KA")["id"]
    _ticket(client, board)
    for method, path in (
        ("GET", f"/tickets/{ref}"),
        ("GET", f"/tickets/{ref}/transitions"),
        ("DELETE", f"/tickets/{ref}"),
    ):
        response = client.request(method, path, headers=TOKEN)
        assert response.status_code == 404, (method, path)
        assert response.json()["error"]["code"] == "not_found"
        assert ref in response.json()["error"]["message"]


def test_parent_references_accept_keys_and_store_the_uuid(client):
    board = _board(client, key_prefix="KA")["id"]
    epic = _ticket(client, board, type="epic")
    other_epic = _ticket(client, board, type="epic")
    task = _ticket(client, board)

    moved = client.patch(f"/tickets/{task['id']}", json={"parent_id": "ka-2"}, headers=TOKEN)
    assert moved.json()["parent_id"] == other_epic["id"]

    listed = client.get("/tickets?parent_id=KA-2", headers=TOKEN).json()
    assert [t["key"] for t in listed["items"]] == ["KA-3"]

    unknown = client.get("/tickets?parent_id=KA-42", headers=TOKEN)
    assert unknown.status_code == 400
    assert unknown.json()["error"]["code"] == "invalid_reference"
    bad_parent = client.patch(
        f"/tickets/{task['id']}", json={"parent_id": "KA-42"}, headers=TOKEN
    )
    assert bad_parent.status_code == 400
    assert epic["key"] == "KA-1"


def test_an_attachment_uploaded_by_key_is_stored_under_the_uuid(client, test_settings):
    from pathlib import Path

    board = _board(client, key_prefix="KA")["id"]
    ticket = _ticket(client, board)
    response = client.post(
        "/tickets/ka-1/attachments",
        files={"upload": ("note.txt", b"hello")},
        headers=TOKEN,
    )
    assert response.status_code == 200, response.text
    assert response.json()["ticket_id"] == ticket["id"]
    stored = list(Path(test_settings.jyra_dir).iterdir())
    assert [p.name for p in stored] == [ticket["id"]]


def test_a_note_links_a_ticket_by_key_and_stores_the_uuid(client):
    board = _board(client, key_prefix="KA")["id"]
    ticket = _ticket(client, board)
    note = client.post(
        "/notes",
        json={"body": "# Plan", "links": [{"target_type": "ticket", "target_id": "ka-1"}]},
        headers=TOKEN,
    )
    assert note.status_code == 200, note.text
    assert note.json()["links"][0]["target_id"] == ticket["id"]
    # The link shows the key too, so it reads the way people name the ticket.
    assert note.json()["links"][0]["key"] == "KA-1"
    fetched = client.get(f"/notes/{note.json()['id']}", headers=TOKEN).json()
    assert fetched["links"][0]["key"] == "KA-1"
    by_key = client.get("/notes?target_id=KA-1", headers=TOKEN).json()
    assert [n["id"] for n in by_key["items"]] == [note.json()["id"]]
    missing = client.post(
        f"/notes/{note.json()['id']}/links",
        json={"target_type": "ticket", "target_id": "KA-9"},
        headers=TOKEN,
    )
    assert missing.status_code == 400
    # The shared resolver's message, not a drifted copy (review R3-F2).
    assert missing.json()["error"]["message"] == "No ticket with key 'KA-9'"


def test_resolve_ticket_id_never_treats_a_uuid_as_a_key(client, test_settings):
    board = _board(client, key_prefix="KA")["id"]
    ticket = _ticket(client, board)
    connection = sqlite3.connect(test_settings.db_path)
    try:
        assert resolve_ticket_id(connection, ticket["id"]) == ticket["id"]
        assert resolve_ticket_id(connection, "kA-1") == ticket["id"]
        assert resolve_ticket_id(connection, "KA-2") is None
    finally:
        connection.close()


# --- a key never comes to name a different ticket (review of #21, R1-F2) ---


def test_a_prefix_cannot_change_once_the_board_has_had_a_ticket(client):
    """The review's scenario: CAN-1 made and deleted, so the board is empty
    again -- but CAN-1 was seen, so the prefix must not be freed."""
    board = _board(client, "Canvas")
    assert board["key_prefix"] == "CAN"
    ticket = _ticket(client, board["id"])
    assert ticket["key"] == "CAN-1"
    client.delete(f"/tickets/{ticket['id']}", headers=TOKEN)

    refused = client.patch(f"/boards/{board['id']}", json={"key_prefix": "ZZ"}, headers=TOKEN)
    assert refused.status_code == 409
    assert "never had a ticket" in refused.json()["error"]["message"]
    # Re-asserting the prefix it already has is not a change, and is fine.
    same = client.patch(f"/boards/{board['id']}", json={"key_prefix": "can"}, headers=TOKEN)
    assert same.status_code == 200


def test_a_never_used_prefix_is_freed_by_a_rename(client):
    """No key was ever issued under it, so nothing needs protecting: a
    typo'd prefix can be corrected and taken back (XY -> XZ -> XY)."""
    board = _board(client, "other", key_prefix="XY")
    for prefix in ("XZ", "XY"):
        renamed = client.patch(
            f"/boards/{board['id']}", json={"key_prefix": prefix}, headers=TOKEN
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["key_prefix"] == prefix
    # And a freed derived prefix is derived again.
    canvas = _board(client, "Canvas")
    client.patch(f"/boards/{canvas['id']}", json={"key_prefix": "ZZ"}, headers=TOKEN)
    assert _board(client, "Canvas")["key_prefix"] == "CAN"


def test_deleting_a_never_used_board_frees_its_prefix(client):
    board = _board(client, "Canvas")
    assert client.delete(f"/boards/{board['id']}", headers=TOKEN).status_code == 200
    assert _board(client, "Canvas")["key_prefix"] == "CAN"


def test_a_deleted_boards_prefix_is_retired(client):
    board = _board(client, "Canvas")
    ticket = _ticket(client, board["id"])
    assert ticket["key"] == "CAN-1"
    client.delete(f"/tickets/{ticket['id']}", headers=TOKEN)
    assert client.delete(f"/boards/{board['id']}", headers=TOKEN).status_code == 200

    reborn = _board(client, "Canvas")
    assert reborn["key_prefix"] == "CAN2"
    first = _ticket(client, reborn["id"])
    assert first["key"] == "CAN2-1"
    # CAN-1 names nothing, rather than the new board's first ticket.
    assert client.get("/tickets/CAN-1", headers=TOKEN).status_code == 404
    explicit = client.post(
        "/boards",
        json={"entity_id": _entity(client), "title": "D", "key_prefix": "CAN"},
        headers=TOKEN,
    )
    assert explicit.status_code == 409
    assert "never reissued" in explicit.json()["error"]["message"]


def test_a_parent_deleted_between_resolve_and_read_is_a_400_not_a_500(client, monkeypatch):
    """R1-F1: the reference resolves, then the row is gone by the read.
    Simulated by resolving to an id that no longer exists."""
    import api.jyra

    board = _board(client, key_prefix="KA")["id"]
    task = _ticket(client, board)
    _ticket(client, board, type="epic")
    real = api.jyra.resolve_ticket_id
    monkeypatch.setattr(
        api.jyra,
        "resolve_ticket_id",
        lambda connection, ref: "gone" if ref == "KA-2" else real(connection, ref),
    )
    response = client.patch(
        f"/tickets/{task['id']}", json={"parent_id": "KA-2"}, headers=TOKEN
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_reference"
