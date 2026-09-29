"""Tools over api/entities.py: entities and the links between them.

One tool per endpoint. Each tool's description is what the model reads to
choose between them, so it carries the enum values the API enforces — a
model that sends an illegal value otherwise discovers the constraint
through a 422.

Descriptions are passed to the decorator explicitly rather than left to
the docstring. The SDK captures a function's `__doc__` at decoration
time, so building a description by formatting `__doc__` afterwards has no
effect on what `list_tools()` reports — verified against the SDK.
"""

from api.entities import (
    LIST_ENTITIES_ORDER,
    RELATIONSHIP_DEDUP_CONTRACT,
    RELATIONSHIP_UPDATE_CONTRACT,
)
from q_core_mcp.paging import MAX_LIMIT, PAGING_NOTE, list_request
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from q_core_mcp.clearing import CLEAR_NOTE, apply_clear
from q_core_mcp.annotations import (
    ADDITIVE,
    ADDITIVE_IDEMPOTENT,
    DESTRUCTIVE,
    READ_ONLY,
)
from q_core_mcp.client import QCoreClient

# These restate api/models.py's EntityType / EntityStatus /
# RelationshipType literals as prose, because prose is what the model
# reads. Two lists describing one set, so they drift silently: both of the
# first two did, when Jyra Task 1 added `project` and `archived` and only
# the API side learned about them. q_core_mcp/tests/test_tools_vocabulary.py pins
# all three against api.models by set equality — add a value there and
# here, or the suite fails.
ENTITY_TYPES = "person, property, vehicle, pet, account, project"
ENTITY_STATUSES = (
    "active, inactive, sold, totaled, deceased, closed, archived"
)
RELATIONSHIP_TYPES = (
    "owns, resides_at, insures, finances, maintains, leases, spouse_of, "
    "parent_of"
)



