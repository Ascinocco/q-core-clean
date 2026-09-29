"""The `list_due_items` tool over api/due.py.

One tool, because there is one endpoint. The description carries the
things a model cannot discover from the shape of the response: that
overdue items come back with the upcoming ones rather than needing a
second call with a past `from`, and that `days_left` is negative for
them. A model that assumed a plain range query would ask the wrong
question and get a confident empty answer.
"""

from api.due import DUE_ORDER
from q_core_mcp.paging import MAX_LIMIT, PAGING_NOTE, list_request
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from q_core_mcp.annotations import (
    ADDITIVE,
    ADDITIVE_IDEMPOTENT,
    DESTRUCTIVE,
    READ_ONLY,
)
from q_core_mcp.client import QCoreClient


# Restates api/due.py's SOURCES. q_core_mcp/tests/test_tools_vocabulary.py
# pins it against the API by set equality, for the same reason the entity
# vocabularies are pinned: two lists describing one set drift silently.
DUE_SOURCES = "attribute, relationship, reminder"


def register_due_tools(server: MCPServer, client: QCoreClient) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            f"{PAGING_NOTE} "
            "List everything coming due for the owner — renewals, "
            "expiries, lease and policy end dates, and reminders — in one "
            "call. This is the only 'what's due' query; there is no "
            "separate reminders endpoint.\n\n"
            "from and to are ISO dates bounding the window. Defaults: "
            "to = 30 days out; reminders include seven days of unfinished overdue occurrences when from is omitted. An explicit from keeps the exact reminder window.\n\n"
            "ANYTHING ALREADY OVERDUE IS INCLUDED regardless of from, for "
            "the attribute and relationship sources, with a NEGATIVE "
            "days_left. An expired registration or a lapsed policy is the "
            "most important thing this can tell you, and nothing moves "
            "those dates forward on its own, so they are reported rather "
            "than filtered out. Do not issue a second call with a past "
            "from to look for them. Reminders are the exception: they "
            "recur, so they are expanded only within this bounded window, using the effective snoozed date. Older reminders age out without being deleted.\n\n"
            f"source filters to one of: {DUE_SOURCES}. entity_id filters "
            f"to one entity. limit is 1-{MAX_LIMIT}.\n\n"
            'Returns {"items", "total"}. Each item has entity_id, '
            "entity_type, entity_name, source, field, title, due_date and "
            "days_left; reminders also have reminder_id and original occurrence_date. Items have related[] naming the other entity for a "
            f"relationship. Sorted {DUE_ORDER}, so the first item is the "
            "most urgent."
        ),
    )
    async def list_due_items(
        from_date: str | None = None,
        to_date: str | None = None,
        source: str | None = None,
        entity_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        all: bool = False,
        allow_truncated: bool = False,
    ) -> dict:
        params: dict = {}
        # `from` is a Python keyword, so the tool parameter cannot be
        # named for the query parameter it sets. The API accepts `from`
        # and `to`; these are translated here rather than aliased on the
        # API side, so the HTTP surface stays the obvious one.
        if from_date is not None:
            params["from"] = from_date
        if to_date is not None:
            params["to"] = to_date
        # Omitted rather than sent as null: the API rejects an unknown
        # source with a 422, and an explicit empty value is an unknown
        # source.
        if source is not None:
            params["source"] = source
        if entity_id is not None:
            params["entity_id"] = entity_id
        return await list_request(
            client, "/due", params=params,
            limit=limit, offset=offset, fetch_all=all,
            allow_truncated=allow_truncated,
        )
