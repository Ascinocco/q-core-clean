"""Four surfaces that two live walks recorded as vacuous, pinned by fixture.

impl-3's and impl-1's independent walks of the live MCP surface could not
exercise these, because the live tables are empty where it counts: no
documents, no merchant-rule history, and every live transaction has
`entity_id` NULL. "It returned an empty list" is not evidence that a
shape is right -- an empty list is the one value every possible
implementation agrees on, correct or not.

So each is driven through `build_server` against the real API with
fixture rows that make the result NON-EMPTY, and every fixture asserts it
produced rows before anything asserts a shape.

ON DERIVING THE EXPECTED KEYS. Where the route declares a
`response_model`, that model is the authority. Where it does not --
merchant_rule_history and the three reports return plain dicts -- the
authority is the API's own response, fetched through the same app in the
same test. Comparing the tool's keys to the API's keys by set equality
states the property that matters: the MCP layer must not drop, rename or
invent a field on its way out. A retyped literal would pass happily while
both sides were wrong together.
"""

from tests.source_import_support import seed_source_batch

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from api.config import get_settings
from api.main import app
from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server


@pytest.fixture()
def wired(test_settings):
    """The MCP server and a direct API client, over the same app."""
    app.dependency_overrides[get_settings] = lambda: test_settings
    api = TestClient(app)
    api.headers.update({"Authorization": f"Bearer {test_settings.api_token}"})
    server = build_server(
        QCoreClient(test_settings, transport=httpx.ASGITransport(app=app))
    )
    try:
        yield server, api
    finally:
        app.dependency_overrides.clear()


# --- documents ---------------------------------------------------------


def _register_document(api, test_settings, name="scan.pdf"):
    """Register through the API, which takes an absolute `file_path`.

    Deliberately not the `register_document` TOOL: that one moves a file
    out of inbox/ and is already round-tripped elsewhere. What is under
    test here is the shape `list_documents` and `get_document` render,
    so the fixture takes the shortest honest path to a stored row --
    which still has to be inside inbox/, because register_document
    refuses a file_path outside the configured directories.
    """
    inbox = Path(test_settings.inbox_dir)
    inbox.mkdir(parents=True, exist_ok=True)
    source = inbox / name
    source.write_bytes(b"%PDF fixture")
    return api.post(
        "/documents",
        json={
            "file_path": str(source),
            "title": "Fixture Doc",
            "doc_type": "statement",
        },
    )


def test_document_item_shapes_match_the_response_model(
    wired, test_settings, call_tool
):
    """`list_documents` and `get_document` rendered shapes.

    Already covered: a round trip through build_server exists in
    test_tools_documents.py and asserts `fetched["id"]` and `listed["total"]`.
    It does NOT assert the item's key set, so
    a field added to or dropped from the response passed unnoticed. This
    is that assertion.
    """
    from api.models import DocumentResponse

    server, api = wired
    created = _register_document(api, test_settings)
    assert created.status_code == 200, created.text

    listed = call_tool(server, "list_documents", {})
    fetched = call_tool(server, "get_document", {"document_id": created.json()["id"]})

    assert listed["total"] >= 1, "fixture produced no documents -- shape is vacuous"
    assert listed["items"], "fixture produced no documents -- shape is vacuous"

    expected = set(DocumentResponse.model_fields)
    assert set(fetched) == expected
    # KEEP THIS LINE AS IT IS. It ties the UNMODELLED listing to the
    # modelled single read: `GET /documents` has no response_model, so this
    # guards its explicit column list, while
    # `GET /documents/{id}` is pinned by DocumentResponse. Comparing them
    # makes the model govern both. review-2 simulated the documents table
    # gaining a column: only this assertion reds -- which is exactly the
    # #130/#137 class, an endpoint that bypasses its response model and
    # hands back whatever the row has.
    assert set(listed["items"][0]) == set(fetched)


# --- merchant rule history ---------------------------------------------


def test_merchant_rule_history_item_shape_matches_the_api(wired, call_tool):
    """Covered at the API layer (test_rule_audit.py asserts `field` and
    `actor` on non-empty history) but NOT at the MCP layer at all, and
    never as a key set.
    """
    server, api = wired

    category = api.post("/categories", json={"name": "Coffee"}).json()
    rule = api.post(
        "/merchant_rules",
        json={"pattern": "TIM HORTON", "category_id": category["id"], "actor": "impl-4"},
    ).json()
    rule_id = rule["id"]
    api.patch(
        f"/merchant_rules/{rule_id}",
        json={"pattern": "MAPLE DONUTS", "actor": "impl-4"},
    )

    from_api = api.get(f"/merchant_rules/{rule_id}/history").json()
    from_tool = call_tool(server, "merchant_rule_history", {"rule_id": rule_id})

    assert from_api["items"], "fixture produced no history -- shape is vacuous"
    assert from_tool["items"], "the tool returned no history"
    assert set(from_tool["items"][0]) == set(from_api["items"][0])
    # The creation row plus the edit: history that records only one is a
    # different bug than history with the wrong shape.
    assert from_tool["total"] == 2


# --- the entity legs of the three reports -------------------------------
#
# Every live transaction has entity_id NULL, so both walks saw these
# return nothing and could say nothing about them. The fixture below is
# the smallest one that makes all three non-empty at once.


def _account(api, name="Main Card"):
    return api.post("/entities", json={"type": "account", "name": name}).json()


