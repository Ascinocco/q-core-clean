"""Every change to a merchant rule is recorded, or the change does not happen.

A merchant rule decides how spending is classified. An unrecorded
correction to one is indistinguishable from the rule always having said
that — so "who changed this and from what" is not bookkeeping here, it is
the difference between a total you can defend and a total you can only
assert.

The tests that matter most are the ones asserting a change and its record
cannot come apart. A trail that is usually written is not a trail.
"""

from tests.source_import_support import seed_source_batch

import sqlite3

import pytest


def _auth(settings):
    return {"Authorization": f"Bearer {settings.api_token}"}


def _rows(settings, rule_id=None):
    connection = sqlite3.connect(settings.db_path)
    try:
        connection.row_factory = sqlite3.Row
        sql = "SELECT * FROM merchant_rule_changes"
        params: tuple = ()
        if rule_id is not None:
            sql += " WHERE rule_id = ?"
            params = (rule_id,)
        sql += " ORDER BY changed_at, field"
        return [dict(r) for r in connection.execute(sql, params)]
    finally:
        connection.close()


@pytest.fixture()
def rule(client, test_settings):
    category = client.get("/categories", headers=_auth(test_settings)).json()["items"][0]
    created = client.post(
        "/merchant_rules",
        json={"pattern": "MAPLE DONUTS", "category_id": category["id"], "actor": "impl-4"},
        headers=_auth(test_settings),
    )
    assert created.status_code == 200, created.text
    return {"rule": created.json(), "category": category}


# --- the three writers the ticket names -----------------------------------


def test_creating_a_rule_records_who_created_it(client, test_settings, rule):
    rows = _rows(test_settings, rule["rule"]["id"])

    assert len(rows) == 1
    assert rows[0]["actor"] == "impl-4"
    assert rows[0]["field"] is None, "a creation is not a change TO a field"
    assert rows[0]["old_value"] is None


def test_updating_records_one_row_per_changed_field(client, test_settings, rule):
    response = client.patch(
        f"/merchant_rules/{rule['rule']['id']}",
        json={"pattern": "MAPLE DONUTS #", "entity_id": None, "actor": "review-1"},
        headers=_auth(test_settings),
    )
    assert response.status_code == 200, response.text

    changes = [r for r in _rows(test_settings, rule["rule"]["id"]) if r["field"]]
    assert [r["field"] for r in changes] == ["pattern"]
    assert changes[0]["old_value"] == "MAPLE DONUTS"
    assert changes[0]["new_value"] == "MAPLE DONUTS #"
    assert changes[0]["actor"] == "review-1"


def test_a_field_set_to_the_value_it_already_had_records_nothing(
    client, test_settings, rule
):
    """Only CHANGED fields. A row saying a value went from X to X is noise
    in the one place noise is most expensive: it pads the trail someone
    reads when they are trying to find the change that mattered."""
    client.patch(
        f"/merchant_rules/{rule['rule']['id']}",
        json={"pattern": "MAPLE DONUTS", "actor": "review-1"},
        headers=_auth(test_settings),
    )

    changes = [r for r in _rows(test_settings, rule["rule"]["id"]) if r["field"]]
    assert changes == []


def test_deleting_a_rule_leaves_its_history_behind(client, test_settings, rule):
    """The deletion row must outlive the rule.

    This is why `rule_id` carries no REFERENCES clause: a foreign key
    would either forbid the delete or take the history with it, and in
    both cases the trail could not record the one event it most needs to.
    """
    rule_id = rule["rule"]["id"]

    response = client.delete(
        f"/merchant_rules/{rule_id}?actor=impl-4", headers=_auth(test_settings)
    )
    assert response.status_code == 200, response.text

    rows = _rows(test_settings, rule_id)
    assert len(rows) == 2, "creation and deletion both survive the rule"
    assert rows[-1]["actor"] == "impl-4"


