import json

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from q_core_mcp.client import ApiError, QCoreClient
from q_core_mcp.server import build_server


def _server(test_settings, handler):
    client = QCoreClient(test_settings, transport=httpx.MockTransport(handler))
    return build_server(client)


def _real_server(test_settings, app):
    client = QCoreClient(test_settings, transport=httpx.ASGITransport(app=app))
    return build_server(client)


def test_create_entity_posts_the_body(test_settings, call_tool):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={"id": "new"})

    call_tool(
        _server(test_settings, handler),
        "create_entity",
        {"entity_type": "vehicle", "name": "Corolla", "attributes": {"make": "Toyota"}},
    )

    assert seen["method"] == "POST"
    assert seen["path"] == "/entities"
    assert seen["body"] == {
        "type": "vehicle",
        "name": "Corolla",
        "status": "active",
        "attributes": {"make": "Toyota"},
    }


def test_update_entity_sends_only_the_supplied_fields(test_settings, call_tool):
    # Sending every field as null would not error — it would silently
    # change nothing (see the round-trip test below). Omitting absent
    # fields is what keeps an update from being a confidently-reported
    # no-op.
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={"id": "abc"})

    call_tool(
        _server(test_settings, handler),
        "update_entity",
        {"entity_id": "abc", "status": "sold"},
    )

    assert seen["method"] == "PATCH"
    assert seen["body"] == {"status": "sold"}


def test_create_relationship_posts_to_the_nested_path(test_settings, call_tool):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={"id": "rel"})

    call_tool(
        _server(test_settings, handler),
        "create_relationship",
        {"entity_id": "owner", "to_entity_id": "car", "relationship_type": "owns"},
    )

    assert seen["path"] == "/entities/owner/relationships"
    assert seen["body"]["to_entity_id"] == "car"
    assert seen["body"]["relationship_type"] == "owns"
    # Dates omitted rather than sent as null: the API types them as
    # date | None but an explicit null on a PATCH-shaped body is a
    # different statement from "not supplied".
    assert "start_date" not in seen["body"]


def test_write_tools_are_not_marked_read_only(test_settings, tools_by_name):
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))

    for name in ("create_entity", "update_entity", "create_relationship"):
        annotations = tools[name].annotations
        hint = annotations.read_only_hint if annotations else None
        assert hint is not True, name


def test_descriptions_document_the_two_surprising_api_behaviours(
    test_settings, tools_by_name
):
    # Neither behaviour produces an error, so a model not told about them
    # reports success for something that did not happen. These assertions
    # exist so the warnings cannot quietly rot out of the descriptions.
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))

    create_rel = tools["create_relationship"].description.lower()
    assert "existing" in create_rel

    update = tools["update_entity"].description.lower()
    assert "replace" in update

    for tool in tools.values():
        for placeholder in ("{types}", "{statuses}", "{relationship_types}"):
            assert placeholder not in tool.description, (tool.name, placeholder)


def test_round_trip_create_then_read(test_settings, call_tool):
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        created = call_tool(
            server,
            "create_entity",
            {
                "entity_type": "vehicle",
                "name": "Corolla",
                "attributes": {"make": "Toyota", "year": 2019},
            },
        )
        fetched = call_tool(server, "get_entity", {"entity_id": created["id"]})
    finally:
        app.dependency_overrides.clear()

    assert fetched["name"] == "Corolla"
    assert fetched["attributes"] == {"make": "Toyota", "year": 2019}


def test_round_trip_update_replaces_attributes_wholesale(test_settings, call_tool):
    # Pins the behaviour the description warns about, against the real API
    # rather than the prose. If the API ever starts merging instead, this
    # fails and the description needs changing — which is the point.
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        created = call_tool(
            server,
            "create_entity",
            {
                "entity_type": "vehicle",
                "name": "Corolla",
                "attributes": {"make": "Toyota", "year": 2019},
            },
        )
        updated = call_tool(
            server,
            "update_entity",
            {"entity_id": created["id"], "attributes": {"make": "Toyota"}},
        )
    finally:
        app.dependency_overrides.clear()

    assert updated["attributes"] == {"make": "Toyota"}
    assert "year" not in updated["attributes"]


