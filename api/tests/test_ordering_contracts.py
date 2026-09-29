"""Seven tool descriptions claim an order. Each is pinned to its query.

"newest first" is the cheapest kind of claim to get wrong and the most
expensive to notice: a flipped `ORDER BY` raises nothing, returns the
right rows, and is invisible to every caller who believed the sentence.
The first page of a paginated read is then the wrong end of the set, and
a model summarising "the latest transactions" summarises the oldest.

The #107 shape, applied seven times through one helper rather than seven
idioms:

    contract   -- one phrase beside the query that produces the order
    pinned     -- exact match, so ADDING a clause is caught too, not just
                  dropping one (review-1's finding on #107: a lower bound
                  with no upper bound lets a false claim through)
    rendered   -- the tool description interpolates it
    behaviour  -- rows actually come back that way

All four must hold. The contract alone would be a constant that can go
stale beside its code; the behavioural test alone would leave the
description free to describe something else.
"""

from tests.source_import_support import seed_source_batch

import sqlite3

import pytest

from api.artifact_links import LIST_ARTIFACT_LINKS_ORDER
from api.documents import LIST_DOCUMENTS_ORDER
from api.due import DUE_ORDER
from api.entities import LIST_ENTITIES_ORDER
from api.financial import LIST_TRANSACTIONS_ORDER, RULE_HISTORY_ORDER
from api.jyra import TICKET_HISTORY_ORDER
from api.reminders import LIST_REMINDERS_ORDER

#: Exact pins. Any reword forces a deliberate update here, next to the
#: behavioural test that says what the phrase has to mean.
EXPECTED = {
    "LIST_DOCUMENTS_ORDER": (LIST_DOCUMENTS_ORDER, "newest first"),
    "LIST_TRANSACTIONS_ORDER": (LIST_TRANSACTIONS_ORDER, "newest first"),
    "RULE_HISTORY_ORDER": (
        RULE_HISTORY_ORDER,
        "oldest first; two changes in the same second come back in "
        "field order, not in the order they were made",
    ),
    "LIST_ENTITIES_ORDER": (LIST_ENTITIES_ORDER, "by name, case-insensitive"),
    "TICKET_HISTORY_ORDER": (TICKET_HISTORY_ORDER, "oldest first"),
    "LIST_REMINDERS_ORDER": (LIST_REMINDERS_ORDER, "by start_date"),
    "DUE_ORDER": (DUE_ORDER, "by due_date, soonest first"),
    "LIST_ARTIFACT_LINKS_ORDER": (LIST_ARTIFACT_LINKS_ORDER, "oldest first"),
}


def _auth(settings):
    return {"Authorization": f"Bearer {settings.api_token}"}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_ordering_contract_is_pinned_exactly(name):
    """Exact, not "mentions the word". See #107: asserting a phrase is
    CONTAINED lets a clause be added silently, and an addition is the edit
    that reaches a description without passing through anything."""
    actual, expected = EXPECTED[name]

    assert actual == expected, (
        f"{name} changed. Update this pin only after checking the "
        "behavioural test below still says what the new phrase means."
    )


def test_every_ordering_contract_reaches_its_description(test_settings):
    """The rendered descriptions, not the source — an f-string can be
    right in the file and wrong once built."""
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
        "list_documents": LIST_DOCUMENTS_ORDER,
        "list_transactions": LIST_TRANSACTIONS_ORDER,
        "merchant_rule_history": RULE_HISTORY_ORDER,
        "list_entities": LIST_ENTITIES_ORDER,
        "get_ticket_history": TICKET_HISTORY_ORDER,
        "list_reminders": LIST_REMINDERS_ORDER,
        "list_due_items": DUE_ORDER,
    }
    missing = [tool for tool, phrase in pairs.items() if phrase not in tools[tool]]

    assert missing == [], f"these claim an order without carrying its contract: {missing}"


# --- behaviour: the rows actually come back that way ----------------------


def test_entities_come_back_by_name_case_insensitively(client, test_settings):
    headers = _auth(test_settings)
    # These three DISCRIMINATE. My first set was zebra/Apple/mango, which
    # sorts identically under binary and NOCASE collation -- so dropping
    # COLLATE NOCASE left the test green. Found by mutating rather than by
    # reading it, which is the only way that kind of hole shows up.
    #
    #   binary: Banana, apple, cherry     (uppercase sorts first)
    #   NOCASE: apple, Banana, cherry
    for name in ("cherry", "apple", "Banana"):
        client.post("/entities", json={"type": "pet", "name": name}, headers=headers)

    names = [
        e["name"]
        for e in client.get("/entities", params={"type": "pet"}, headers=headers).json()["items"]
    ]

    assert names == ["apple", "Banana", "cherry"], (
        "case-insensitive by name; a plain ORDER BY name puts Banana first"
    )