@pytest.fixture()
def car_with_costs(wired):
    """A vehicle with two direct transactions and one via a shared policy.

    The relationship leg is the half that only exists on this path: the
    premium is booked against the INSURANCE ACCOUNT, and reaches the car
    solely because that account `insures` it. A fixture with direct
    transactions only would leave cost_of_ownership's join untested while
    still returning a number.
    """
    _, api = wired

    car = api.post("/entities", json={"type": "vehicle", "name": "Corolla"}).json()
    # A SECOND vehicle on the same policy. Without it `shared_with` is
    # empty and the full-value reporting looks like plain double-counting
    # with nothing to disclose it -- the field only says anything when
    # the policy actually covers more than the entity being asked about.
    van = api.post("/entities", json={"type": "vehicle", "name": "Transit"}).json()
    policy = api.post(
        "/entities", json={"type": "account", "name": "Insurance Autopay"}
    ).json()
    for covered in (car, van):
        api.post(
            f"/entities/{policy['id']}/relationships",
            json={"to_entity_id": covered["id"], "relationship_type": "insures"},
        )

    main = _account(api)
    seed_source_batch(api, json={
            "account_id": main["id"],
            "period_start": "2026-08-01",
            "period_end": "2026-08-31",
            "transactions": [
                {"txn_date": "2026-08-05", "description": "GAS", "amount_cents": -4000},
                {"txn_date": "2026-08-12", "description": "TIRES", "amount_cents": -2500},
            ],
        })
    seed_source_batch(api, json={
            "account_id": policy["id"],
            "period_start": "2026-08-01",
            "period_end": "2026-08-31",
            "transactions": [
                {
                    "txn_date": "2026-08-01",
                    "description": "AUTO PREMIUM",
                    "amount_cents": -8000,
                }
            ],
        })

    direct = api.get(f"/transactions?account_id={main['id']}").json()["items"]
    assert len(direct) == 2, "the fixture's own import did not land"
    for row in direct:
        api.patch(f"/transactions/{row['id']}", json={"entity_id": car["id"]})

    premium = api.get(f"/transactions?account_id={policy['id']}").json()["items"]
    assert len(premium) == 1, "the fixture's own import did not land"
    api.patch(f"/transactions/{premium[0]['id']}", json={"entity_id": policy["id"]})

    return car, van, policy


def test_cost_of_ownership_sums_both_legs(car_with_costs, wired, call_tool):
    """The arithmetic, not "it returned something".

    -4000 and -2500 are booked directly against the car; -8000 is booked
    against the insurance account and reaches the car only through the
    `insures` relationship. The total is the sum at FULL value -- this
    report does not apportion a shared policy across what it covers (D66
    left that decision open), and asserting a divided figure here would
    pin a behaviour the API does not have.

    Which is exactly why `shared_with` is asserted too. The same premium
    appears in full against the van as well, so the number alone would be
    a silent double-count; that field is where the response discloses it,
    and it is the reason full-value-per-entity is honest rather than
    wrong. Adding the two vehicles' totals is the mistake it exists to
    prevent.
    """
    car, van, _ = car_with_costs
    server, api = wired

    report = call_tool(server, "cost_of_ownership", {"entity_id": car["id"], "period": "2026-08"})

    assert report["total"] == -14500, report
    # And each leg is load-bearing: dropping either changes the total, so
    # the number cannot be reached by one leg alone.
    assert report["total"] != -6500, "the relationship leg contributed nothing"
    assert report["total"] != -8000, "the direct leg contributed nothing"

    # The field that makes full-value reporting honest rather than a
    # silent double-count (#109, D66: apportioning was left to the owner).
    # The premium appears in full against BOTH vehicles, and this is
    # where the response says so.
    assert report["shared_cost_entities"] == 1, report["shared_with"]
    shared = report["shared_with"]
    assert [row["entity_id"] for row in shared] == [van["id"]], shared
    assert shared[0]["entity_name"] == "Transit"
    assert shared[0]["via_entity_name"] == "Insurance Autopay"
    assert shared[0]["relationship_type"] == "insures"
    # The queried entity is never listed as sharing with itself.
    assert car["id"] not in {row["entity_id"] for row in shared}

    from_api = api.get(
        f"/entities/{car['id']}/cost_of_ownership?period=2026-08"
    ).json()
    assert set(report) == set(from_api)
    assert report["shared_with"] == from_api["shared_with"]


def test_spending_summary_groups_by_entity_with_rows(car_with_costs, wired, call_tool):
    car, _van, _ = car_with_costs
    server, api = wired

    grouped = call_tool(
        server, "spending_summary", {"period": "2026-08", "group_by": "entity"}
    )

    assert grouped["items"], "fixture produced no groups -- shape is vacuous"
    by_key = {item["key"]: item for item in grouped["items"]}
    assert car["id"] in by_key, f"the car did not appear: {list(by_key)}"
    assert by_key[car["id"]]["total"] == -6500, by_key[car["id"]]

    from_api = api.get("/spending_summary?period=2026-08&group_by=entity").json()
    assert set(grouped["items"][0]) == set(from_api["items"][0])


def test_trend_filtered_by_entity_has_rows(car_with_costs, wired, call_tool):
    car, _van, _ = car_with_costs
    server, api = wired

    series = call_tool(server, "trend", {"entity_id": car["id"], "months": 12})

    assert series["items"], "fixture produced no points -- shape is vacuous"
    august = [point for point in series["items"] if point["month"] == "2026-08"]
    assert august, f"the fixture's month is absent: {[p['month'] for p in series['items']]}"
    assert august[0]["total"] == -6500

    from_api = api.get(f"/trend?entity_id={car['id']}&months=12").json()
    assert set(series["items"][0]) == set(from_api["items"][0])
