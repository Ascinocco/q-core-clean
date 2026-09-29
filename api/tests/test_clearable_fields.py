"""An explicit null clears; an absent key leaves alone (ticket T-17 / D93).

Across the codebase `None` meant "not supplied", so a nullable field was
write-once: you could set it and never take it back. Pydantic records
which keys the caller actually sent (`model_fields_set`), so the two
cases are distinguishable and `cleared()` reads that.

Two things have to hold together, and only one of them is obvious:

1. An explicit null on a nullable column clears it.
2. An explicit null on a NOT NULL column is a 422 NAMING THE FIELD --
   not a 500. Without (2) the clear reaches SQLite, comes back an
   IntegrityError, and the caller gets a server error for a mistake they
   made and cannot see.

Each model declares CLEARABLE. A declaration is a judgement, so the
first test here does not trust it: it derives the answer from the
database's own column nullability and fails when the two disagree.
"""

from __future__ import annotations

import inspect
import sqlite3

import pytest
from pydantic import BaseModel

import api.models as models_module

UPDATE_MODELS = {
    name: obj
    for name, obj in vars(models_module).items()
    if inspect.isclass(obj)
    and issubclass(obj, BaseModel)
    and obj.__module__ == models_module.__name__
    and name.endswith("Update")
}


def _columns(connection, table):
    """column -> is_nullable, from the database rather than schema.sql."""
    return {
        row[1]: not row[3]  # row[3] is "notnull"
        for row in connection.execute(f"PRAGMA table_info({table})")
    }


def _connect(client, test_settings):
    client.get("/entities", headers={"Authorization": "Bearer test-token"})
    return sqlite3.connect(test_settings.db_path)


def test_every_update_model_declares_its_table_and_clearable_set():
    """A model added without these is one nobody decided about."""
    assert UPDATE_MODELS, "discovery found no *Update models -- the walk broke"
    missing = sorted(
        name
        for name, model in UPDATE_MODELS.items()
        if not (hasattr(model, "TABLE") and hasattr(model, "CLEARABLE"))
    )
    assert not missing, (
        f"*Update models with no TABLE/CLEARABLE declaration: {missing} -- "
        f"decide which of their fields an explicit null may clear."
    )


def test_clearable_matches_the_databases_own_nullability(client, test_settings):
    """The derived check: CLEARABLE is a claim, the schema is not.

    A field is clearable exactly when its column allows NULL. Deriving it
    means a column that changes nullability cannot silently disagree with
    the model -- which would show up either as a 500 on a valid-looking
    clear, or as a 422 refusing one that would have worked.
    """
    connection = _connect(client, test_settings)
    wrong = {}
    for name, model in UPDATE_MODELS.items():
        columns = _columns(connection, model.TABLE)
        assert columns, f"{name}.TABLE={model.TABLE!r} has no columns"
        # Settable fields are those that ARE columns: `actor` is audit
        # metadata recorded elsewhere, not a column of the row being patched.
        expected = {
            field for field in model.model_fields if columns.get(field, False)
        }
        if expected != set(model.CLEARABLE):
            wrong[name] = {
                "declared": sorted(model.CLEARABLE),
                "nullable in db": sorted(expected),
            }
    assert not wrong, (
        f"CLEARABLE disagrees with the schema: {wrong} -- a field is "
        f"clearable exactly when its column allows NULL."
    )


# --- behaviour, per model ------------------------------------------------

def _headers(test_settings):
    return {"Authorization": f"Bearer {test_settings.api_token}"}


def test_clearing_a_nullable_field_takes_effect(client, test_settings):
    headers = _headers(test_settings)
    entity = client.post(
        "/entities",
        json={"type": "vehicle", "name": "Corolla", "attributes": {"make": "Toyota"}},
        headers=headers,
    ).json()
    assert entity["attributes"] == {"make": "Toyota"}

    patched = client.patch(
        f"/entities/{entity['id']}", json={"attributes": None}, headers=headers
    )
    assert patched.status_code == 200, patched.text
    # The column is a real SQL NULL, not the string "null": json.dumps(None)
    # would store the latter and it would read back as a JSON null.
    connection = sqlite3.connect(test_settings.db_path)
    stored = connection.execute(
        "SELECT attributes FROM entities WHERE id = ?", (entity["id"],)
    ).fetchone()[0]
    connection.close()
    assert stored is None, f"expected SQL NULL, stored {stored!r}"


def test_an_absent_key_still_leaves_the_field_alone(client, test_settings):
    """The other half. A clear that also wiped un-named fields would pass
    the test above while destroying data."""
    headers = _headers(test_settings)
    entity = client.post(
        "/entities",
        json={"type": "vehicle", "name": "Corolla", "attributes": {"make": "Toyota"}},
        headers=headers,
    ).json()

    patched = client.patch(
        f"/entities/{entity['id']}", json={"name": "Accord"}, headers=headers
    ).json()

    assert patched["name"] == "Accord"
    assert patched["attributes"] == {"make": "Toyota"}, "an absent key was cleared"


def test_clearing_a_required_field_is_a_422_naming_it(client, test_settings):
    headers = _headers(test_settings)
    entity = client.post(
        "/entities", json={"type": "pet", "name": "Rex"}, headers=headers
    ).json()

    response = client.patch(
        f"/entities/{entity['id']}", json={"name": None}, headers=headers
    )

    assert response.status_code == 422, response.text
    body = str(response.json())
    assert "name" in body
    assert "cannot clear" in body
    # And it says what CAN be cleared, so the caller is not left guessing.
    assert "attributes" in body


def test_an_empty_body_is_still_refused(client, test_settings):
    """The #110 guard survives the change -- naming no field is still a
    no-op reported as success."""
    headers = _headers(test_settings)
    entity = client.post(
        "/entities", json={"type": "pet", "name": "Rex"}, headers=headers
    ).json()

    response = client.patch(f"/entities/{entity['id']}", json={}, headers=headers)

    assert response.status_code == 422, response.text
    assert "at least one" in str(response.json())


def test_a_null_is_a_named_field_not_an_empty_body(client, test_settings):
    """impl-3's trap, pinned.

    `_require_at_least_one` used to define "empty" as every field being
    None -- which is exactly what a deliberate clear looks like. A model
    converted to `cleared()` without changing that guard turns clearing
    its only nullable field into a 422, and it reads as the feature not
    working rather than the guard misfiring.
    """
    headers = _headers(test_settings)
    entity = client.post(
        "/entities",
        json={"type": "vehicle", "name": "Corolla", "attributes": {"make": "Toyota"}},
        headers=headers,
    ).json()

    response = client.patch(
        f"/entities/{entity['id']}", json={"attributes": None}, headers=headers
    )

    assert response.status_code == 200, (
        "clearing the only nullable field was refused as an empty body -- "
        "the at-least-one guard is still using all-None semantics"
    )


@pytest.mark.parametrize("name", sorted(UPDATE_MODELS))
def test_extra_fields_are_still_forbidden(name):
    """`extra="forbid"` must survive the conversion, or a misspelled field
    is silently ignored instead of refused."""
    model = UPDATE_MODELS[name]
    assert model.model_config.get("extra") == "forbid"
