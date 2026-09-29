"""The vocabulary in the MCP tool descriptions must match what the API accepts.

`q_core_mcp/tools/entities.py` spells the entity type, status and
relationship vocabularies out as prose, because that prose is what a model
reads when deciding which tool to call and what to pass. `api/models.py`
spells the same vocabularies out as `Literal` types, because those are what
the API validates against. Two lists describing one set, maintained
separately — the same shape as `ALL_STATUS_ORDER` vs `TicketStatus`, which
`api/tests/test_jyra_models.py::test_board_columns_match_the_status_vocabulary`
already pins for exactly this reason.

Both drifted the moment Jyra Task 1 added `project` to `EntityType` and
`archived` to `EntityStatus`: the API accepted both, and the MCP
descriptions did not mention either, so a model reading `create_entity`
could not discover that projects exist. Nothing failed — the tools kept
working for every value they did list, which is what let it go unnoticed.

Drift in the other direction is just as bad and less obvious: a value
advertised here but rejected by the API sends the model into a 422 it
cannot fix, so these assert set equality rather than "the description
mentions everything".
"""

from typing import get_args

from api.models import EntityStatus, EntityType, RelationshipType
from q_core_mcp.tools.entities import (
    ENTITY_STATUSES,
    ENTITY_TYPES,
    RELATIONSHIP_TYPES,
)


def _advertised(description_fragment: str) -> set[str]:
    """Parse a comma-separated prose list back into a set of values."""
    return {part.strip() for part in description_fragment.split(",") if part.strip()}


def test_advertised_entity_types_match_the_api():
    assert _advertised(ENTITY_TYPES) == set(get_args(EntityType))


def test_advertised_entity_statuses_match_the_api():
    assert _advertised(ENTITY_STATUSES) == set(get_args(EntityStatus))


def test_advertised_relationship_types_match_the_api():
    assert _advertised(RELATIONSHIP_TYPES) == set(get_args(RelationshipType))


def test_no_vocabulary_is_advertised_twice():
    """A duplicate would make the two sets compare equal while the prose a
    model actually reads says the same value twice."""
    for name, fragment in (
        ("ENTITY_TYPES", ENTITY_TYPES),
        ("ENTITY_STATUSES", ENTITY_STATUSES),
        ("RELATIONSHIP_TYPES", RELATIONSHIP_TYPES),
    ):
        values = [part.strip() for part in fragment.split(",") if part.strip()]
        assert len(values) == len(set(values)), f"{name} repeats a value: {values}"


# --- the rendered descriptions, not just the constants -----------------


def _descriptions() -> dict[str, str]:
    """The tool descriptions as the SDK actually reports them."""
    import anyio

    from api.config import Settings
    from q_core_mcp.client import QCoreClient
    from q_core_mcp.server import build_server

    server = build_server(QCoreClient(Settings(_env_file=None, api_token="t")))
    return {tool.name: tool.description for tool in anyio.run(server.list_tools)}


def test_entity_tool_descriptions_name_every_entity_type():
    """The constants above are interpolated into descriptions that ALSO
    spell the vocabulary out in English ("Create a person, property,
    vehicle, pet or account."). Fixing only the constant leaves the
    sentence contradicting the enum list two lines below it, and the
    sentence is the part a model reads first.

    Found exactly that way: the first pass of this change updated
    ENTITY_TYPES, every constant-level test passed, and `create_entity`
    still said "Create a person, property, vehicle, pet or account."
    """
    descriptions = _descriptions()

    for tool_name in ("get_entity", "list_entities", "create_entity"):
        description = descriptions[tool_name]
        missing = [
            value for value in get_args(EntityType) if value not in description
        ]
        assert not missing, f"{tool_name}'s description never mentions {missing}"


def test_advertised_due_sources_match_the_api():
    """Same shape as the three above. `/due` rejects an unknown source
    with a 422, so a source advertised here and not accepted there sends
    the model into an error it cannot reason its way out of."""
    from api.due import SOURCES
    from q_core_mcp.tools.due import DUE_SOURCES

    assert _advertised(DUE_SOURCES) == set(SOURCES)


# --- attributes: the 422 names what IS permitted (ticket T-67) --------------
#
# `attributes` is `extra="forbid"` per entity type, and the rejection used
# to name only the key it refused: a model learned `colour` was wrong
# without learning the field is `model`. Every enum error here lists its
# legal values; this was the one rejection that did not.
#
# Set equality, both directions, against the attribute models themselves.
# A key the API accepts but the message omits is one a model cannot
# discover; a key the message offers but the API rejects sends it into a
# 422 it cannot reason its way out of -- which is the direction an
# "includes everything" assertion misses.

