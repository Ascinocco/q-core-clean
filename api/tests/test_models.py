import pytest
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from api.models import (
    CategoryCreate,
    EntityCreate,
    EntityUpdate,
    ImportStatementRequest,
    MerchantRuleCreate,
    StatementCreate,
    TransactionInput,
    TransactionUpdate,
    validate_entity_attributes,
)


def test_validate_entity_attributes_normalizes_property_attributes():
    result = validate_entity_attributes(
        "property",
        {
            "address": {"street": "1 Main St", "city": "Springfield"},
            "property_type": "rental",
            "purchase_price": 350000,
        },
    )

    assert result == {
        "address": {"street": "1 Main St", "city": "Springfield"},
        "property_type": "rental",
        "purchase_price": 350000.0,
    }


def test_validate_entity_attributes_rejects_unknown_field():
    with pytest.raises(RequestValidationError):
        validate_entity_attributes(
            "vehicle", {"vin": "1HGCM82633A123456", "color": "blue"}
        )


def test_validate_entity_attributes_rejects_invalid_enum_value():
    with pytest.raises(RequestValidationError):
        validate_entity_attributes("account", {"account_subtype": "cryptocurrency"})


def test_validate_entity_attributes_allows_empty_attributes():
    assert validate_entity_attributes("pet", {}) == {}


def test_validate_entity_attributes_parses_nested_mortgage():
    result = validate_entity_attributes(
        "property",
        {
            "mortgage": {
                "lender": "Example Lender",
                "origination_date": "2015-05-01",
                "term_years": 30,
                "rate": 4.1,
            }
        },
    )

    assert result == {
        "mortgage": {
            "lender": "Example Lender",
            "origination_date": "2015-05-01",
            "term_years": 30,
            "rate": 4.1,
        }
    }


def test_entity_create_rejects_unknown_type():
    with pytest.raises(ValidationError):
        EntityCreate(type="robot", name="R2D2")


def test_entity_create_rejects_unknown_status():
    with pytest.raises(ValidationError):
        EntityCreate(type="pet", name="Rex", status="lost")


def test_entity_create_defaults_status_to_active():
    entity = EntityCreate(type="pet", name="Rex")
    assert entity.status == "active"
    assert entity.attributes == {}


def test_entity_update_allows_partial_fields():
    update = EntityUpdate(status="sold")
    assert update.name is None
    assert update.status == "sold"
    assert update.attributes is None


def test_entity_update_rejects_type_field():
    with pytest.raises(ValidationError):
        EntityUpdate(type="vehicle")


from api.models import RelationshipCreate


def test_relationship_create_rejects_unknown_relationship_type():
    with pytest.raises(ValidationError):
        RelationshipCreate(to_entity_id="abc", relationship_type="rents_from")


def test_relationship_create_accepts_known_relationship_type():
    relationship = RelationshipCreate(to_entity_id="abc", relationship_type="owns")
    assert relationship.relationship_type == "owns"
    assert relationship.start_date is None
    assert relationship.attributes == {}


def test_relationship_create_parses_dates():
    relationship = RelationshipCreate(
        to_entity_id="abc",
        relationship_type="leases",
        start_date="2026-01-01",
        end_date="2026-12-31",
    )
    assert relationship.start_date.isoformat() == "2026-01-01"
    assert relationship.end_date.isoformat() == "2026-12-31"


def test_validate_entity_attributes_rejects_full_card_number_in_last4():
    """A 16-digit card number must not reach the DB via account.last4.

    Rejected, not truncated: silently storing "1111" from a full card number
    would hide the intake-layer bug that sent it and leave no signal that
    something upstream is leaking full account numbers.
    """
    with pytest.raises(RequestValidationError):
        validate_entity_attributes("account", {"last4": "4111111111111111"})


def test_validate_entity_attributes_rejects_full_ssn_in_id_last4():
    with pytest.raises(RequestValidationError):
        validate_entity_attributes("person", {"id_last4": "123456789"})


def test_validate_entity_attributes_accepts_four_char_last4():
    assert validate_entity_attributes("account", {"last4": "1111"}) == {
        "last4": "1111"
    }
    assert validate_entity_attributes("person", {"id_last4": "6789"}) == {
        "id_last4": "6789"
    }


def test_validate_entity_attributes_accepts_shorter_than_four_last4():
    """max_length, not exact length — a masked value may be shorter."""
    assert validate_entity_attributes("account", {"last4": "11"}) == {"last4": "11"}


