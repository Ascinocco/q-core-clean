"""Tools over api/notes.py: freeform notes, revised in place, linkable to anything.

The descriptions carry what the shapes cannot: that a note is edited by
replacing its whole body (read it first), that the title is its first line,
and that lists return previews while get_note returns the full text.
"""

from mcp.server import MCPServer

from api.notes import LIST_NOTES_ORDER, MAX_BODY_CHARS, TARGET_TABLES
from q_core_mcp.annotations import ADDITIVE, ADDITIVE_IDEMPOTENT, DESTRUCTIVE, READ_ONLY
from q_core_mcp.client import QCoreClient
from q_core_mcp.paging import MAX_LIMIT, PAGING_NOTE, list_request

_TARGET_TYPES = ", ".join(TARGET_TABLES)
_PRIVACY = (
    "Never put a SIN/SSN or a full account, card or routing number in a note: "
    "the API refuses text that looks like one."
)


def register_note_tools(server: MCPServer, client: QCoreClient) -> None:
    @server.tool(
        annotations=ADDITIVE,
        description=(
            "Create a freeform note: a plan, a draft, a checklist, anything worth keeping. "
            "body is Markdown-friendly text up to "
            f"{MAX_BODY_CHARS:,} characters; its first non-blank line (leading '#'s removed) "
            "is the note's title, so start with a heading. A note can stand alone or be "
            f"linked at creation via links=[{{target_type, target_id}}], target_type one of: "
            f"{_TARGET_TYPES}. Every target must exist (archived transactions and statements "
            "cannot be linked); one bad link refuses the whole create. A ticket may be "
            "named by its key (e.g. KA-12); the link stores its UUID. "
            + _PRIVACY
        ),
    )
    async def create_note(body: str, links: list[dict] | None = None) -> dict:
        payload: dict = {"body": body}
        if links:
            payload["links"] = links
        return await client.request("POST", "/notes", json=payload)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Get one note by id with its FULL body, derived title, timestamps and links "
            "(each link has its own id, used by unlink_note). Read a note with this before "
            "update_note, since an update replaces the whole body."
        ),
    )
    async def get_note(note_id: str) -> dict:
        return await client.request("GET", f"/notes/{note_id}")

    @server.tool(
        annotations=READ_ONLY,
        description=(
            f"{PAGING_NOTE} "
            f"List notes, {LIST_NOTES_ORDER}. Items carry title, a short preview, "
            "timestamps and link_count, NOT the full body; call get_note for that. "
            "Filter by link: target_id alone lists the notes linked to that thing, "
            f"target_type alone the notes linked to anything of that type ({_TARGET_TYPES}), "
            "both together one exact target; a ticket key (KA-12) works as a target_id. "
            "An id that names nothing is refused rather than "
            "answered with an empty page. q matches notes whose body contains that text "
            "(case-insensitive, literal characters, no wildcards). "
            f"limit is 1-{MAX_LIMIT}.\n\n"
            'Returns {"items", "total", "limit", "offset"}.'
        ),
    )
    async def list_notes(
        target_type: str | None = None,
        target_id: str | None = None,
        q: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        all: bool = False,
        allow_truncated: bool = False,
    ) -> dict:
        params: dict = {}
        if target_type is not None:
            params["target_type"] = target_type
        if target_id is not None:
            params["target_id"] = target_id
        if q is not None:
            params["q"] = q
        return await list_request(
            client, "/notes", params=params, limit=limit, offset=offset,
            fetch_all=all, allow_truncated=allow_truncated,
        )

    @server.tool(
        annotations=DESTRUCTIVE,
        description=(
            "Revise a note in place: body REPLACES the whole stored text and stamps "
            "updated_at. It is not an append. Get the note first, edit that text, and send "
            "all of it back; anything you leave out is gone. Links are unchanged. "
            + _PRIVACY
        ),
    )
    async def update_note(note_id: str, body: str) -> dict:
        return await client.request("PATCH", f"/notes/{note_id}", json={"body": body})

    @server.tool(
        annotations=DESTRUCTIVE,
        description=(
            "Delete a note and all of its links. This cannot be undone. The things it was "
            "linked to are untouched. To detach it from one thing only, use unlink_note."
        ),
    )
    async def delete_note(note_id: str) -> dict:
        return await client.request("DELETE", f"/notes/{note_id}")

    @server.tool(
        annotations=ADDITIVE_IDEMPOTENT,
        description=(
            f"Link a note to something: target_type one of {_TARGET_TYPES}, plus that "
            "thing's id (a ticket's key such as KA-12 also works; its UUID is what is "
            "stored). The target must exist (archived transactions and statements cannot "
            "be linked). Linking the same target twice returns the existing link. A note can "
            "have many links. Returns the note with link_id set to this link."
        ),
    )
    async def link_note(note_id: str, target_type: str, target_id: str) -> dict:
        return await client.request(
            "POST", f"/notes/{note_id}/links",
            json={"target_type": target_type, "target_id": target_id},
        )

    @server.tool(
        annotations=DESTRUCTIVE,
        description=(
            "Remove one link from a note by the link's id (from get_note's links). The note "
            "and the target both remain. Returns the note's remaining links."
        ),
    )
    async def unlink_note(note_id: str, link_id: str) -> dict:
        return await client.request("DELETE", f"/notes/{note_id}/links/{link_id}")
