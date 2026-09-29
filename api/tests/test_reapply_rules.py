"""Reapplying rules to rows that were never classified (ticket T-51).

Rules run at import time only, so adding or correcting a rule changes
nothing already stored: rows imported before a rule existed stay
uncategorized until a reapply.
"""

from __future__ import annotations

from tests.source_import_support import seed_source_batch

import pytest


def _auth(settings) -> dict:
    return {"Authorization": f"Bearer {settings.api_token}"}


@pytest.fixture()
def seeded(client, test_settings):
    """One imported statement, imported BEFORE any rule exists."""
    headers = _auth(test_settings)
    account = client.post(
        "/entities",
        json={"type": "account", "name": "Card", "attributes": {"last4": "4321"}},
        headers=headers,
    ).json()
    imported = seed_source_batch(client, json={
            "account_id": account["id"],
            "period_start": "2026-05-01",
            "period_end": "2026-05-31",
            "transactions": [
                {"txn_date": "2026-05-04", "description": "ZORBLAX COFFEE #1",
                 "amount_cents": -1999},
                {"txn_date": "2026-05-05", "description": "ZORBLAX COFFEE #2",
                 "amount_cents": -2999},
                {"txn_date": "2026-05-06", "description": "UNKNOWN VENDOR",
                 "amount_cents": -500},
            ],
        }, headers=headers).json()
    assert imported["created"] == 3
    # The rule arrives AFTER the import -- the whole point.
    # Status asserted: an earlier version did not, so when #104 made
    # `actor` required the create 422'd silently and the failure surfaced
    # three asserts later as "0 == 2". A fixture that does not check its
    # own setup reports the wrong thing.
    rule = client.post(
        "/merchant_rules",
        json={"actor": "test", "pattern": "ZORBLAX", "category_id": "food_dining"},
        headers=headers,
    )
    assert rule.status_code in (200, 201), rule.text
    return {"account": account, "headers": headers, "statement": imported}


