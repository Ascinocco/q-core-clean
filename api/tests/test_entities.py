from datetime import date, datetime

import pytest

from api.models import (
    ATTRIBUTE_MODELS,
    FORWARD_DATED_ATTRIBUTES,
    HISTORICAL_DATE_ATTRIBUTES,
)


def test_create_entity_returns_created_entity(client, test_settings):
    response = client.post(
        "/entities",
        json={"type": "pet", "name": "Rex"},
        headers={"Authorization": f"Bearer {test_settings.api_token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "pet"
    assert body["name"] == "Rex"
    assert body["status"] == "active"
    assert body["attributes"] == {}
    assert body["id"]
    assert body["created_at"]


def test_create_entity_requires_auth(client):
    response = client.post("/entities", json={"type": "pet", "name": "Rex"})
    assert response.status_code == 401


def test_create_entity_validates_attributes_against_type(client, test_settings):
    response = client.post(
        "/entities",
        json={
            "type": "account",
            "name": "Chase Checking",
            "attributes": {"account_subtype": "cryptocurrency"},
        },
        headers={"Authorization": f"Bearer {test_settings.api_token}"},
    )

    assert response.status_code == 422


def test_get_entity_returns_404_for_unknown_id(client, test_settings):
    response = client.get(
        "/entities/does-not-exist",
        headers={"Authorization": f"Bearer {test_settings.api_token}"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_list_entities_filters_by_type_and_status(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    client.post("/entities", json={"type": "pet", "name": "Rex"}, headers=headers)
    client.post(
        "/entities",
        json={"type": "vehicle", "name": "Corolla", "status": "sold"},
        headers=headers,
    )

    response = client.get("/entities?type=pet", headers=headers)
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "Rex"

    response = client.get("/entities?status=sold", headers=headers)
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "Corolla"


def test_list_entities_paginates(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    for i in range(3):
        client.post(
            "/entities", json={"type": "pet", "name": f"Pet {i}"}, headers=headers
        )

    response = client.get("/entities?limit=2&offset=0", headers=headers)
    body = response.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert body["limit"] == 2
    assert body["offset"] == 0


def test_list_entities_rejects_limit_over_200(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    response = client.get("/entities?limit=201", headers=headers)
    assert response.status_code == 422


def test_list_entities_rejects_non_positive_limit(client, test_settings):
    # paginate() raises ValueError on limit <= 0, which would surface as an
    # unhandled 500. Query validation has to reject it as a 422 first.
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}

    assert client.get("/entities?limit=0", headers=headers).status_code == 422
    assert client.get("/entities?limit=-1", headers=headers).status_code == 422


def test_list_entities_orders_case_insensitively(client, test_settings):
    # SQLite's default binary collation would sort these ["Boat", "Zebra",
    # "apple"] — surprising for a personal inventory listing.
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    for name in ["Zebra", "apple", "Boat"]:
        client.post("/entities", json={"type": "pet", "name": name}, headers=headers)

    response = client.get("/entities", headers=headers)
    names = [item["name"] for item in response.json()["items"]]

    assert names == ["apple", "Boat", "Zebra"]


def test_list_entities_breaks_name_ties_deterministically(client, test_settings):
    # COLLATE NOCASE makes "Apple" and "apple" compare equal, and SQLite
    # gives no guarantee about tie order — a future index on name could
    # reorder them between two paginated requests, repeating or skipping a
    # row. The `, id` tiebreak pins it. Six tied rows so this fails loudly
    # without the tiebreak (1/720 odds of insertion order matching id
    # order by luck) rather than flaking.
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    for name in ["Apple", "apple", "APPLE", "aPPle", "ApPlE", "appLE"]:
        client.post("/entities", json={"type": "pet", "name": name}, headers=headers)

    ids = [item["id"] for item in client.get("/entities", headers=headers).json()["items"]]
    assert ids == sorted(ids)

    # And the same total order holds across page boundaries.
    paged = []
    for offset in range(0, 6, 2):
        page = client.get(f"/entities?limit=2&offset={offset}", headers=headers).json()
        paged.extend(item["id"] for item in page["items"])
    assert paged == ids


def test_create_relationship(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    owner = client.post(
        "/entities", json={"type": "person", "name": "Alex"}, headers=headers
    ).json()
    car = client.post(
        "/entities", json={"type": "vehicle", "name": "Corolla"}, headers=headers
    ).json()

    response = client.post(
        f"/entities/{owner['id']}/relationships",
        json={"to_entity_id": car["id"], "relationship_type": "owns"},
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["from_entity_id"] == owner["id"]
    assert body["to_entity_id"] == car["id"]
    assert body["relationship_type"] == "owns"


def test_create_relationship_rejects_unknown_to_entity(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    owner = client.post(
        "/entities", json={"type": "person", "name": "Alex"}, headers=headers
    ).json()

    response = client.post(
        f"/entities/{owner['id']}/relationships",
        json={"to_entity_id": "does-not-exist", "relationship_type": "owns"},
        headers=headers,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_reference"


def test_create_relationship_returns_404_for_unknown_from_entity(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    car = client.post(
        "/entities", json={"type": "vehicle", "name": "Corolla"}, headers=headers
    ).json()

    response = client.post(
        "/entities/does-not-exist/relationships",
        json={"to_entity_id": car["id"], "relationship_type": "owns"},
        headers=headers,
    )

    assert response.status_code == 404


def test_create_relationship_dedupes_point_in_time_type(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    owner = client.post(
        "/entities", json={"type": "person", "name": "Alex"}, headers=headers
    ).json()
    car = client.post(
        "/entities", json={"type": "vehicle", "name": "Corolla"}, headers=headers
    ).json()

    first = client.post(
        f"/entities/{owner['id']}/relationships",
        json={"to_entity_id": car["id"], "relationship_type": "owns"},
        headers=headers,
    ).json()
    second = client.post(
        f"/entities/{owner['id']}/relationships",
        json={"to_entity_id": car["id"], "relationship_type": "owns"},
        headers=headers,
    ).json()

    assert first["id"] == second["id"]

    listing = client.get(
        f"/entities/{owner['id']}/relationships", headers=headers
    ).json()
    assert listing["total"] == 1


def test_create_relationship_accumulates_dated_type(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    tenant = client.post(
        "/entities", json={"type": "person", "name": "Jamie"}, headers=headers
    ).json()
    unit = client.post(
        "/entities", json={"type": "property", "name": "Unit 4B"}, headers=headers
    ).json()

    client.post(
        f"/entities/{tenant['id']}/relationships",
        json={
            "to_entity_id": unit["id"],
            "relationship_type": "leases",
            "start_date": "2025-01-01",
            "end_date": "2025-12-31",
        },
        headers=headers,
    )
    client.post(
        f"/entities/{tenant['id']}/relationships",
        json={
            "to_entity_id": unit["id"],
            "relationship_type": "leases",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
        },
        headers=headers,
    )

    listing = client.get(
        f"/entities/{tenant['id']}/relationships", headers=headers
    ).json()
    assert listing["total"] == 2


def test_list_relationships_includes_both_directions(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    owner = client.post(
        "/entities", json={"type": "person", "name": "Alex"}, headers=headers
    ).json()
    car = client.post(
        "/entities", json={"type": "vehicle", "name": "Corolla"}, headers=headers
    ).json()
    client.post(
        f"/entities/{owner['id']}/relationships",
        json={"to_entity_id": car["id"], "relationship_type": "owns"},
        headers=headers,
    )

    from_owner = client.get(
        f"/entities/{owner['id']}/relationships", headers=headers
    ).json()
    from_car = client.get(
        f"/entities/{car['id']}/relationships", headers=headers
    ).json()

    assert from_owner["total"] == 1
    assert from_car["total"] == 1


def test_delete_relationship(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    owner = client.post(
        "/entities", json={"type": "person", "name": "Alex"}, headers=headers
    ).json()
    car = client.post(
        "/entities", json={"type": "vehicle", "name": "Corolla"}, headers=headers
    ).json()
    relationship = client.post(
        f"/entities/{owner['id']}/relationships",
        json={"to_entity_id": car["id"], "relationship_type": "owns"},
        headers=headers,
    ).json()

    response = client.delete(f"/relationships/{relationship['id']}", headers=headers)
    assert response.status_code == 200

    listing = client.get(
        f"/entities/{owner['id']}/relationships", headers=headers
    ).json()
    assert listing["total"] == 0


def test_delete_relationship_returns_404_for_unknown_id(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    response = client.delete("/relationships/does-not-exist", headers=headers)
    assert response.status_code == 404


def test_list_relationships_rejects_non_positive_limit(client, test_settings):
    # paginate() raises ValueError on limit <= 0, which would surface as an
    # unhandled 500. list_entities already guards this with ge=1; this asserts
    # the relationships listing rejects it the same way instead of crashing.
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    owner = client.post(
        "/entities", json={"type": "person", "name": "Alex"}, headers=headers
    ).json()

    response = client.get(
        f"/entities/{owner['id']}/relationships?limit=0", headers=headers
    )

    assert response.status_code == 422


def test_create_relationship_dedupes_symmetric_type_in_both_directions(
    client, test_settings
):
    # runbooks/entity-attribute-schemas.md: symmetric types (spouse_of) are
    # "inserted once; query both from/to sides rather than double-writing".
    # Recording the same marriage from each spouse's side must not create a
    # second row — list_relationships already reads both directions, so a
    # double-write surfaces the same marriage twice.
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    alice = client.post(
        "/entities", json={"type": "person", "name": "Alice"}, headers=headers
    ).json()
    bob = client.post(
        "/entities", json={"type": "person", "name": "Bob"}, headers=headers
    ).json()

    first = client.post(
        f"/entities/{alice['id']}/relationships",
        json={"to_entity_id": bob["id"], "relationship_type": "spouse_of"},
        headers=headers,
    ).json()
    second = client.post(
        f"/entities/{bob['id']}/relationships",
        json={"to_entity_id": alice["id"], "relationship_type": "spouse_of"},
        headers=headers,
    ).json()

    assert first["id"] == second["id"]

    listing = client.get(
        f"/entities/{alice['id']}/relationships", headers=headers
    ).json()
    assert listing["total"] == 1


def test_create_relationship_keeps_asymmetric_type_directional(client, test_settings):
    # parent_of is deliberately NOT symmetric: "A parent_of B" and
    # "B parent_of A" are different claims, so both must persist as separate
    # rows. This pins the asymmetry so the symmetric fix above cannot later
    # be widened to all point-in-time types.
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    parent = client.post(
        "/entities", json={"type": "person", "name": "Pat"}, headers=headers
    ).json()
    child = client.post(
        "/entities", json={"type": "person", "name": "Kim"}, headers=headers
    ).json()

    first = client.post(
        f"/entities/{parent['id']}/relationships",
        json={"to_entity_id": child["id"], "relationship_type": "parent_of"},
        headers=headers,
    ).json()
    second = client.post(
        f"/entities/{child['id']}/relationships",
        json={"to_entity_id": parent["id"], "relationship_type": "parent_of"},
        headers=headers,
    ).json()

    assert first["id"] != second["id"]

    listing = client.get(
        f"/entities/{parent['id']}/relationships", headers=headers
    ).json()
    assert listing["total"] == 2
def test_patch_entity_updates_provided_fields_only(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    created = client.post(
        "/entities",
        json={"type": "vehicle", "name": "Corolla", "attributes": {"make": "Toyota"}},
        headers=headers,
    ).json()

    response = client.patch(
        f"/entities/{created['id']}", json={"status": "sold"}, headers=headers
    )

    body = response.json()
    assert body["status"] == "sold"
    assert body["name"] == "Corolla"
    assert body["attributes"] == {"make": "Toyota"}
    assert body["updated_at"] is not None


def test_patch_entity_replaces_attributes_when_provided(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    created = client.post(
        "/entities",
        json={"type": "vehicle", "name": "Corolla", "attributes": {"make": "Toyota"}},
        headers=headers,
    ).json()

    response = client.patch(
        f"/entities/{created['id']}",
        json={"attributes": {"make": "Toyota", "model": "Corolla"}},
        headers=headers,
    )

    assert response.json()["attributes"] == {"make": "Toyota", "model": "Corolla"}


def test_patch_entity_rejects_type_field(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    created = client.post(
        "/entities", json={"type": "pet", "name": "Rex"}, headers=headers
    ).json()

    response = client.patch(
        f"/entities/{created['id']}", json={"type": "vehicle"}, headers=headers
    )

    assert response.status_code == 422


def test_patch_entity_returns_404_for_unknown_id(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    response = client.patch(
        "/entities/does-not-exist", json={"status": "sold"}, headers=headers
    )
    assert response.status_code == 404


def test_delete_entity_removes_it(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    created = client.post(
        "/entities", json={"type": "pet", "name": "Rex"}, headers=headers
    ).json()

    response = client.delete(f"/entities/{created['id']}", headers=headers)
    assert response.status_code == 200

    response = client.get(f"/entities/{created['id']}", headers=headers)
    assert response.status_code == 404


def test_delete_entity_returns_404_for_unknown_id(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    response = client.delete("/entities/does-not-exist", headers=headers)
    assert response.status_code == 404


def test_delete_entity_blocked_by_foreign_key(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    owner = client.post(
        "/entities", json={"type": "person", "name": "Alex"}, headers=headers
    ).json()
    car = client.post(
        "/entities", json={"type": "vehicle", "name": "Corolla"}, headers=headers
    ).json()
    client.post(
        f"/entities/{owner['id']}/relationships",
        json={"to_entity_id": car["id"], "relationship_type": "owns"},
        headers=headers,
    )

    response = client.delete(f"/entities/{owner['id']}", headers=headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_patch_entity_timestamps_share_one_format(client, test_settings):
    # created_at comes from SQLite's CURRENT_TIMESTAMP ("YYYY-MM-DD HH:MM:SS",
    # UTC, no microseconds). updated_at must use the same mechanism, or a
    # single response carries two different timestamp formats and every
    # downstream consumer needs two parsers. EntityResponse types both as a
    # bare str, so nothing else catches a divergence here.
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    created = client.post(
        "/entities", json={"type": "pet", "name": "Rex"}, headers=headers
    ).json()

    body = client.patch(
        f"/entities/{created['id']}", json={"status": "deceased"}, headers=headers
    ).json()

    assert body["updated_at"] is not None
    for field in ("created_at", "updated_at"):
        datetime.strptime(body[field], "%Y-%m-%d %H:%M:%S")


def test_create_project_entity(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    response = client.post(
        "/entities",
        json={"type": "project", "name": "sunny hill bakery"},
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "project"
    assert body["name"] == "sunny hill bakery"


def test_entity_accepts_archived_status(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    created = client.post(
        "/entities",
        json={"type": "project", "name": "old thing"},
        headers=headers,
    ).json()
    response = client.patch(
        f"/entities/{created['id']}", json={"status": "archived"}, headers=headers
    )
    assert response.status_code == 200
    assert response.json()["status"] == "archived"


# --- forward-dated attributes (D17) ------------------------------------


@pytest.mark.parametrize(
    ("entity_type", "attribute", "value"),
    [
        ("property", "insurance_renewal", "2026-11-01"),
        ("property", "tax_due", "2026-02-28"),
        ("vehicle", "registration_expiry", "2027-03-31"),
        ("vehicle", "warranty_end", "2026-04-10"),
        ("pet", "vaccination_due", "2026-05-12"),
    ],
)
def test_forward_dated_attribute_round_trips(
    client, test_settings, entity_type, attribute, value
):
    """Each of these was a 422 before D17, because the attribute models
    are extra="forbid" — so a "what's due" digest had nothing to read for
    a property, vehicle or pet. Asserts the stored value comes back, not
    just that the write was accepted: attributes are a JSON blob, and a
    field the model drops would still return 200.
    """
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    created = client.post(
        "/entities",
        json={"type": entity_type, "name": "thing", "attributes": {attribute: value}},
        headers=headers,
    )
    assert created.status_code == 200, created.json()
    assert created.json()["attributes"][attribute] == value

    fetched = client.get(f"/entities/{created.json()['id']}", headers=headers)
    assert fetched.json()["attributes"][attribute] == value


def test_a_misspelled_forward_dated_attribute_is_still_rejected(client, test_settings):
    """The fields were added to the model, not the guard removed. A typo
    has to stay a 422 — extra="forbid" is the only thing standing between
    "registration_expires" and a date the digest will never look at.
    """
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    response = client.post(
        "/entities",
        json={
            "type": "vehicle",
            "name": "RAV4",
            "attributes": {"registration_expires": "2027-03-31"},
        },
        headers=headers,
    )
    assert response.status_code == 422


def test_a_forward_dated_attribute_must_be_a_date(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    response = client.post(
        "/entities",
        json={
            "type": "pet",
            "name": "Rex",
            "attributes": {"vaccination_due": "next spring"},
        },
        headers=headers,
    )
    assert response.status_code == 422


def test_attribute_dates_are_all_classified():
    """Every date-typed attribute is either forward-dated or historical.

    The /due digest reads FORWARD_DATED_ATTRIBUTES. A date field added to
    an attribute model and left off both lists is invisible to "what's
    due" and nothing reports it — the symptom is a renewal that never
    appears, which looks like "nothing is due" rather than like a bug.
    This fails the moment such a field is added.

    Top-level fields only: Mortgage.origination_date is nested inside
    property.mortgage and is not an attribute in its own right. A nested
    forward-dated date would need this walk extended.
    """
    unclassified = {}
    for entity_type, model in ATTRIBUTE_MODELS.items():
        date_fields = {
            name
            for name, field in model.model_fields.items()
            if field.annotation in (date, date | None)
        }
        classified = set(FORWARD_DATED_ATTRIBUTES.get(entity_type, ())) | set(
            HISTORICAL_DATE_ATTRIBUTES.get(entity_type, ())
        )
        if date_fields - classified:
            unclassified[entity_type] = sorted(date_fields - classified)

    assert not unclassified, (
        f"date attributes classified as neither forward-dated nor "
        f"historical: {unclassified} — add each to FORWARD_DATED_ATTRIBUTES "
        f"or HISTORICAL_DATE_ATTRIBUTES in api/models.py, and to the "
        f"table in runbooks/entity-attribute-schemas.md"
    )


def test_the_classification_lists_name_real_fields():
    """The other direction: a renamed or removed attribute left behind on
    a list would have the digest querying a field that no longer exists,
    silently returning nothing.
    """
    for table, label in (
        (FORWARD_DATED_ATTRIBUTES, "FORWARD_DATED_ATTRIBUTES"),
        (HISTORICAL_DATE_ATTRIBUTES, "HISTORICAL_DATE_ATTRIBUTES"),
    ):
        for entity_type, names in table.items():
            assert entity_type in ATTRIBUTE_MODELS, f"{label}: unknown {entity_type}"
            fields = ATTRIBUTE_MODELS[entity_type].model_fields
            for name in names:
                assert name in fields, f"{label}[{entity_type}]: no such field {name}"
                assert fields[name].annotation in (date, date | None), (
                    f"{label}[{entity_type}].{name} is not date-typed"
                )


# --- relationship creation: idempotent, or refused (ticket T-32 / D71) -------


def _pair(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    owner = client.post(
        "/entities", json={"type": "person", "name": "Alex"}, headers=headers
    ).json()
    car = client.post(
        "/entities", json={"type": "vehicle", "name": "Corolla"}, headers=headers
    ).json()
    return headers, owner, car


def test_an_identical_re_post_is_still_idempotent(client, test_settings):
    """The safe case, and the reason this is not a blanket 409.

    A skill step re-run or a network retry sends the same body. Turning
    that into an error would make the one operation a caller is entitled
    to repeat the one that fails.
    """
    headers, owner, car = _pair(client, test_settings)
    payload = {
        "to_entity_id": car["id"],
        "relationship_type": "owns",
        "attributes": {"note": "primary"},
    }

    first = client.post(
        f"/entities/{owner['id']}/relationships", json=payload, headers=headers
    )
    second = client.post(
        f"/entities/{owner['id']}/relationships", json=payload, headers=headers
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]


def test_a_differing_payload_is_refused_rather_than_discarded(
    client, test_settings
):
    """The defect: this used to return 200 carrying the OLD attributes,
    so a caller correcting a relationship believed the fix landed."""
    headers, owner, car = _pair(client, test_settings)
    client.post(
        f"/entities/{owner['id']}/relationships",
        json={
            "to_entity_id": car["id"],
            "relationship_type": "owns",
            "attributes": {"note": "primary"},
        },
        headers=headers,
    )

    response = client.post(
        f"/entities/{owner['id']}/relationships",
        json={
            "to_entity_id": car["id"],
            "relationship_type": "owns",
            "attributes": {"note": "CORRECTED"},
        },
        headers=headers,
    )

    # 409 explicitly now (ticket T-33). This asserted only `!= 200` while
    # "conflict" was 400 here, so the test would survive the status move
    # either way — deliberate then, and too weak to keep once the code
    # is settled.
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "conflict"
    assert "attributes" in response.text

    # And the stored row is untouched — a refusal that half-applied
    # would be worse than the silent discard it replaces.
    listing = client.get(
        f"/entities/{owner['id']}/relationships", headers=headers
    ).json()
    assert listing["total"] == 1
    assert listing["items"][0]["attributes"] == {"note": "primary"}


@pytest.mark.parametrize("field", ["start_date", "end_date"])
def test_a_differing_date_is_refused(client, test_settings, field):
    headers, owner, car = _pair(client, test_settings)
    base = {"to_entity_id": car["id"], "relationship_type": "owns"}
    client.post(
        f"/entities/{owner['id']}/relationships",
        json={**base, field: "2026-01-01"},
        headers=headers,
    )

    response = client.post(
        f"/entities/{owner['id']}/relationships",
        json={**base, field: "2026-06-01"},
        headers=headers,
    )

    assert response.status_code == 409, response.text
    assert field in response.text


def test_a_symmetric_relationship_posted_the_other_way_is_not_a_conflict(
    client, test_settings
):
    """Direction is excluded from the comparison on purpose.

    A symmetric type is stored once in whichever direction it was first
    recorded, so `B spouse_of A` legitimately matches a stored
    `A spouse_of B`. Comparing direction would refuse the correct case —
    which is the failure a naive "the payload differs" check produces.
    """
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    alex = client.post(
        "/entities", json={"type": "person", "name": "Alex"}, headers=headers
    ).json()
    sam = client.post(
        "/entities", json={"type": "person", "name": "Sam"}, headers=headers
    ).json()
    first = client.post(
        f"/entities/{alex['id']}/relationships",
        json={"to_entity_id": sam["id"], "relationship_type": "spouse_of"},
        headers=headers,
    )

    reverse = client.post(
        f"/entities/{sam['id']}/relationships",
        json={"to_entity_id": alex["id"], "relationship_type": "spouse_of"},
        headers=headers,
    )

    assert first.status_code == 200
    assert reverse.status_code == 200
    assert reverse.json()["id"] == first.json()["id"]


def test_a_dated_type_still_accumulates_rather_than_conflicting(
    client, test_settings
):
    """`leases` and `resides_at` are expected to accumulate over time —
    a lease renewal is a new row, not a conflicting edit. The conflict
    only applies where dedup applies."""
    headers, owner, car = _pair(client, test_settings)
    base = {"to_entity_id": car["id"], "relationship_type": "leases"}
    first = client.post(
        f"/entities/{owner['id']}/relationships",
        json={**base, "start_date": "2025-01-01", "end_date": "2025-12-31"},
        headers=headers,
    )
    second = client.post(
        f"/entities/{owner['id']}/relationships",
        json={**base, "start_date": "2026-01-01", "end_date": "2026-12-31"},
        headers=headers,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] != second.json()["id"]


@pytest.mark.parametrize(
    "relationship_type",
    [
        "owns",
        "resides_at",
        "insures",
        "finances",
        "maintains",
        "leases",
        "spouse_of",
        "parent_of",
    ],
)
def test_no_relationship_type_may_point_at_itself(
    client, test_settings, relationship_type
):
    """Every type, not a sample. Each relates two distinct things, and
    "A owns A" is malformed rather than merely unusual.

    One concrete consequence, recorded because it is safe by accident
    rather than by design: `cost_of_ownership` walks `insures` and
    `finances` to build its entity list, so a self-insuring account
    lands in its own list. It does not double-count today only because
    SQL `IN` collapses the duplicate — rewrite that query as a join and
    it would.
    """
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    entity = client.post(
        "/entities", json={"type": "person", "name": "Alex"}, headers=headers
    ).json()

    response = client.post(
        f"/entities/{entity['id']}/relationships",
        json={
            "to_entity_id": entity["id"],
            "relationship_type": relationship_type,
        },
        headers=headers,
    )

    assert response.status_code == 422, response.text
    # The envelope strips the leading "body" segment by design, so the
    # field reads as the caller wrote it (api/main.py's renderer).
    assert response.json()["error"]["details"][0]["field"] == "to_entity_id"


def test_a_self_reference_is_not_reported_as_an_unknown_entity(
    client, test_settings
):
    """422, not InvalidReferenceError's 400. D17 made 400 mean "refers
    to something that does not exist", and this refers to something that
    plainly does — telling a caller their own id is unknown would send
    them looking for the wrong problem."""
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    entity = client.post(
        "/entities", json={"type": "person", "name": "Alex"}, headers=headers
    ).json()

    response = client.post(
        f"/entities/{entity['id']}/relationships",
        json={"to_entity_id": entity["id"], "relationship_type": "owns"},
        headers=headers,
    )

    assert response.status_code == 422
    assert "does not exist" not in response.text.lower()


def test_reordered_attribute_keys_are_the_same_payload(client, test_settings):
    """`_differing_fields` compares attributes as VALUES, not as text.

    The docstring said so and nothing enforced it (review-1 on #114).
    Switching to a text comparison leaves the rest of this file green,
    and it is not harmless: two dicts with identical values in a
    different key order serialise differently, so an honest re-POST
    would get a conflict where it used to get its row back. Dict order
    is not something a caller controls or should have to.

    Deliberately `insures` — neither dated nor symmetric, so the
    point-in-time dedup branch actually runs. review-1's first probes
    used `resides_at`, where `_differing_fields` is never reached and
    the mutation therefore looked harmless: the test has to use a type
    that dedups, or it measures nothing.
    """
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    policy = client.post(
        "/entities", json={"type": "account", "name": "Policy"}, headers=headers
    ).json()
    car = client.post(
        "/entities", json={"type": "vehicle", "name": "Corolla"}, headers=headers
    ).json()

    first = client.post(
        f"/entities/{policy['id']}/relationships",
        json={
            "to_entity_id": car["id"],
            "relationship_type": "insures",
            "attributes": {"policy_no": "AB-1", "excess": 500},
        },
        headers=headers,
    )
    reordered = client.post(
        f"/entities/{policy['id']}/relationships",
        json={
            "to_entity_id": car["id"],
            "relationship_type": "insures",
            "attributes": {"excess": 500, "policy_no": "AB-1"},
        },
        headers=headers,
    )

    assert first.status_code == 200, first.text
    assert reordered.status_code == 200, reordered.text
    assert reordered.json()["id"] == first.json()["id"]


# --------------------------------------------------------------------------
# Deleting an entity a note points at (ticket T-34 / D88).
#
# note_links.target_id is polymorphic and carries no FK, so SQLite never
# refuses on its behalf. Before D88 the delete returned 200 {"deleted":
# true} and left the link pointing at an id that no longer resolved.
#
# Status codes are asserted by ERROR CODE NAME rather than by number:
# ticket T-33 moves conflict from 400 to 409, and these tests are about which
# refusal fires, not which number carries it.
# --------------------------------------------------------------------------

import sqlite3
import uuid as _uuid


def _link_note_to(test_settings, target_id, target_type="entity"):
    """Insert a note and a link to `target_id`, returning the note id.

    Direct SQL because there are no notes routes yet -- that is precisely
    why the orphan went unnoticed.
    """
    connection = sqlite3.connect(test_settings.db_path)
    note_id = str(_uuid.uuid4())
    connection.execute("INSERT INTO notes (id, body) VALUES (?, ?)", (note_id, "n"))
    connection.execute(
        "INSERT INTO note_links (id, note_id, target_type, target_id) "
        "VALUES (?, ?, ?, ?)",
        (str(_uuid.uuid4()), note_id, target_type, target_id),
    )
    connection.commit()
    connection.close()
    return note_id


def _count_links(test_settings, target_id):
    connection = sqlite3.connect(test_settings.db_path)
    try:
        return connection.execute(
            "SELECT count(*) FROM note_links WHERE target_id = ?", (target_id,)
        ).fetchone()[0]
    finally:
        connection.close()


def test_delete_entity_refused_when_a_note_links_to_it(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    entity = client.post(
        "/entities", json={"type": "vehicle", "name": "Corolla"}, headers=headers
    ).json()
    note_id = _link_note_to(test_settings, entity["id"])

    response = client.delete(f"/entities/{entity['id']}", headers=headers)

    assert response.json()["error"]["code"] == "conflict"
    # 409 explicitly. This asserted a RANGE (400 <= code < 500) while
    # "conflict" was still 400 here, so it would have passed at 400,
    # 409, 422 or 451 alike -- deliberate then, and too weak to keep
    # once ticket T-33 settled the code. The count in test_errors.py says
    # this raise site EXISTS; only this line says anyone watches what
    # it answers.
    assert response.status_code == 409
    # The entity survives -- a refusal that still deleted would be worse
    # than the orphan it was meant to prevent.
    assert client.get(f"/entities/{entity['id']}", headers=headers).status_code == 200
    assert _count_links(test_settings, entity["id"]) == 1


def test_the_refusal_names_the_blocking_note(client, test_settings):
    """A count alone is unactionable: you cannot unlink what you can't name."""
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    entity = client.post(
        "/entities", json={"type": "pet", "name": "Rex"}, headers=headers
    ).json()
    note_id = _link_note_to(test_settings, entity["id"])

    message = client.delete(f"/entities/{entity['id']}", headers=headers).json()[
        "error"
    ]["message"]

    assert note_id in message, f"the note id is not in the refusal: {message}"
    assert "note_links.target_id (1)" in message


def test_the_refusal_names_which_tables_block_it(client, test_settings):
    """"Still referenced by other records" names nothing and helps nobody.

    With two different kinds of reference the message has to distinguish
    them, or the caller clears one and is refused again for the other with
    the same opaque sentence.
    """
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    owner = client.post(
        "/entities", json={"type": "person", "name": "Alex"}, headers=headers
    ).json()
    car = client.post(
        "/entities", json={"type": "vehicle", "name": "Corolla"}, headers=headers
    ).json()
    client.post(
        f"/entities/{owner['id']}/relationships",
        json={"to_entity_id": car["id"], "relationship_type": "owns"},
        headers=headers,
    )
    _link_note_to(test_settings, owner["id"])

    message = client.delete(f"/entities/{owner['id']}", headers=headers).json()[
        "error"
    ]["message"]

    assert "entity_relationships.from_entity_id (1)" in message
    assert "note_links.target_id (1)" in message


def test_the_refusal_points_at_archiving(client, test_settings):
    """The case where this refusal is annoying is the case where the caller
    wanted `archived`, so the refusal says so rather than just saying no."""
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    entity = client.post(
        "/entities", json={"type": "vehicle", "name": "Corolla"}, headers=headers
    ).json()
    _link_note_to(test_settings, entity["id"])

    message = client.delete(f"/entities/{entity['id']}", headers=headers).json()[
        "error"
    ]["message"]

    assert "archived" in message
    # And archiving actually works while the link exists -- otherwise the
    # advice would be a dead end.
    patched = client.patch(
        f"/entities/{entity['id']}", json={"status": "archived"}, headers=headers
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "archived"
    assert _count_links(test_settings, entity["id"]) == 1


def test_delete_entity_with_no_references_still_works(client, test_settings):
    """The guard must not refuse everything -- including when a note links
    to a DIFFERENT entity, which would catch a query missing its WHERE."""
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    doomed = client.post(
        "/entities", json={"type": "pet", "name": "Rex"}, headers=headers
    ).json()
    other = client.post(
        "/entities", json={"type": "pet", "name": "Spot"}, headers=headers
    ).json()
    _link_note_to(test_settings, other["id"])

    response = client.delete(f"/entities/{doomed['id']}", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"deleted": True}
    assert client.get(f"/entities/{doomed['id']}", headers=headers).status_code == 404


def test_a_note_link_of_another_target_type_does_not_block(client, test_settings):
    """target_id is only meaningful alongside target_type.

    Counting target_id alone would let a note attached to a transaction
    block deletion of an entity that happens to share the id. Ids are uuids
    so this is unlikely, but the query would be wrong on its face.
    """
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    entity = client.post(
        "/entities", json={"type": "pet", "name": "Rex"}, headers=headers
    ).json()
    _link_note_to(test_settings, entity["id"], target_type="transaction")

    response = client.delete(f"/entities/{entity['id']}", headers=headers)

    assert response.status_code == 200, response.text


# --- PATCH / GET a relationship by id (ticket T-35 / D93) --------------------


def _linked(client, test_settings, **extra):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    owner = client.post(
        "/entities", json={"type": "person", "name": "Alex"}, headers=headers
    ).json()
    car = client.post(
        "/entities", json={"type": "vehicle", "name": "Corolla"}, headers=headers
    ).json()
    body = {"to_entity_id": car["id"], "relationship_type": "leases", **extra}
    rel = client.post(
        f"/entities/{owner['id']}/relationships", json=body, headers=headers
    )
    assert rel.status_code == 200, rel.text
    return headers, owner, car, rel.json()


def test_get_relationship_reads_one_by_id(client, test_settings):
    """Added with PATCH rather than after it: without this the PATCH
    response would be the only by-id read, so checking what a row holds
    would mean writing to it."""
    headers, _, _, rel = _linked(client, test_settings, start_date="2026-01-01")

    fetched = client.get(f"/relationships/{rel['id']}", headers=headers)

    assert fetched.status_code == 200
    assert fetched.json() == rel


def test_get_an_unknown_relationship_is_a_404(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}

    assert client.get("/relationships/nope", headers=headers).status_code == 404


def test_an_explicit_null_clears_a_date(client, test_settings):
    """The case D71 made unreachable any other way.

    `create_relationship` now refuses a differing payload, so a lease
    ended by mistake could not be corrected by re-creating it — and
    with `None` meaning "not supplied", it could not be corrected by
    PATCH either. Explicit null is the only route back.
    """
    headers, _, _, rel = _linked(
        client, test_settings, start_date="2026-01-01", end_date="2026-06-30"
    )

    patched = client.patch(
        f"/relationships/{rel['id']}", json={"end_date": None}, headers=headers
    )

    assert patched.status_code == 200, patched.text
    assert patched.json()["end_date"] is None
    assert patched.json()["start_date"] == "2026-01-01", "untouched key preserved"
    # Round-trip: the clear is stored, not just reflected in the response.
    assert client.get(f"/relationships/{rel['id']}", headers=headers).json()[
        "end_date"
    ] is None


def test_an_absent_key_leaves_the_value_alone(client, test_settings):
    """The other half, and the one that makes the first meaningful: if
    absent also cleared, every PATCH would wipe whatever it did not
    mention."""
    headers, _, _, rel = _linked(
        client, test_settings, start_date="2026-01-01", end_date="2026-06-30"
    )

    patched = client.patch(
        f"/relationships/{rel['id']}",
        json={"attributes": {"rent": 1200}},
        headers=headers,
    )

    assert patched.json()["start_date"] == "2026-01-01"
    assert patched.json()["end_date"] == "2026-06-30"
    assert patched.json()["attributes"] == {"rent": 1200}


def test_attributes_replace_rather_than_merge(client, test_settings):
    """Merging would make it impossible to REMOVE a key — the same trap
    one level down from the one explicit-null exists to avoid."""
    headers, _, _, rel = _linked(
        client, test_settings, attributes={"rent": 1200, "deposit": 2400}
    )

    patched = client.patch(
        f"/relationships/{rel['id']}",
        json={"attributes": {"rent": 1300}},
        headers=headers,
    )

    assert patched.json()["attributes"] == {"rent": 1300}


def test_an_explicit_null_clears_attributes(client, test_settings):
    headers, _, _, rel = _linked(client, test_settings, attributes={"rent": 1200})

    patched = client.patch(
        f"/relationships/{rel['id']}", json={"attributes": None}, headers=headers
    )

    assert patched.json()["attributes"] == {}


def test_an_empty_patch_is_refused(client, test_settings):
    """A 200 on an empty body reads exactly like a successful edit."""
    headers, _, _, rel = _linked(client, test_settings)

    response = client.patch(f"/relationships/{rel['id']}", json={}, headers=headers)

    assert response.status_code == 422
    assert "at least one" in response.text


@pytest.mark.parametrize(
    "field", ["to_entity_id", "from_entity_id", "relationship_type"]
)
def test_the_dedup_key_cannot_be_patched(client, test_settings, field):
    """Those three are what `create_relationship` dedups on, so changing
    one would not amend this relationship — it would make it a different
    one, with the original still there. Rejected rather than ignored."""
    headers, _, car, rel = _linked(client, test_settings)

    response = client.patch(
        f"/relationships/{rel['id']}",
        json={field: car["id"] if field.endswith("entity_id") else "owns"},
        headers=headers,
    )

    assert response.status_code == 422, response.text


def test_patching_cannot_create_a_duplicate_create_would_refuse(
    client, test_settings
):
    """The consequence of the key being untouchable, asserted rather
    than reasoned: whatever a PATCH changes, the row still dedups the
    same way, so no two rows can be made to collide."""
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    owner = client.post(
        "/entities", json={"type": "person", "name": "Alex"}, headers=headers
    ).json()
    car = client.post(
        "/entities", json={"type": "vehicle", "name": "Corolla"}, headers=headers
    ).json()
    base = {"to_entity_id": car["id"], "relationship_type": "leases"}
    first = client.post(
        f"/entities/{owner['id']}/relationships",
        json={**base, "start_date": "2025-01-01"},
        headers=headers,
    ).json()
    second = client.post(
        f"/entities/{owner['id']}/relationships",
        json={**base, "start_date": "2026-01-01"},
        headers=headers,
    ).json()

    # Make the second identical to the first in every patchable field.
    client.patch(
        f"/relationships/{second['id']}",
        json={"start_date": "2025-01-01"},
        headers=headers,
    )

    listing = client.get(
        f"/entities/{owner['id']}/relationships", headers=headers
    ).json()
    assert listing["total"] == 2, "a dated type accumulates; both rows remain"
    assert {r["id"] for r in listing["items"]} == {first["id"], second["id"]}


def test_a_patched_symmetric_row_reads_changed_from_both_directions(
    client, test_settings
):
    """The symmetric reversal is a non-issue here BY CONSTRUCTION, and
    this is what that means: one stored row, addressed by id, read from
    either end. A direction-aware branch in the PATCH could never fire.
    """
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}
    alex = client.post(
        "/entities", json={"type": "person", "name": "Alex"}, headers=headers
    ).json()
    sam = client.post(
        "/entities", json={"type": "person", "name": "Sam"}, headers=headers
    ).json()
    rel = client.post(
        f"/entities/{alex['id']}/relationships",
        json={"to_entity_id": sam["id"], "relationship_type": "spouse_of"},
        headers=headers,
    ).json()

    client.patch(
        f"/relationships/{rel['id']}",
        json={"attributes": {"since": "2020"}},
        headers=headers,
    )

    for entity in (alex, sam):
        listing = client.get(
            f"/entities/{entity['id']}/relationships", headers=headers
        ).json()
        assert listing["total"] == 1
        assert listing["items"][0]["attributes"] == {"since": "2020"}


def test_patching_an_unknown_relationship_is_a_404(client, test_settings):
    headers = {"Authorization": f"Bearer {test_settings.api_token}"}

    response = client.patch(
        "/relationships/nope", json={"attributes": {}}, headers=headers
    )

    assert response.status_code == 404