import pytest
from fastapi.exceptions import RequestValidationError

from api.models import ATTRIBUTE_MODELS, validate_entity_attributes


def _permitted_from_message(entity_type: str) -> set[str]:
    """Parse the permitted list back out of the error a caller receives.

    Read from the message rather than from the helper's return value: the
    message is the artifact a model acts on, and a formatting change that
    drops half the list would leave any internal assertion green.
    """
    with pytest.raises(RequestValidationError) as excinfo:
        validate_entity_attributes(entity_type, {"definitely_not_a_field": "x"})
    message = excinfo.value.errors()[0]["msg"]
    listed = message.split(f"Permitted attributes for {entity_type}:")[1]
    return {part.strip(" .") for part in listed.split(",") if part.strip(" .")}


@pytest.mark.parametrize("entity_type", sorted(ATTRIBUTE_MODELS))
def test_the_422_lists_exactly_the_permitted_attributes(entity_type):
    assert _permitted_from_message(entity_type) == set(
        ATTRIBUTE_MODELS[entity_type].model_fields
    )


def test_the_permitted_list_is_not_matching_by_accident():
    """Guards the guard, as the subtype pin does.

    If the parse returned an empty set and a model happened to have no
    fields, set equality would pass over nothing.
    """
    permitted = _permitted_from_message("vehicle")
    assert len(permitted) >= 5
    assert "make" in permitted and "model" in permitted


def test_the_rejected_key_is_named_too():
    """Both halves. Listing what is allowed without saying what was
    refused makes the caller diff two lists to find their own typo."""
    with pytest.raises(RequestValidationError) as excinfo:
        validate_entity_attributes("vehicle", {"colour": "red"})
    message = excinfo.value.errors()[0]["msg"]
    assert "'colour' is not an attribute of a vehicle" in message


def test_an_unknown_keys_value_is_not_echoed_back():
    """A redaction hole, not a tidy-up.

    `_redact_sensitive_inputs` masks by KEY NAME against a set of names
    known to be sensitive, so an UNRECOGNISED key is by construction not
    on that list, and the error OBJECT held the value.

    IT NEVER REACHED A RESPONSE. `api/main.py`'s `_error_detail` projects
    only `field` and `message`, dropping `input` for every error type --
    measured end-to-end after this test was first written with the
    opposite claim in it (D119). This asserts the INNER layer, which is
    defence in depth; `api/tests/test_error_detail_drops_input.py` pins
    the outer one that carries the guarantee.

    Worth keeping both: a deny-list of field names cannot cover a field
    the caller invented, so the inner layer should not hold the value
    even though something downstream currently removes it.
    """
    with pytest.raises(RequestValidationError) as excinfo:
        validate_entity_attributes("person", {"ssn": "123-45-6789"})
    error = excinfo.value.errors()[0]

    assert "123-45-6789" not in str(error), error
    assert error["input"] == "[omitted]"
    # The key name still appears: that is what tells the caller what to fix,
    # and a key name they chose is not the secret.
    assert "'ssn'" in error["msg"]


def test_a_known_sensitive_field_is_still_redacted():
    """The existing protection must survive the new one -- they cover
    different cases and neither subsumes the other."""
    with pytest.raises(RequestValidationError) as excinfo:
        validate_entity_attributes("person", {"id_last4": "123456789"})
    error = excinfo.value.errors()[0]

    assert error["input"] == "[redacted]"
    assert "123456789" not in str(error)


def test_the_entity_write_tools_point_at_the_attribute_error(test_settings, tools_by_name):
    """The description must send a model to the error rather than let it
    retry blind.

    A tool that says "attributes holds type-specific fields" and nothing
    else invites a second guess after a 422. The error already carries
    the whole list; the description's job is to say so.
    """
    import httpx

    from q_core_mcp.client import QCoreClient
    from q_core_mcp.server import build_server

    client = QCoreClient(
        test_settings,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    tools = tools_by_name(build_server(client))
    for name in ("create_entity", "update_entity"):
        description = tools[name].description or ""
        assert "unknown" in description.lower(), name
        assert "lists every attribute" in description or "does accept" in description, name