def test_round_trip_update_without_attributes_leaves_them_alone(
    test_settings, call_tool
):
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        created = call_tool(
            server,
            "create_entity",
            {
                "entity_type": "vehicle",
                "name": "Corolla",
                "attributes": {"make": "Toyota", "year": 2019},
            },
        )
        updated = call_tool(
            server, "update_entity", {"entity_id": created["id"], "status": "sold"}
        )
    finally:
        app.dependency_overrides.clear()

    assert updated["status"] == "sold"
    assert updated["attributes"] == {"make": "Toyota", "year": 2019}


def test_round_trip_relationship_dedupe_returns_the_existing_row(
    test_settings, call_tool
):
    # The other behaviour the description warns about: a second identical
    # create returns the first row rather than making a new one.
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        owner = call_tool(
            server, "create_entity", {"entity_type": "person", "name": "Alex"}
        )
        car = call_tool(
            server, "create_entity", {"entity_type": "vehicle", "name": "Corolla"}
        )
        first = call_tool(
            server,
            "create_relationship",
            {
                "entity_id": owner["id"],
                "to_entity_id": car["id"],
                "relationship_type": "owns",
            },
        )
        second = call_tool(
            server,
            "create_relationship",
            {
                "entity_id": owner["id"],
                "to_entity_id": car["id"],
                "relationship_type": "owns",
            },
        )
        links = call_tool(server, "list_relationships", {"entity_id": owner["id"]})
    finally:
        app.dependency_overrides.clear()

    assert second["id"] == first["id"]
    assert links["total"] == 1