def _reapply(client, headers, **body):
    response = client.post("/merchant_rules/reapply", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_dry_run_is_the_default_and_writes_nothing(client, seeded):
    headers = seeded["headers"]
    result = _reapply(client, headers)
    assert result["dry_run"] is True
    assert result["changed"] == 2
    assert result["cents_by_category"] == {"food_dining": -4998}

    stored = client.get("/transactions", headers=headers).json()["items"]
    assert all(t["category_id"] is None for t in stored), "dry run must not write"


def test_it_returns_the_ids_it_changed(client, seeded):
    """There is no undo. This list is it."""
    headers = seeded["headers"]
    result = _reapply(client, headers, dry_run=False)
    assert len(result["changed_ids"]) == 2
    stored = {t["id"]: t for t in client.get("/transactions", headers=headers).json()["items"]}
    for txn_id in result["changed_ids"]:
        assert stored[txn_id]["category_id"] == "food_dining"


def test_a_second_run_changes_nothing(client, seeded):
    """Idempotent: once classified, a row is no longer eligible."""
    headers = seeded["headers"]
    _reapply(client, headers, dry_run=False)
    again = _reapply(client, headers, dry_run=False)
    assert again["changed"] == 0
    assert again["changed_ids"] == []


def test_it_never_overwrites_an_existing_category(client, seeded):
    """A row already classified is left alone, even if a rule now disagrees.

    That is also why correcting a WRONG rule does not retroactively fix
    the rows it miscategorized -- those need a separate decision.
    """
    headers = seeded["headers"]
    stored = client.get("/transactions", headers=headers).json()["items"]
    target = next(t for t in stored if t["description"] == "ZORBLAX COFFEE #1")
    client.patch(
        f"/transactions/{target['id']}",
        json={"category_id": "entertainment"},
        headers=headers,
    )

    result = _reapply(client, headers, dry_run=False)
    assert target["id"] not in result["changed_ids"]

    after = client.get(f"/transactions/{target['id']}", headers=headers).json()
    assert after["category_id"] == "entertainment", "a human decision was overwritten"


def test_unmatched_rows_are_examined_but_not_changed(client, seeded):
    headers = seeded["headers"]
    result = _reapply(client, headers)
    assert result["examined"] == 3
    assert result["changed"] == 2


def test_reapply_uses_the_shared_classifier(client, seeded, monkeypatch):
    """Shared BY CONSTRUCTION, not by looking alike.

    Two functions that agree today drift the moment one is edited, and
    nothing would report it -- the preview/import divergence all over
    again, one layer down. Patching `_classify` and observing reapply
    change proves it calls the same code; a copy would ignore this.
    """
    import api.financial as financial

    monkeypatch.setattr(
        financial, "_classify", lambda connection, description: ("pets_vet", None)
    )
    result = _reapply(client, seeded["headers"], dry_run=False)
    assert result["cents_by_category"] == {"pets_vet": -5498}, (
        "reapply did not use the patched classifier -- it has its own copy"
    )


def test_import_uses_the_shared_classifier(client, test_settings, monkeypatch):
    """The OTHER direction, and it is the one that rots.

    Pinning only reapply leaves import free to grow a private copy later:
    reapply would still pass, the two would diverge, and the test named
    for sharing would report nothing. A pin that watches one side of a
    shared dependency is half a pin.
    """
    import api.financial as financial

    headers = _auth(test_settings)
    account = client.post(
        "/entities",
        json={"type": "account", "name": "Card2", "attributes": {"last4": "4321"}},
        headers=headers,
    ).json()
    monkeypatch.setattr(
        financial, "_classify", lambda connection, description: ("pets_vet", None)
    )
    seed_source_batch(client, json={
            "account_id": account["id"],
            "period_start": "2026-06-01",
            "period_end": "2026-06-30",
            "transactions": [
                {"txn_date": "2026-06-04", "description": "ANYTHING AT ALL",
                 "amount_cents": -100}
            ],
        }, headers=headers)
    stored = client.get(
        "/transactions", headers=headers, params={"account_id": account["id"]}
    ).json()["items"]
    assert [t["category_id"] for t in stored] == ["pets_vet"], (
        "import did not use the patched classifier -- it has its own copy"
    )


def test_a_cleared_category_is_a_decision_reapply_must_not_overwrite(client, seeded):
    """The successor to test_update_transaction_cannot_clear_a_category.

    That test defended an ABSENCE: a null category could not be produced
    deliberately, so reapply could treat null as "never classified". It
    said, in capitals, that if it ever went red someone had made a
    deliberate null possible, and that the fix was to TIGHTEN THE
    PREDICATE rather than relax the test.

    ticket T-17 made deliberate nulls possible. So the predicate gained
    `edited_at IS NULL`, and this test now asserts the protection its
    predecessor existed to demand -- not merely that clearing works, which
    would be relaxing it.
    """
    headers = seeded["headers"]
    stored = client.get("/transactions", headers=headers).json()["items"]
    target = next(t for t in stored if t["description"] == "UNKNOWN VENDOR")

    client.patch(
        f"/transactions/{target['id']}",
        json={"category_id": "food_dining"},
        headers=headers,
    )
    # The clear now takes effect -- the half the old test asserted could
    # not happen.
    client.patch(
        f"/transactions/{target['id']}", json={"category_id": None}, headers=headers
    )
    after = client.get(f"/transactions/{target['id']}", headers=headers).json()
    assert after["category_id"] is None, "explicit null did not clear the category"

    # And the half that matters: a rule that WOULD match this row must not
    # reclassify it, because a person decided it belongs to nothing.
    rule = client.post(
        "/merchant_rules",
        json={
            "actor": "test",
            "pattern": "UNKNOWN VENDOR",
            "category_id": "food_dining",
        },
        headers=headers,
    )
    assert rule.status_code in (200, 201), rule.text
    _reapply(client, headers, dry_run=False)

    final = client.get(f"/transactions/{target['id']}", headers=headers).json()
    assert final["category_id"] is None, (
        "reapply overwrote a deliberately cleared category -- the "
        "edited_at IS NULL clause is not doing its job"
    )


def test_the_edited_at_clause_depends_on_this_field_set(client, seeded):
    """reapply's `edited_at IS NULL` rests on a contingent fact.

    `edited_at IS NOT NULL` only means "a person decided the
    classification" because `TransactionUpdate` can patch NOTHING ELSE. A
    third settable field could stamp `edited_at` without touching
    category or entity, and reapply would then skip rows nobody had
    classified -- silently doing less than it says.

    So the fact is pinned here rather than trusted. If this fails, re-read
    reapply_merchant_rules' docstring before adding the field.
    """
    from api.models import TransactionUpdate

    assert set(TransactionUpdate.model_fields) == {"category_id", "entity_id"}


def test_archived_rows_are_not_reclassified(client, seeded):
    """Archiving says "this import should not count".

    Silently re-categorizing its rows would make it count again in the one
    column a reader trusts. Caught by #94's scope guard rather than by
    design -- the first version of this endpoint read the raw table.
    """
    headers = seeded["headers"]
    statement_id = seeded["statement"]["statement_id"]
    archived = client.post(f"/statements/{statement_id}/archive", headers=headers)
    assert archived.status_code == 200, archived.text

    result = _reapply(client, headers, dry_run=False)
    assert result["examined"] == 0
    assert result["changed"] == 0




# --------------------------------------------------------------------------
# reapply's `edited_at IS NULL` rests on TWO facts (D105, ticket T-52).
#
# The first -- that `TransactionUpdate` is patchable in nothing but the
# two classification fields -- is pinned by
# test_the_edited_at_clause_depends_on_this_field_set above.
#
# The second is that `update_transaction` is the ONLY code path that
# writes `edited_at`. That was stated in two docstrings and a comment, was
# true, and was pinned by nothing. If a future endpoint stamps `edited_at`
# on rows it did not classify, reapply skips them and silently does less
# than it says -- the worst shape of failure here, because the report says
# "0 changed" and that reads as "nothing needed changing".
# --------------------------------------------------------------------------

import ast as _ast
import re as _re
from pathlib import Path as _Path

from api.config import REPO_ROOT as _REPO_ROOT

#: An assignment, not a comparison: `edited_at =` matches the write and
#: not `edited_at IS NULL` / `IS NOT NULL`, which are the reads.
_EDITED_AT_WRITE = _re.compile(r"edited_at\s*=", _re.IGNORECASE)
_SQLISH = _re.compile(r"\b(UPDATE|INSERT|SET)\b", _re.IGNORECASE)


def _edited_at_write_sites() -> list[tuple[str, int, str]]:
    """Every place `edited_at` is assigned, read out of the source.

    Walks STRING CONSTANTS via the AST rather than grepping lines. Two
    reasons, both load-bearing:

    - Prose is skipped. `api/financial.py` and `api/models.py` discuss
      `edited_at` at length; a line-based search counts the discussion.
    - Implicitly concatenated SQL is joined before it is examined. The
      one real write site is split across two source lines, with
      `UPDATE` on the first and `edited_at =` on the second, so a
      line-based search sees a fragment with no verb and either misses
      it or matches on something weaker.
    """
    sites: list[tuple[str, int, str]] = []
    for path in sorted((_REPO_ROOT / "api").glob("*.py")):
        for node in _ast.walk(_ast.parse(path.read_text())):
            if not (isinstance(node, _ast.Constant) and isinstance(node.value, str)):
                continue
            if _EDITED_AT_WRITE.search(node.value) and _SQLISH.search(node.value):
                sites.append(
                    (path.name, node.lineno, " ".join(node.value.split())[:70])
                )
    return sites


def _sql_files() -> list[_Path]:
    return [_REPO_ROOT / "db" / "schema.sql"] + sorted(
        (_REPO_ROOT / "db" / "migrations").glob("*.sql")
    )


def test_update_transaction_is_the_only_writer_of_edited_at():
    """The unpinned half of reapply's argument, pinned.

    WHEN THIS LEGITIMATELY BECOMES TWO: do not raise the number. A second
    writer means `edited_at IS NOT NULL` no longer implies "a person
    decided the classification", so the fix is to change reapply's
    predicate -- pair it with something recording what was decided, or
    scope it to the writer that means it. Then update this test to match
    the new argument. Bumping the count keeps the suite green and the
    predicate wrong.
    """
    sites = _edited_at_write_sites()

    assert sites, (
        "found no edited_at write site at all -- the AST walk broke. "
        "Without this the assertion below would pass over an empty list "
        "and report a rule enforced across nothing."
    )
    assert len(sites) == 1, (
        f"edited_at is written in {len(sites)} places: {sites}. reapply's "
        f"edited_at IS NULL clause assumes exactly one. Read this test's "
        f"docstring before changing the number."
    )

    name, _lineno, sql = sites[0]
    assert name == "financial.py", f"the single writer moved to {name}"
    assert "UPDATE transactions" in sql, sql


def test_no_trigger_writes_edited_at_behind_the_endpoint():
    """A trigger would be a writer no source-reading count of Python
    finds, and no reader of `update_transaction` would ever see.

    There are no triggers in this schema at all today, which is asserted
    rather than assumed: if one is ever added, this is the test that
    should be re-read, because a trigger writing `edited_at` is exactly
    the invisible second writer the test above cannot see.
    """
    files = _sql_files()
    names = {path.name for path in files}
    # Not merely "the list is non-empty". Measured: dropping schema.sql
    # from the list still left the six migration files, so a bare
    # truthiness check passed while the file most likely to hold a
    # trigger was no longer being read. A vacuity guard has to name what
    # it needs, not count what it got.
    assert "schema.sql" in names, f"schema.sql is not being read: {sorted(names)}"
    assert len(files) > 1, f"migrations are not being read: {sorted(names)}"
    for path in files:
        assert path.is_file(), f"{path} does not exist"

    trigger_files = [
        path.name
        for path in files
        if _re.search(r"CREATE\s+TRIGGER", path.read_text(), _re.IGNORECASE)
    ]
    writes = [
        (path.name, line.strip()[:70])
        for path in files
        for line in path.read_text().split("\n")
        if _EDITED_AT_WRITE.search(line)
    ]

    assert not writes, f"SQL assigns edited_at outside the endpoint: {writes}"
    assert not trigger_files, (
        f"this schema now has triggers ({trigger_files}) -- check none of "
        f"them writes edited_at, then update this test to check that "
        f"rather than asserting there are none"
    )