def test_transactions_come_back_newest_first(client, test_settings):
    headers = _auth(test_settings)
    account = client.post(
        "/entities", json={"type": "account", "name": "Chequing"}, headers=headers
    ).json()
    seed_source_batch(client, json={
            "account_id": account["id"], "period_start": "2026-06-01",
            "period_end": "2026-06-30",
            "transactions": [
                {"txn_date": "2026-06-01", "description": "FIRST", "amount_cents": -100},
                {"txn_date": "2026-06-20", "description": "LAST", "amount_cents": -300},
                {"txn_date": "2026-06-10", "description": "MIDDLE", "amount_cents": -200},
            ],
        }, headers=headers)

    rows = client.get("/transactions", headers=headers).json()["items"]

    assert [r["description"] for r in rows] == ["LAST", "MIDDLE", "FIRST"]


def test_reminders_come_back_by_start_date(client, test_settings):
    headers = _auth(test_settings)
    for title, start in (("later", "2026-09-01"), ("sooner", "2026-07-01")):
        client.post(
            "/reminders", json={"title": title, "start_date": start}, headers=headers
        )

    titles = [r["title"] for r in client.get("/reminders", headers=headers).json()["items"]]

    assert titles == ["sooner", "later"]


def test_rule_history_comes_back_oldest_first(client, test_settings):
    """Timestamps set explicitly, because the obvious version does not test
    what it looks like it tests.

    Creating a rule and immediately patching it puts both history rows in
    the SAME second, so `changed_at` ties and the secondary `field` key
    decides — NULL (the creation) before 'pattern'. Reversing `changed_at`
    to DESC then changes nothing and the test stays green. Found by
    mutation; the first version of this test passed against a deliberately
    reversed ORDER BY.

    It is also a real property worth knowing rather than a test artefact:
    "oldest first" holds to SECOND granularity. Two changes inside one
    second are ordered by field name, not by time.
    """
    headers = _auth(test_settings)
    category = client.get("/categories", headers=headers).json()["items"][0]
    rule = client.post(
        "/merchant_rules",
        json={"pattern": "FIRST", "category_id": category["id"], "actor": "impl-4"},
        headers=headers,
    ).json()
    client.patch(
        f"/merchant_rules/{rule['id']}",
        json={"pattern": "SECOND", "actor": "impl-4"},
        headers=headers,
    )

    # Force distinct seconds so `changed_at` actually decides the order,
    # and make the LATER row the creation-shaped one so a DESC flip is
    # unambiguous rather than masked by the field tie-break.
    connection = sqlite3.connect(test_settings.db_path)
    try:
        connection.execute(
            "UPDATE merchant_rule_changes SET changed_at = '2026-01-01 00:00:00' "
            "WHERE field IS NULL"
        )
        connection.execute(
            "UPDATE merchant_rule_changes SET changed_at = '2026-06-01 00:00:00' "
            "WHERE field IS NOT NULL"
        )
        connection.commit()
    finally:
        connection.close()

    rows = client.get(
        f"/merchant_rules/{rule['id']}/history", headers=headers
    ).json()["items"]

    assert [r["changed_at"] for r in rows] == [
        "2026-01-01 00:00:00",
        "2026-06-01 00:00:00",
    ], "oldest first, decided by changed_at rather than by the field tie-break"


def test_ticket_history_comes_back_oldest_first(client, test_settings):
    headers = _auth(test_settings)
    entity = client.post(
        "/entities", json={"type": "project", "name": "P"}, headers=headers
    ).json()
    board = client.post(
        "/boards", json={"entity_id": entity["id"], "title": "B"}, headers=headers
    ).json()
    ticket = client.post(
        "/tickets",
        json={"board_id": board["id"], "title": "T", "type": "task", "actor": "impl-4"},
        headers=headers,
    ).json()
    client.post(
        f"/tickets/{ticket['id']}/transition",
        json={"to_status": "in_progress", "actor": "impl-4", "note": "start"},
        headers=headers,
    )

    rows = client.get(
        f"/tickets/{ticket['id']}/transitions", headers=headers
    ).json()["items"]

    assert rows[0]["to_status"] == "backlog", "creation first"
    assert rows[-1]["to_status"] == "in_progress", "the later move last"


def test_documents_come_back_newest_first(client, test_settings):
    """Rows inserted directly: the claim under test is the endpoint's
    ORDER BY, and registering three real files to exercise it would test
    the intake path instead."""
    # One request first: the schema is installed on first use, so opening
    # the file before any call finds an empty database. The failure reads
    # as "no such table: documents", which looks like a schema bug rather
    # than a test that got ahead of itself.
    assert client.get("/documents", headers=_auth(test_settings)).status_code == 200

    connection = sqlite3.connect(test_settings.db_path)
    try:
        for index, stamp in enumerate(
            ("2026-01-01 00:00:00", "2026-03-01 00:00:00", "2026-02-01 00:00:00")
        ):
            connection.execute(
                "INSERT INTO documents "
                "(id, title, doc_type, file_path, content_hash, imported_at) "
                "VALUES (?, ?, 'other', ?, ?, ?)",
                (f"d{index}", f"doc {stamp[:7]}", f"/tmp/d{index}", f"h{index}", stamp),
            )
        connection.commit()
    finally:
        connection.close()

    rows = client.get("/documents", headers=_auth(test_settings)).json()["items"]

    assert [r["title"] for r in rows] == ["doc 2026-03", "doc 2026-02", "doc 2026-01"]