def test_validate_entity_attributes_rejects_five_char_last4():
    with pytest.raises(RequestValidationError):
        validate_entity_attributes("account", {"last4": "11111"})


def test_rejected_sensitive_value_is_not_echoed_in_the_validation_error():
    """The rejection must not leak what it rejected.

    Pydantic puts the offending value in each error's `input`, and that error
    list becomes the 422 response body — which travels back to the MCP client
    and therefore into a Claude session's context. Capping the length without
    this would stop the full number reaching the DB and reroute it into an LLM
    instead, which CLAUDE.md's ground rule forbids just as squarely.
    """
    with pytest.raises(RequestValidationError) as exc_info:
        validate_entity_attributes("account", {"last4": "4111111111111111"})

    errors = exc_info.value.errors()
    assert "4111111111111111" not in repr(errors)
    assert errors[0]["input"] == "[redacted]"
    # The useful part of the diagnostic survives. The loc carries the
    # ("body", "attributes") prefix added below, so redaction and
    # re-prefixing compose rather than one clobbering the other.
    assert errors[0]["loc"] == ("body", "attributes", "last4")
    assert errors[0]["type"] == "string_too_long"


def test_non_sensitive_validation_errors_still_report_their_input():
    with pytest.raises(RequestValidationError) as exc_info:
        validate_entity_attributes("account", {"account_subtype": "cryptocurrency"})

    assert exc_info.value.errors()[0]["input"] == "cryptocurrency"

def test_validate_entity_attributes_prefixes_error_loc_like_top_level_fields():
    # A bad nested attribute must report the same loc shape as a bad
    # top-level field (["body", "type"]), not a bare ["purchase_date"] a
    # client can't distinguish from a top-level field of that name.
    with pytest.raises(RequestValidationError) as exc_info:
        validate_entity_attributes("property", {"purchase_date": "not-a-date"})

    locs = [tuple(error["loc"]) for error in exc_info.value.errors()]
    assert locs == [("body", "attributes", "purchase_date")]


def test_validate_entity_attributes_prefixes_deeply_nested_error_loc():
    with pytest.raises(RequestValidationError) as exc_info:
        validate_entity_attributes("property", {"mortgage": {"term_years": "soon"}})

    locs = [tuple(error["loc"]) for error in exc_info.value.errors()]
    assert locs == [("body", "attributes", "mortgage", "term_years")]


def test_category_create_defaults_parent_to_none():
    category = CategoryCreate(name="Streaming Services")
    assert category.parent_id is None


def test_statement_create_parses_dates():
    statement = StatementCreate(
        account_id="abc", period_start="2026-01-01", period_end="2026-01-31"
    )
    assert statement.period_start.isoformat() == "2026-01-01"


def test_transaction_input_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        TransactionInput(
            txn_date="2026-01-05",
            description="COFFEE SHOP",
            amount_cents=-450,
            category_id="food",  # not settable on import input
        )


def test_transaction_update_allows_partial_correction():
    update = TransactionUpdate(category_id="food")
    assert update.entity_id is None


def test_merchant_rule_create_requires_a_target():
    # at least one of category_id/entity_id must be set, or the rule does nothing
    with pytest.raises(ValidationError):
        MerchantRuleCreate(pattern="STARBUCKS")


def test_import_statement_request_holds_a_transaction_batch():
    request = ImportStatementRequest(
        account_id="abc",
        period_start="2026-01-01",
        period_end="2026-01-31",
        transactions=[
            TransactionInput(txn_date="2026-01-05", description="COFFEE", amount_cents=-450)
        ],
    )
    assert len(request.transactions) == 1


def test_statement_create_rejects_a_reversed_period():
    with pytest.raises(ValidationError):
        StatementCreate(
            account_id="abc", period_start="2026-06-30", period_end="2026-06-01"
        )


def test_import_statement_request_rejects_a_reversed_period():
    """The import path creates a statement too, so the check has to live on
    both models or it just moves the hole."""
    with pytest.raises(ValidationError):
        ImportStatementRequest(
            account_id="abc",
            period_start="2026-06-30",
            period_end="2026-06-01",
            transactions=[],
        )

def test_project_attributes_accepts_description():
    assert validate_entity_attributes("project", {"description": "ship it"}) == {
        "description": "ship it"
    }


def test_project_attributes_allows_empty():
    assert validate_entity_attributes("project", {}) == {}


def test_project_attributes_rejects_unknown_key():
    with pytest.raises(RequestValidationError):
        validate_entity_attributes("project", {"owner": "owner"})
