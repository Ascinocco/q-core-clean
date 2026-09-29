"""The refusal message's coverage is derived, not remembered.

`DELETE /entities/{id}` used to work by attempting the delete and catching
whatever SQLite complained about. That makes its coverage exactly "whatever
the database happens to enforce" -- invisible at the API layer, and silently
incomplete for any reference the database does not enforce.

`note_links` was that case: `target_id` is polymorphic and carries no FK
(db/schema.sql assigns its integrity to "the API layer"), so deleting an
entity a note pointed at returned 200 and orphaned the link. Nothing caught
it, because there was nothing to catch.

`ENTITY_REFERENCES` now lists every reference so the refusal can name the
blocker. A hand-maintained list has its own failure mode -- it drifts -- so
these tests derive the real answer from the database and fail when the list
disagrees. A new table reaching entities cannot go unenumerated whether it
carries an FK or not.
"""

from __future__ import annotations

import sqlite3

from api.entities import ENTITY_REFERENCES, _POLYMORPHIC


def _connect(client, test_settings) -> sqlite3.Connection:
    """Bootstrap through the app first -- opening the file directly would
    create an empty one and trip the corrupt-database guard."""
    client.get("/entities", headers={"Authorization": "Bearer test-token"})
    return sqlite3.connect(test_settings.db_path)


def _tables(connection) -> list[str]:
    return [
        r[0]
        for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    ]


def _fk_references_to_entities(connection) -> set[tuple[str, str]]:
    """Every (table, column) with a real FK to entities(id), from the DB."""
    found = set()
    for table in _tables(connection):
        for row in connection.execute(f"PRAGMA foreign_key_list({table})"):
            # (id, seq, target_table, from_col, to_col, ...)
            if row[2] == "entities":
                found.add((table, row[3]))
    return found


def _polymorphic_tables(connection) -> set[str]:
    """Tables carrying a target_type/target_id pair -- a reference the
    database cannot enforce, and therefore one this layer must."""
    found = set()
    for table in _tables(connection):
        columns = {r[1] for r in connection.execute(f"PRAGMA table_info({table})")}
        if {"target_type", "target_id"} <= columns:
            found.add(table)
    return found


def test_every_fk_backed_reference_is_enumerated(client, test_settings):
    """A new FK to entities cannot land without joining the refusal message.

    Derived from `PRAGMA foreign_key_list`, so it reflects what the database
    actually enforces -- including anything a migration added that never
    made it back into db/schema.sql.
    """
    connection = _connect(client, test_settings)
    actual = _fk_references_to_entities(connection)
    assert actual, "derived no FK references to entities -- the PRAGMA walk broke"
    missing = sorted(actual - set(ENTITY_REFERENCES))
    assert not missing, (
        f"these columns have a real FK to entities(id) but are not in "
        f"ENTITY_REFERENCES: {missing} -- add each one, or a delete blocked "
        f"by it will fall through to the backstop and refuse without naming it."
    )


def test_every_polymorphic_reference_is_enumerated(client, test_settings):
    """The case the database cannot help with -- note_links' original bug.

    A polymorphic target has no FK, so nothing refuses on its behalf. If a
    second such table appears and is not enumerated here, deleting an entity
    orphans its rows exactly as note_links did.
    """
    connection = _connect(client, test_settings)
    polymorphic = _polymorphic_tables(connection)
    assert polymorphic, "found no polymorphic tables -- the column walk broke"
    enumerated = {table for table, _ in ENTITY_REFERENCES}
    missing = sorted(polymorphic - enumerated)
    assert not missing, (
        f"these tables carry a polymorphic target_type/target_id pair but are "
        f"not in ENTITY_REFERENCES: {missing} -- nothing in the database will "
        f"refuse on their behalf, so deleting an entity silently orphans them."
    )


def test_the_registry_names_real_columns(client, test_settings):
    """The reverse: a renamed or dropped column left behind on the list.

    A stale entry makes `_reference_counts` raise OperationalError on every
    delete -- a 500 on a working request -- so this is not cosmetic.
    """
    connection = _connect(client, test_settings)
    tables = set(_tables(connection))
    for table, column in ENTITY_REFERENCES:
        assert table in tables, f"ENTITY_REFERENCES names missing table {table!r}"
        columns = {r[1] for r in connection.execute(f"PRAGMA table_info({table})")}
        assert column in columns, (
            f"ENTITY_REFERENCES names {table}.{column}, which does not exist"
        )