def test_apply_as_rule_is_recorded_too(client, test_settings):
    """The fourth writer, which an endpoint-by-endpoint reading misses.

    `apply_as_rule` is a second creation path. It is the writer the
    source-reading guard exists to catch, and the reason a checklist of
    POST/PATCH/DELETE would have shipped a hole.
    """
    headers = _auth(test_settings)
    category = client.get("/categories", headers=headers).json()["items"][0]
    account = client.post(
        "/entities", json={"type": "account", "name": "Visa"}, headers=headers
    ).json()
    seed_source_batch(client, json={
            "account_id": account["id"], "period_start": "2026-05-01",
            "period_end": "2026-05-31",
            "transactions": [
                {"txn_date": "2026-05-04", "description": "PETRO CANADA 123",
                 "amount_cents": -6000},
            ],
        }, headers=headers)
    txn = client.get("/transactions", headers=headers).json()["items"][0]
    client.patch(
        f"/transactions/{txn['id']}", json={"category_id": category["id"]},
        headers=headers,
    )

    created = client.post(
        f"/transactions/{txn['id']}/apply_as_rule",
        json={"actor": "impl-1"},
        headers=headers,
    )

    assert created.status_code == 200, created.text
    rows = _rows(test_settings, created.json()["id"])
    assert len(rows) == 1
    assert rows[0]["actor"] == "impl-1"


# --- the property the whole table depends on ------------------------------


def test_a_change_without_its_record_is_impossible(
    client, test_settings, rule, monkeypatch
):
    """Force the record to fail and assert the change rolled back.

    This is the test the table exists for. Everything else here asserts
    that a row is written; this asserts the two cannot come apart — which
    is the only version of the guarantee worth having, because a trail
    that is usually written is not a trail.
    """
    import api.financial as financial

    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("simulated failure writing the audit row")

    monkeypatch.setattr(financial, "_record_rule_change", explode)

    response = client.patch(
        f"/merchant_rules/{rule['rule']['id']}",
        json={"pattern": "SOMETHING ELSE", "actor": "impl-4"},
        headers=_auth(test_settings),
    )

    assert response.status_code == 500
    after = client.get(
        f"/merchant_rules/{rule['rule']['id']}", headers=_auth(test_settings)
    ).json()
    assert after["pattern"] == "MAPLE DONUTS", (
        "the rule changed even though its record failed — the two came apart"
    )


# --- actor ----------------------------------------------------------------


@pytest.mark.parametrize("actor", ["", "   "])
def test_an_unattributed_change_is_refused(client, test_settings, actor):
    """Content-required, not presence-required.

    Requiring the field alone lets actor="" satisfy the rule and defeat
    its purpose in the same call — an unattributed row is barely better
    than a missing one, since "who changed this" is half of what the trail
    is for. Same reasoning as TransitionCreate, which is where this
    validation is copied from rather than reinvented.
    """
    category = client.get("/categories", headers=_auth(test_settings)).json()["items"][0]

    response = client.post(
        "/merchant_rules",
        json={"pattern": "X", "category_id": category["id"], "actor": actor},
        headers=_auth(test_settings),
    )

    assert response.status_code == 422


# --- reading it back ------------------------------------------------------


def test_history_is_readable_oldest_first(client, test_settings, rule):
    rule_id = rule["rule"]["id"]
    client.patch(
        f"/merchant_rules/{rule_id}",
        json={"pattern": "MAPLE DONUTS #", "actor": "review-1"},
        headers=_auth(test_settings),
    )

    body = client.get(
        f"/merchant_rules/{rule_id}/history", headers=_auth(test_settings)
    ).json()

    assert body["total"] == 2
    assert body["items"][0]["field"] is None, "creation first"
    assert body["items"][1]["field"] == "pattern"
    assert body["items"][1]["actor"] == "review-1"


def test_history_of_a_deleted_rule_is_still_readable(client, test_settings, rule):
    """A deleted rule's past is exactly when you want to read it."""
    rule_id = rule["rule"]["id"]
    client.delete(f"/merchant_rules/{rule_id}?actor=impl-4", headers=_auth(test_settings))

    body = client.get(
        f"/merchant_rules/{rule_id}/history", headers=_auth(test_settings)
    ).json()

    assert body["total"] == 2
