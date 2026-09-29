"""Every `*Update` model must refuse an update that names no field.

An update model with all-optional fields accepts an empty body, the
endpoint returns 200 with the unchanged row, and that reads exactly like
a successful edit. The caller believes the correction landed.

Found three times separately before it was swept (ticket T-56):
`MerchantRuleUpdate` at the API and again at the tool surface (#100),
then `TransactionUpdate` -- where it was worse than a no-op, because
`update_transaction` is the only writer of `edited_at`, so an empty PATCH
stamped a row as hand-edited and #94's archive report then told the user
a re-import would discard work nobody had done.

THE LITERAL LIST AND THE DISCOVERY CHECK EACH OTHER. A literal list alone
cannot catch a new model nobody added to it -- the diff that introduces
it is the diff that would have carried the entry. Discovery alone can go
vacuous: if the introspection returned nothing, every requirement below
would pass over an empty set and report green. Asserting the two agree
has neither failure.
"""

from __future__ import annotations

from tests.source_import_support import seed_source_batch

import inspect

import pytest
from pydantic import BaseModel, ValidationError

import api.models as models

#: model name -> the fields that count as "something to change".
#: Metadata like `actor` is excluded deliberately: naming who is making a
#: change is not a change.
EXPECTED: dict[str, tuple[str, ...]] = {
    "ReminderUpdate": ("title", "notes", "entity_id", "recurrence_rule", "start_date", "end_date", "due_time", "location", "notification_offsets_minutes"),
    "BoardUpdate": ("title", "key_prefix"),
    "CategoryUpdate": ("name", "parent_id"),
    "EntityUpdate": ("name", "status", "attributes"),
    "MerchantRuleUpdate": ("pattern", "category_id", "entity_id"),
    # The three amendable fields. The pair and the type are absent on
    # purpose — they are create_relationship's dedup key (D93).
    "RelationshipUpdate": ("start_date", "end_date", "attributes"),
    "TicketUpdate": ("title", "description", "parent_id", "position"),
    "TransactionUpdate": ("category_id", "entity_id"),
}

#: Fields a model requires that are NOT settable values -- supplied so the
#: model can be constructed at all when testing the guard.
REQUIRED_METADATA: dict[str, dict[str, object]] = {
    "MerchantRuleUpdate": {"actor": "test"},
    "ReminderUpdate": {"reset_occurrences": False},
}


def _discovered() -> set[str]:
    return {
        name
        for name, obj in vars(models).items()
        if inspect.isclass(obj)
        and issubclass(obj, BaseModel)
        and name.endswith("Update")
    }


def test_the_literal_list_and_the_discovery_agree():
    """Guards both directions, and neither guards itself.

    If a new `*Update` model lands without an entry, this fails naming it
    -- which is the case a literal list alone cannot see. If the
    introspection ever returns nothing, this fails too, rather than
    letting every test below pass over an empty set.
    """
    discovered = _discovered()
    assert discovered, "introspection found no *Update models at all"
    missing = discovered - set(EXPECTED)
    extra = set(EXPECTED) - discovered
    assert not missing, f"new *Update model(s) with no entry here: {sorted(missing)}"
    assert not extra, f"EXPECTED names a model that no longer exists: {sorted(extra)}"


@pytest.mark.parametrize("name", sorted(EXPECTED), ids=lambda n: n)
def test_the_field_list_matches_the_model(name):
    """The same agreement the model list gets, one level down.

    `_require_at_least_one(model, *fields)` takes a per-model literal
    list, and nothing pinned it against the model. A new settable field
    would then make a legitimate single-field edit refuse -- with a
    message naming only the OLD fields -- and no test would notice,
    because every existing case still passes.

    "Settable" excludes required metadata: naming who is making a change
    is not a change, which is why `actor` is in REQUIRED_METADATA rather
    than in EXPECTED.
    """
    model = getattr(models, name)
    settable = set(model.model_fields) - set(REQUIRED_METADATA.get(name, {}))
    assert set(EXPECTED[name]) == settable, (
        f"{name}'s guard checks {sorted(EXPECTED[name])} but the model has "
        f"{sorted(settable)} -- a field the guard does not know about makes "
        "a legitimate edit refuse"
    )


@pytest.mark.parametrize("name", sorted(EXPECTED), ids=lambda n: n)
def test_an_update_naming_no_field_is_refused(name):
    model = getattr(models, name)
    with pytest.raises(ValidationError) as excinfo:
        model(**REQUIRED_METADATA.get(name, {}))
    assert "at least one" in str(excinfo.value)


@pytest.mark.parametrize("name", sorted(EXPECTED), ids=lambda n: n)
def test_naming_a_single_field_is_accepted(name):
    """The guard must not become "all fields required" by accident.

    A partial update is the normal case; only saying nothing is refused.
    """
    model = getattr(models, name)
    for field in EXPECTED[name]:
        value = _sample(model, field)
        if value is None:
            continue
        model(**REQUIRED_METADATA.get(name, {}), **{field: value})


def _sample(model: type[BaseModel], field: str):
    """A plausible value for one field, or None to skip it."""
    annotation = str(model.model_fields[field].annotation)
    if field == "key_prefix":
        return "KA"  # held to KEY_PREFIX_PATTERN, so "x" is refused
    if "list[int]" in annotation:
        return [60]
    if "int" in annotation and "str" not in annotation:
        return 1
    if "dict" in annotation:
        return {"k": "v"}
    if "EntityStatus" in annotation:
        return "active"
    if "str" in annotation:
        return "x"
    return None


def test_a_field_set_to_its_current_value_is_still_an_edit():
    """Re-asserting a value is a decision, not a no-op.

    The guard refuses a body that says NOTHING. It does not try to work
    out whether what the caller said differs from what is stored -- "yes,
    this is still right" is something the caller said, and the model has
    no access to the stored row anyway.
    """
    models.TransactionUpdate(category_id="food_dining")
    models.EntityUpdate(name="unchanged")


def test_an_empty_patch_does_not_stamp_edited_at(client, test_settings):
    """The concrete harm, end to end (ticket T-56).

    `update_transaction` is the only writer of `edited_at`, and #94's
    archive report reads it to tell a user what a re-import would discard.
    An empty PATCH used to return 200 and stamp the row, so the report
    listed work nobody had done.

    The guard lives in the model, so validation refuses the body before
    the endpoint runs -- `edited_at` is stamped only on an accepted PATCH
    by construction, not by an ordering someone has to maintain.
    """
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
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
                {"txn_date": "2026-05-04", "description": "ANYTHING", "amount_cents": -100}
            ],
        }, headers=headers).json()
    txn = client.get("/transactions", headers=headers).json()["items"][0]

    empty = client.patch(f"/transactions/{txn['id']}", json={}, headers=headers)
    assert empty.status_code == 422, empty.text

    report = client.post(
        f"/statements/{imported['statement_id']}/archive", headers=headers
    ).json()
    assert report["hand_edited"] == 0, (
        "an empty PATCH marked a row as hand-edited; the archive report now "
        "claims a re-import would discard work nobody did"
    )