def test_every_polymorphic_entry_has_a_type_predicate(client, test_settings):
    """Matching target_id alone would count another type's rows.

    Ids are uuids so a collision is vanishingly unlikely, but the query
    would be wrong on its face, and "unlikely to collide" is not the
    property a delete guard should rest on.
    """
    connection = _connect(client, test_settings)
    polymorphic = _polymorphic_tables(connection)
    for table, _ in ENTITY_REFERENCES:
        if table in polymorphic:
            assert table in _POLYMORPHIC, (
                f"{table} is polymorphic but has no entry in _POLYMORPHIC, so "
                f"its count would match rows of every target_type"
            )


# --------------------------------------------------------------------------
# The backstop itself, exercised (ticket T-36).
#
# `delete_entity` counts references BEFORE deleting, so the FK is a
# backstop that the registry keeps unreachable. That left its behaviour
# asserted by nothing: neutering it failed no test at all, and the
# nine-site conflict count stayed green because the count reads the
# `raise ConflictError` TEXT, not what the branch answers. A count pin
# detects arrival, not verification.
#
# Reaching it needs a table with a real FK to entities(id) that
# ENTITY_REFERENCES does not know about -- precisely the situation the
# registry test exists to prevent, built deliberately here. Each test gets
# its own tmp_path database, so this table exists only for this test and
# cannot make the derived registry tests fail.
# --------------------------------------------------------------------------


def _make_unregistered_referencing_table(connection, entity_id):
    """A table the registry has never heard of, holding a live reference."""
    connection.execute(
        "CREATE TABLE shadow_refs ("
        "  id TEXT PRIMARY KEY,"
        "  entity_id TEXT NOT NULL REFERENCES entities(id)"
        ")"
    )
    connection.execute(
        "INSERT INTO shadow_refs (id, entity_id) VALUES ('s1', ?)", (entity_id,)
    )
    connection.commit()


def test_the_backstop_refuses_a_reference_the_registry_does_not_know(
    client, test_settings
):
    """The fall-through answers a conflict, not a 500.

    Without this the branch was code nobody had ever run. An unhandled
    IntegrityError here is a 500: the caller is told the server broke on a
    request that was actually refused for a good reason, and the row they
    wanted gone is still there with no way to find out why.
    """
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    entity = client.post(
        "/entities", json={"type": "pet", "name": "Rex"}, headers=headers
    ).json()

    connection = _connect(client, test_settings)
    _make_unregistered_referencing_table(connection, entity["id"])

    response = client.delete(f"/entities/{entity['id']}", headers=headers)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "conflict", response.text

    message = response.json()["error"]["message"]
    # The BACKSTOP's own words, not the pre-count's. Asserting only
    # "conflict" would pass if the pre-count had somehow refused instead,
    # and this test would then be exercising the branch it was written to
    # reach past.
    assert "this check does not yet enumerate" in message
    assert "still referenced by" in message

    # The entity survives, and so does the reference -- a rollback that
    # left the row deleted would be the worst outcome of the three.
    assert client.get(f"/entities/{entity['id']}", headers=headers).status_code == 200
    connection = sqlite3.connect(test_settings.db_path)
    try:
        assert connection.execute("SELECT count(*) FROM shadow_refs").fetchone()[0] == 1
    finally:
        connection.close()


def test_the_backstop_is_reached_past_the_pre_count_not_instead_of_it(
    client, test_settings
):
    """Proves the test above exercises the fall-through.

    `shadow_refs` is deliberately absent from ENTITY_REFERENCES, so the
    pre-count sees nothing and the DELETE is attempted. If the table were
    ever added to the registry, the pre-count would refuse first and the
    test above would still pass -- green, while the branch it names went
    back to being unexercised.
    """
    from api.entities import ENTITY_REFERENCES, _reference_counts

    assert "shadow_refs" not in {table for table, _ in ENTITY_REFERENCES}

    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    entity = client.post(
        "/entities", json={"type": "pet", "name": "Rex"}, headers=headers
    ).json()
    connection = _connect(client, test_settings)
    _make_unregistered_referencing_table(connection, entity["id"])

    # The pre-count is blind to it: that is what makes the DELETE run at all.
    assert _reference_counts(connection, entity["id"]) == {}