def register_entity_tools(server: MCPServer, client: QCoreClient) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            f"Get one entity ({ENTITY_TYPES}) by id. Returns its type, "
            "name, status, attributes and timestamps."
        ),
    )
    async def get_entity(entity_id: str) -> dict:
        return await client.request("GET", f"/entities/{entity_id}")

    @server.tool(
        annotations=READ_ONLY,
        description=(
            f"{PAGING_NOTE} "
            f"List the owner's entities, sorted {LIST_ENTITIES_ORDER}. "
            f"entity_type filters to one of: {ENTITY_TYPES}. status filters "
            f"to one of: {ENTITY_STATUSES}. limit is 1-{MAX_LIMIT}. "
            'Returns {"items", "total", "limit", "offset"}.'
        ),
    )
    async def list_entities(
        entity_type: str | None = None,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        all: bool = False,
        allow_truncated: bool = False,
    ) -> dict:
        params: dict = {}
        # Omitted rather than sent as null: both are Literal-typed on the
        # API side, so an explicit empty value is a 422, not a no-op.
        if entity_type is not None:
            params["type"] = entity_type
        if status is not None:
            params["status"] = status
        return await list_request(
            client, "/entities", params=params,
            limit=limit, offset=offset, fetch_all=all,
            allow_truncated=allow_truncated,
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            f"{PAGING_NOTE} "
            "List every relationship involving this entity, in either "
            'direction. Returns {"items", "total", "limit", "offset"}; each '
            "item has its own id, from_entity_id, to_entity_id and "
            "relationship_type. Relationship types are: "
            f"{RELATIONSHIP_TYPES}."
        ),
    )
    async def list_relationships(
        entity_id: str, limit: int | None = None, offset: int = 0, all: bool = False,
        allow_truncated: bool = False
    ) -> dict:
        return await list_request(
            client,
            f"/entities/{entity_id}/relationships",
            limit=limit, offset=offset, fetch_all=all,
            allow_truncated=allow_truncated,
        )

    @server.tool(
        annotations=ADDITIVE,
        description=(
            "Create an entity. "
            f"entity_type must be one of: {ENTITY_TYPES}. status must be "
            f"one of: {ENTITY_STATUSES}, default active. attributes holds "
            "type-specific fields, e.g. a vehicle's make, model, year, vin. "
            "Attributes are checked against the entity_type and an unknown "
            "key is refused — the error names the key and lists every "
            "attribute that type does accept, so read it rather than "
            "guessing again. Projects optionally accept repository_path as "
            "projects/<lowercase-slug> (letters/digits and single separating "
            "hyphens); duplicate project assignments are refused with conflict. "
            "The id is generated server-side; never supply one."
        ),
    )
    async def create_entity(
        entity_type: str,
        name: str,
        status: str = "active",
        attributes: dict | None = None,
    ) -> dict:
        return await client.request(
            "POST",
            "/entities",
            json={
                "type": entity_type,
                "name": name,
                "status": status,
                "attributes": attributes or {},
            },
        )

    @server.tool(
        annotations=DESTRUCTIVE,
        description=(
            "Change an entity's name, status or attributes. Only the fields "
            "you pass are changed. An unknown attribute key is refused, and "
            "the error lists every attribute that entity_type accepts. "
            "Projects accept repository_path as projects/<lowercase-slug> "
            "(letters/digits and single separating hyphens); duplicate "
            "assignments, including archived projects, are refused with conflict. "
            "IMPORTANT: attributes REPLACES the whole "
            "attributes object rather than merging into it, so to add or "
            "change one attribute, call get_entity first and send back the "
            "full set with your edit applied — otherwise the others are "
            f"lost. status must be one of: {ENTITY_STATUSES}. "
            "An entity's type and its timestamps cannot be changed: create "
            f"the entity you meant instead. {CLEAR_NOTE}"
        ),
    )
    async def update_entity(
        entity_id: str,
        name: str | None = None,
        status: str | None = None,
        attributes: dict | None = None,
        clear: list[str] | None = None,
        entity_type: str | None = None,
        type: str | None = None,  # noqa: A002 - the API's own field name
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> dict:
        # The immutable fields are accepted ONLY to refuse them, and that is
        # not redundancy. The MCP SDK discards an argument the signature does
        # not declare, before the tool body runs — so update_entity(
        # type="vehicle") sent an empty PATCH and returned 200, reporting a
        # successful update that changed nothing. Worse than an error,
        # because the caller gets no signal to correct. The API's own
        # extra="forbid" never saw the field: the tool never forwarded it.
        #
        # Both spellings, deliberately: `type` is what the API calls it and
        # what a model reading the schema would try, `entity_type` is what
        # create_entity calls it and what a model that just created one
        # would reach for. Missing either leaves the silent no-op reachable.
        immutable = {
            "entity_type": entity_type,
            "type": type,
            "created_at": created_at,
            "updated_at": updated_at,
        }
        # Collected, not raised on the first: a caller sending two immutable
        # fields would otherwise learn about them one at a time, fixing and
        # retrying for each. Matches update_transaction, which names them
        # together, and collecting costs nothing.
        attempted = [
            f"{field!r} (you sent {value!r})"
            for field, value in immutable.items()
            if value is not None
        ]
        if attempted:
            raise ToolError(
                f"update_entity cannot change {', '.join(attempted)}. An "
                "entity's type and timestamps are fixed once it exists — "
                "create the entity you meant instead, with create_entity. "
                "This tool changes name, status and attributes."
            )
        # Only supplied fields are sent, and the reason has now changed
        # twice. It was: a null took the same branch as an absent field, so
        # sending one returned 200 having changed nothing. Then ticket T-56
        # made a body naming no field a 422. Now ticket T-17 makes an explicit
        # null MEAN clear at the API.
        #
        # Omitting is still right here, and the reason is no longer about
        # the API at all: THIS SIGNATURE cannot express the difference.
        # `update_entity(id)` and `update_entity(id, name=None)` are the
        # same call -- measured, both arrive as None -- so a null argument
        # carries no intent to forward. A sentinel default would
        # distinguish them, but it publishes `"default": "__unset__"` into
        # the tool schema and a caller sending that literal string would
        # have it read as "not supplied".
        #
        # So clearing gets its own argument, which the JSON body CAN
        # express even though the signature cannot.
        body: dict = {}
        if name is not None:
            body["name"] = name
        if status is not None:
            body["status"] = status
        if attributes is not None:
            body["attributes"] = attributes
        # Explicit nulls for the named fields; see q_core_mcp/clearing.py
        # for why clearing needs its own argument.
        apply_clear(body, clear, "update_entity")
        return await client.request("PATCH", f"/entities/{entity_id}", json=body)

    @server.tool(
        annotations=ADDITIVE,
        description=(
            "Link two entities, e.g. a person owns a vehicle. entity_id is "
            "the from side, to_entity_id the to side. relationship_type "
            f"must be one of: {RELATIONSHIP_TYPES}. Dates are YYYY-MM-DD "
            "and only meaningful for leases and resides_at.\n\n"
            f"In short: {RELATIONSHIP_DEDUP_CONTRACT}.\n\n"
            "RE-SENDING THE SAME LINK IS SAFE. An identical payload gets "
            "the existing relationship back with a 200 — nothing is "
            "duplicated, so a retry after a dropped response costs "
            "nothing.\n\n"
            "CHANGING ONE IS REFUSED, NOT APPLIED. If a matching "
            "relationship exists and you send DIFFERENT dates or "
            "attributes, the call fails naming the existing id and which "
            "fields differ. Creating a link again never edits it, and "
            "there is no endpoint to amend one yet, so do not retry with "
            "the same change — report it instead.\n\n"
            "AN ENTITY CANNOT LINK TO ITSELF. Passing the same id for both "
            "sides is rejected for every relationship_type. If that looks "
            "like what you want, you have the wrong id on one side."
        ),
    )
    async def create_relationship(
        entity_id: str,
        to_entity_id: str,
        relationship_type: str,
        start_date: str | None = None,
        end_date: str | None = None,
        attributes: dict | None = None,
    ) -> dict:
        body: dict = {
            "to_entity_id": to_entity_id,
            "relationship_type": relationship_type,
            "attributes": attributes or {},
        }
        if start_date is not None:
            body["start_date"] = start_date
        if end_date is not None:
            body["end_date"] = end_date
        return await client.request(
            "POST", f"/entities/{entity_id}/relationships", json=body
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Get one relationship by id: the two entities, the type, its "
            "dates and attributes. Use this to check what a relationship "
            "holds before changing it — list_relationships gives you the "
            "id, this gives you the row."
        ),
    )
    async def get_relationship(relationship_id: str) -> dict:
        return await client.request("GET", f"/relationships/{relationship_id}")

    @server.tool(
        annotations=DESTRUCTIVE,
        description=(
            "Amend a relationship: "
            f"{RELATIONSHIP_UPDATE_CONTRACT}.\n\n"
            "This is the route to use when create_relationship refuses a "
            "change. Creating a link again never edits it, so a correction "
            "goes here.\n\n"
            f"{CLEAR_NOTE}\n\n"
            "A call that names no field at all is rejected rather than "
            "reported as a successful edit.\n\n"
            "attributes REPLACES what is stored rather than merging, so "
            "send the whole object — anything you leave out is gone.\n\n"
            "You cannot change which entities are linked or the "
            "relationship_type. Those identify the relationship; changing "
            "one would mean a different link, so delete this one and "
            "create that one."
        ),
    )
    async def update_relationship(
        relationship_id: str,
        start_date: str | None = None,
        end_date: str | None = None,
        attributes: dict | None = None,
        clear: list[str] | None = None,
    ) -> dict:
        body: dict = {}
        # Omitted rather than sent as null. The API reads a null on this
        # endpoint as "clear", so forwarding one for an argument the
        # caller never supplied would wipe the stored value -- and the
        # signature cannot tell the two apart. `clear` is how a caller
        # asks for it on purpose; see q_core_mcp/clearing.py.
        supplied = {
            "start_date": start_date,
            "end_date": end_date,
            "attributes": attributes,
        }
        for field, value in supplied.items():
            if value is not None:
                body[field] = value
        # Including the refusal of a field both set and cleared, which
        # this tool used to implement inline. Routed rather than kept:
        # two implementations of one convention is how the two drift,
        # and apply_clear's refusal is the same refusal (#126, D111).
        apply_clear(body, clear, "update_relationship")
        return await client.request(
            "PATCH", f"/relationships/{relationship_id}", json=body
        )

    @server.tool(
        annotations=DESTRUCTIVE,
        description=(
            "Permanently delete an entity. This cannot be undone. Fails if "
            "anything still references it — including a note linked to it — "
            "and the refusal names which tables block it and which notes, so "
            "the message says what to unlink. Hard delete is for mistakes, "
            "and an entity created by mistake has nothing attached to it. To "
            "stop tracking something you really do own, set status to "
            "archived, sold, closed, inactive or deceased instead — that "
            "keeps the row and every reference to it valid. A sold car's "
            "service costs and documents are usually still worth having."
        ),
    )
    async def delete_entity(entity_id: str) -> dict:
        return await client.request("DELETE", f"/entities/{entity_id}")

    @server.tool(
        annotations=DESTRUCTIVE,
        description=(
            "Permanently delete one relationship link between two entities. "
            "This cannot be undone. It deletes only the link, never the "
            "entities it connects. Takes the relationship's own id, which "
            "list_relationships returns as each item's id — not the id of "
            "either entity involved."
        ),
    )
    async def delete_relationship(relationship_id: str) -> dict:
        return await client.request("DELETE", f"/relationships/{relationship_id}")