def test_round_trip_relationship_to_unknown_entity_reports_it(
    test_settings, call_tool
):
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        owner = call_tool(
            server, "create_entity", {"entity_type": "person", "name": "Alex"}
        )
        with pytest.raises(ToolError) as exc_info:
            call_tool(
                server,
                "create_relationship",
                {
                    "entity_id": owner["id"],
                    "to_entity_id": "no-such-entity",
                    "relationship_type": "owns",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert "no-such-entity" in str(exc_info.value)


def test_round_trip_invalid_attribute_reports_the_field(test_settings, call_tool):
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        with pytest.raises(ToolError) as exc_info:
            call_tool(
                server,
                "create_entity",
                {
                    "entity_type": "property",
                    "name": "Lake House",
                    "attributes": {"purchase_date": "not-a-date"},
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert "attributes.purchase_date:" in str(exc_info.value)


def test_an_explicit_null_body_now_clears_or_is_refused_by_name(
    test_settings, call_tool
):
    """THIS TRIPWIRE HAS NOW FIRED THREE TIMES, each time as an instruction.

    v1 asserted an all-null PATCH returned 200 having changed nothing,
    because the route read `body.x if body.x is not None` and a null took
    the same branch as an absent field. Its docstring named the change
    that would invalidate it.

    v2: ticket T-56 added the at-least-one guard, it went red exactly as
    written, and it became "a body that names no field is a 422".

    v3 is this one. ticket T-17 makes an explicit null MEAN clear, so a body
    of three nulls is no longer empty -- it names three fields and asks to
    unset them. Two of the three are NOT NULL columns, so it is refused BY
    NAME rather than as an empty body. The message changed from "at least
    one" to "cannot clear", which is what turned this red again.

    The tripwire is the point: each failure arrived as an instruction
    rather than a puzzle, because the test said in advance what would
    invalidate it and what to do.
    """
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        client = QCoreClient(test_settings, transport=httpx.ASGITransport(app=app))
        created = call_tool(
            server,
            "create_entity",
            {"entity_type": "vehicle", "name": "Corolla", "attributes": {"make": "Toyota"}},
        )

        import anyio

        async def patch(json_body):
            return await client.request(
                "PATCH", f"/entities/{created['id']}", json=json_body
            )

        # Three explicit nulls: names three fields, two of them required.
        with pytest.raises(ApiError) as excinfo:
            anyio.run(patch, {"name": None, "status": None, "attributes": None})
        message = str(excinfo.value)
        assert "cannot clear" in message
        assert "name" in message and "status" in message

        # Naming NO field is still the empty-body 422 -- v2's contract,
        # which v3 must not quietly drop.
        with pytest.raises(ApiError) as excinfo:
            anyio.run(patch, {})
        assert "at least one" in str(excinfo.value)

        # And the clearable one on its own succeeds.
        result = anyio.run(patch, {"attributes": None})
        assert result["attributes"] == {}
    finally:
        app.dependency_overrides.clear()


def test_the_tool_clears_via_the_clear_argument_not_a_null(test_settings, call_tool):
    """The tool signature cannot express "explicitly null".

    `update_entity(id)` and `update_entity(id, name=None)` are the same
    Python call, so a null argument carries no intent to forward. Clearing
    therefore gets its own argument, which is turned into an explicit null
    in the JSON body -- the layer that CAN express it.
    """
    seen, handler = _recording_patch()
    call_tool(
        _server(test_settings, handler),
        "update_entity",
        {"entity_id": "abc", "clear": ["attributes"]},
    )
    assert seen["body"] == {"attributes": None}


def test_a_null_argument_still_sends_nothing(test_settings, call_tool):
    """The companion. If a null argument DID reach the body, every caller
    passing name=None to mean "leave alone" would silently clear it."""
    seen, handler = _recording_patch()
    call_tool(
        _server(test_settings, handler),
        "update_entity",
        {"entity_id": "abc", "name": "Accord", "status": None},
    )
    assert seen["body"] == {"name": "Accord"}, "a null argument reached the body"


def test_setting_and_clearing_the_same_field_is_refused(test_settings, call_tool):
    """Contradictory instructions: guessing which was meant would be a
    silent wrong write, and both guesses are defensible."""
    seen, handler = _recording_patch()
    with pytest.raises(ToolError) as excinfo:
        call_tool(
            _server(test_settings, handler),
            "update_entity",
            {"entity_id": "abc", "attributes": {"make": "Toyota"},
             "clear": ["attributes"]},
        )
    assert "both to set and to clear" in str(excinfo.value)


# --- immutable fields must be refused, not dropped (a todo) -----------


def _recording_patch():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["body"] = json.loads(request.read()) if request.read() else None
        return httpx.Response(200, json={"id": "abc"})

    return seen, handler


@pytest.mark.parametrize(
    "field,value",
    [("entity_type", "vehicle"), ("type", "vehicle"), ("created_at", "2020-01-01")],
)
def test_update_entity_refuses_an_immutable_field(
    test_settings, call_tool, field, value
):
    """An immutable field must be refused, not silently dropped.

    The MCP SDK discards an argument the tool signature does not declare,
    *before* the tool body runs. So update_entity(type="vehicle") used to
    send an EMPTY PATCH and return 200 — reporting a successful update that
    changed nothing, which is worse than an error because the caller has no
    signal to correct. The API's own extra="forbid" never saw the field,
    because the tool never forwarded it.

    Both spellings are covered: `type` is what the API calls it and what a
    model reading the schema would try, `entity_type` is what create_entity
    calls it and so what a model that just created one would reach for.
    """
    seen, handler = _recording_patch()

    with pytest.raises(ToolError) as exc_info:
        call_tool(
            _server(test_settings, handler),
            "update_entity",
            {"entity_id": "abc", field: value},
        )

    message = str(exc_info.value)
    assert field in message or "type" in message
    # Echo what was attempted, so the correction is obvious.
    assert value in message
    # Nothing was sent: no silent empty PATCH reported as success.
    assert "body" not in seen, seen.get("body")


def test_update_entity_refusal_names_what_is_patchable(test_settings, call_tool):
    """A refusal that only says no is a dead end. It has to say what the
    tool CAN change, so the caller's next move is obvious."""
    seen, handler = _recording_patch()

    with pytest.raises(ToolError) as exc_info:
        call_tool(
            _server(test_settings, handler),
            "update_entity",
            {"entity_id": "abc", "type": "vehicle"},
        )

    message = str(exc_info.value).lower()
    assert "name" in message and "status" in message and "attributes" in message


def test_update_entity_still_sends_the_patchable_fields(test_settings, call_tool):
    """The refusal must not have broken the normal path."""
    seen, handler = _recording_patch()
    call_tool(
        _server(test_settings, handler),
        "update_entity",
        {"entity_id": "abc", "name": "Renamed", "status": "sold"},
    )
    assert seen["body"] == {"name": "Renamed", "status": "sold"}


def test_round_trip_update_entity_leaves_type_alone(test_settings, call_tool):
    """The behaviour half, against the real API: the entity's type is
    unchanged and the caller was told, rather than being told it worked."""
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        created = call_tool(
            server, "create_entity", {"entity_type": "pet", "name": "Rex"}
        )
        with pytest.raises(ToolError):
            call_tool(
                server,
                "update_entity",
                {"entity_id": created["id"], "type": "vehicle"},
            )
        fetched = call_tool(server, "get_entity", {"entity_id": created["id"]})
    finally:
        app.dependency_overrides.clear()

    assert fetched["type"] == "pet"


def test_update_entity_names_every_immutable_field_at_once(test_settings, call_tool):
    """One refusal listing all of them, not one per round trip.

    Raising on the first violation meant a caller sending two immutable
    fields learned about them one at a time, fixing and retrying for each.
    #52 established naming them together for update_transaction; this
    matches it, and the cost of collecting first is nothing.
    """
    seen, handler = _recording_patch()

    with pytest.raises(ToolError) as exc_info:
        call_tool(
            _server(test_settings, handler),
            "update_entity",
            {
                "entity_id": "abc",
                "type": "vehicle",
                "created_at": "2020-01-01",
                "name": "Renamed",
            },
        )

    message = str(exc_info.value)
    assert "type" in message and "created_at" in message
    # Still echoes what was attempted, for each.
    assert "vehicle" in message and "2020-01-01" in message
    # The patchable field the caller also sent is not silently applied.
    assert "body" not in seen, seen.get("body")


def test_update_entity_refusal_reads_naturally_for_a_single_field(
    test_settings, call_tool
):
    """Collecting must not make the common case read like a list of one."""
    seen, handler = _recording_patch()

    with pytest.raises(ToolError) as exc_info:
        call_tool(
            _server(test_settings, handler),
            "update_entity",
            {"entity_id": "abc", "type": "vehicle"},
        )

    message = str(exc_info.value)
    assert "fields" not in message.split("cannot change")[1][:40]


def test_create_relationship_description_states_retry_and_refusal(
    test_settings, tools_by_name
):
    """Both halves, because only stating one is worse than neither.

    "Re-sending is safe" alone invites a model to retry a rejected
    CHANGE forever. "Changes are refused" alone makes it avoid the
    idempotent retry that is the normal recovery from a dropped
    response. The pair is what makes the behaviour actionable.
    """
    tools = tools_by_name(
        _server(test_settings, lambda r: httpx.Response(200, json={}))
    )
    description = tools["create_relationship"].description.lower()

    assert "identical" in description
    assert "refused" in description or "fails" in description
    assert "no endpoint to amend" in description


def test_create_relationship_description_states_the_self_link_rule(
    test_settings, tools_by_name
):
    """A model that does not know will send the same id twice and read
    the 422 as a bug in its ids rather than in its intent."""
    tools = tools_by_name(
        _server(test_settings, lambda r: httpx.Response(200, json={}))
    )
    description = tools["create_relationship"].description.lower()

    assert "cannot link to itself" in description
    assert "every relationship_type" in description


def test_every_update_tool_offers_clear_and_explains_it(test_settings, tools_by_name):
    """Discovered from the server, not listed here.

    The API distinguishes absent from explicit-null; a Python signature
    cannot. So every `update_*` tool needs `clear` or its callers have no
    way to unset anything -- and a new update tool added without it would
    simply be missing the capability, silently, with nothing to notice.

    A tool whose model has nothing clearable should still fail this and be
    dealt with deliberately, rather than defaulting into the gap.
    """
    from q_core_mcp.clearing import CLEAR_NOTE

    tools = tools_by_name(_server(test_settings, _recording_patch()[1]))
    update_tools = sorted(n for n in tools if n.startswith("update_"))
    assert update_tools, "discovery found no update_* tools -- the walk broke"

    # A tool whose resource has NO nullable field has nothing to clear, and
    # a `clear` argument that can only be refused would teach a model a
    # verb that never works. Each exemption names why; the set is checked
    # against the registry so it cannot outlive its tool.
    nothing_to_clear = {
        "update_note": "body is the note's only field and is required",
    }
    assert set(nothing_to_clear) <= set(update_tools), nothing_to_clear
    update_tools = [n for n in update_tools if n not in nothing_to_clear]

    missing_param = [
        name for name in update_tools
        if "clear" not in tools[name].input_schema["properties"]
    ]
    assert not missing_param, (
        f"update_* tools with no `clear` parameter: {missing_param} -- their "
        f"callers cannot unset a nullable field at all."
    )

    # The note, not merely the parameter: an argument a model does not
    # know the semantics of is one it will use wrongly or not at all.
    unexplained = [
        name for name in update_tools
        if CLEAR_NOTE.split("—")[0].strip() not in (tools[name].description or "")
    ]
    assert not unexplained, (
        f"update_* tools taking `clear` without explaining it: {unexplained}"
    )
def test_update_relationship_sends_only_what_it_was_given(test_settings, call_tool):
    """null MEANS clear on this endpoint, so an argument the caller did
    not supply must not be sent as null — it would wipe the stored
    value. This is the one tool where omitted and null differ, so the
    usual "omit what you were not given" rule is load-bearing rather
    than tidy."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["method"] = request.method
        return httpx.Response(200, json={"id": "r1"})

    call_tool(
        _server(test_settings, handler),
        "update_relationship",
        {"relationship_id": "r1", "attributes": {"rent": 1200}},
    )

    assert seen["method"] == "PATCH"
    assert seen["body"] == {"attributes": {"rent": 1200}}


def test_update_relationship_clears_only_what_clear_names(test_settings, call_tool):
    """`clear` is how a caller asks for a null on purpose, rather than
    the tool inferring it from an absent argument."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "r1"})

    call_tool(
        _server(test_settings, handler),
        "update_relationship",
        {"relationship_id": "r1", "clear": ["end_date"]},
    )

    assert seen["body"] == {"end_date": None}


def test_update_relationship_description_states_what_is_specific_to_it(
    test_settings, tools_by_name
):
    """The two claims a caller cannot get from the shared clear note.

    This test used to assert the description said "null clears a field".
    It did say so, and it was WRONG -- measured in #126: the SDK cannot
    distinguish a null argument from an omitted one, so a model following
    that sentence would have sent nulls expecting them to clear and had
    them silently dropped. The sentence is gone and `clear` replaced it.

    What is left here is only what is specific to this tool, because
    test_every_update_tool_offers_clear_and_explains_it already pins the
    clear note across the whole surface, and a second copy of that
    assertion would be a second thing to update when the note changes.
    """
    tools = tools_by_name(
        _server(test_settings, lambda r: httpx.Response(200, json={}))
    )
    description = tools["update_relationship"].description.lower()

    # attributes overwrites rather than merges: the caller who does not
    # know this loses every attribute they left out.
    assert "replaces" in description
    # The identity fields, which is what makes this different from every
    # other update tool -- amending them is not an edit, it is a
    # different link.
    assert "relationship_type" in description
    assert "null clears a field" not in description, (
        "the description must not tell a model to clear with a null -- "
        "the SDK drops it and the field silently keeps its value"
    )


def test_update_relationship_refuses_a_value_and_a_clear_on_one_field(
    test_settings, call_tool
):
    """review-1 on #124: `clear` is applied after the assignments, so
    this used to send {"end_date": null} — the caller's value silently
    erased, answered 200.

    That is this tool's own failure mode one step along: a write that
    reports success and did something other than what was asked. There
    is no sensible precedence to pick either, since both readings
    discard half of a contradictory request.
    """
    sent: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "r1"})

    with pytest.raises(ToolError) as excinfo:
        call_tool(
            _server(test_settings, handler),
            "update_relationship",
            {
                "relationship_id": "r1",
                "end_date": "2026-01-01",
                "clear": ["end_date"],
            },
        )

    assert "end_date" in str(excinfo.value)
    assert "both to set and to clear" in str(excinfo.value)
    assert sent == {}, "the request must not be sent at all"


def test_update_relationship_allows_setting_one_field_and_clearing_another(
    test_settings, call_tool
):
    """The refusal is per field, not per call — clearing an end date
    while setting a start date is a coherent request and must still
    work."""
    sent: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "r1"})

    call_tool(
        _server(test_settings, handler),
        "update_relationship",
        {
            "relationship_id": "r1",
            "start_date": "2026-01-01",
            "clear": ["end_date"],
        },
    )

    assert sent["body"] == {"start_date": "2026-01-01", "end_date": None}
