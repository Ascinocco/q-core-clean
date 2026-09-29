"""Jyra tools: boards, tickets, the transition log, the claim, attachments.

The vocabulary constants below are pinned against `api.models` by
`test_ticket_vocabulary_matches_the_api`. That guard is not decorative:
`ENTITY_TYPES` in `entities.py` silently went stale the moment Jyra added
`project` to the API's entity vocabulary, because nothing compared the two.
A model reads these strings as the authoritative list of what it may send.
"""

from pathlib import Path

from api.config import REPO_ROOT
from api.documents import ATTACHMENT_CONTRACT, MAX_ATTACHMENT_BYTES
from api.redaction import UnscrubbedDigitsError, assert_no_account_numbers
from api.jyra import (
    DELETE_BOARD_CONTRACT,
    DELETE_TICKET_CONTRACT,
    KEY_PREFIX_CHANGE_CONTRACT,
    TICKET_HISTORY_ORDER,
    TICKET_REF_CONTRACT,
    TRANSITION_ATOMICITY_CONTRACT,
)
from api.models import TICKET_UPDATE_CONTRACT, TRANSITION_NOTE_CONTRACT
from q_core_mcp.clearing import CLEAR_NOTE, apply_clear
from q_core_mcp.paging import MAX_LIMIT, PAGING_NOTE, list_request
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from q_core_mcp.annotations import (
    ADDITIVE,
    ADDITIVE_IDEMPOTENT,
    DESTRUCTIVE,
    READ_ONLY,
)
from q_core_mcp.client import NO_CONTENT, QCoreClient

TICKET_TYPES = "solution, epic, story, bug, task"
TICKET_STATUSES = (
    "backlog, in_progress, agent_ready, agent_coding, review, blocked, done"
)

#: Appended to every tool that takes a ticket id. From api.jyra, beside
#: the resolver, so the sentence is the rule the API applies.
TICKET_REF = f"ticket_id: {TICKET_REF_CONTRACT}."

KEY_PREFIX_RULE = (
    "key_prefix is 2-6 characters, a letter then letters or digits (KA, "
    "KCX), unique across boards, never one that issued keys on a deleted "
    "board, and "
    "upper-cased for you"
)



def register_jyra_tools(server: MCPServer, client: QCoreClient) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Get a board and what is on it, grouped into columns. Returns "
            "all seven columns — backlog, in_progress, agent_ready, "
            "agent_coding, review, blocked, done — in that left-to-right "
            "workflow order, and present even when empty, so a board can "
            "be drawn without special-casing. Also returns the entity the "
            "board belongs to. This is the read to use when someone asks "
            "what is on a board or what is in flight.\n\n"
            "The board carries its key_prefix, and gives SUMMARIES, not "
            "whole tickets: each item has id, key (e.g. KA-12), type, "
            "title, status, position, parent_id and claimed_by. "
            "It does NOT include the description — that is not missing, it "
            "is in get_ticket. Call get_ticket on the one you actually "
            "need rather than assuming the board is incomplete.\n\n"
            'Each column is {"items", "total"}. When total is greater than '
            "the number of items, the column was capped and you are not "
            "seeing all of it."
        ),
    )
    async def get_board(board_id: str) -> dict:
        return await client.request("GET", f"/boards/{board_id}")

    @server.tool(
        annotations=READ_ONLY,
        description=(
            f"{PAGING_NOTE} "
            "List boards. entity_id narrows to the boards belonging to one "
            'entity; omit it to list every board. Returns {"items", '
            '"total", "limit", "offset"} — each item has the board id you '
            f"need for get_board. limit is 1-{MAX_LIMIT}."
        ),
    )
    async def list_boards(
        entity_id: str | None = None, limit: int | None = None, offset: int = 0, all: bool = False,
        allow_truncated: bool = False
    ) -> dict:
        params: dict = {}
        if entity_id is not None:
            params["entity_id"] = entity_id
        return await list_request(
            client, "/boards", params=params,
            limit=limit, offset=offset, fetch_all=all,
            allow_truncated=allow_truncated,
        )

    @server.tool(
        annotations=ADDITIVE,
        description=(
            "Create a board on an entity. entity_id is usually a project, "
            "but a board hangs off ANY entity — a maintenance backlog on a "
            "vehicle or a property works exactly the same way, because a "
            "project is itself an entity rather than a separate kind of "
            "thing. Create the project first with create_entity "
            '(entity_type="project") if it does not exist yet. The id is '
            "generated server-side; never supply one.\n\n"
            "Every ticket on the board gets a key made of the board's "
            "key_prefix and a number, e.g. KA-12. "
            f"{KEY_PREFIX_RULE}. Omit it to derive one from the entity's "
            "name: the initials of a multi-word name, otherwise its first "
            "three characters, numbered if already taken."
        ),
    )
    async def create_board(
        entity_id: str, title: str, key_prefix: str | None = None
    ) -> dict:
        body: dict = {"entity_id": entity_id, "title": title}
        if key_prefix is not None:
            body["key_prefix"] = key_prefix
        return await client.request("POST", "/boards", json=body)

    @server.tool(
        annotations=DESTRUCTIVE,
        description=(
            "Rename a board or change its ticket-key prefix. Its entity and "
            "its id are fixed, and its columns are the workflow's, not the "
            f"board's. {KEY_PREFIX_RULE}; {KEY_PREFIX_CHANGE_CONTRACT}, "
            "because the prefix is part of every ticket's key.\n\n"
            f"{CLEAR_NOTE}\n\n"
            "Nothing on a board is clearable, so naming any field in clear "
            "is refused rather than quietly accepted: a board with no "
            "title could not be told apart from the others."
        ),
    )
    async def update_board(
        board_id: str,
        title: str | None = None,
        key_prefix: str | None = None,
        clear: list[str] | None = None,
    ) -> dict:
        body: dict = {}
        if title is not None:
            body["title"] = title
        if key_prefix is not None:
            body["key_prefix"] = key_prefix
        # Forwarded rather than refused here, so the API's own
        # _refuse_unclearable answers and names what IS clearable. One
        # source for that list; see q_core_mcp/clearing.py.
        apply_clear(body, clear, "update_board")
        return await client.request("PATCH", f"/boards/{board_id}", json=body)

    @server.tool(
        annotations=DESTRUCTIVE,
        description=(
            "Delete a board permanently. This cannot be undone. "
            f"{DELETE_BOARD_CONTRACT}, so an accidental delete cannot take "
            "work with it — empty the board first if you really mean it, "
            "which is a decision about each ticket rather than one about "
            "the board.\n\n"
            "It deletes only the board, never the entity it hangs off. To "
            "stop using a board that has real history on it, leave it "
            "alone: a board nobody looks at costs nothing, and its "
            "tickets are the record of what happened."
        ),
    )
    async def delete_board(board_id: str) -> dict:
        return await client.request("DELETE", f"/boards/{board_id}")

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Get one ticket by id or key, with everything you need to work "
            "it in one call: its key, board, type, title, description, status, "
            "position and claim fields, plus its attachments (id, filename "
            "and size -- read one with read_attachment, which fetches it by "
            "id; there is no path to open) and refs to "
            "its child tickets. parent_id is the parent. "
            "Child refs carry id, key, type, title and status — call get_ticket "
            "on one to see its description. This is also where a "
            "description lives that get_board does not show.\n\n"
            "attachment_count is the true total; if it exceeds the length "
            "of attachments, the list was capped. "
            "artifacts lists linked Canvas docs (artifact_count is the true total).\n\n"
            f"{TICKET_REF}"
        ),
    )
    async def get_ticket(ticket_id: str) -> dict:
        return await client.request("GET", f"/tickets/{ticket_id}")

    @server.tool(
        annotations=READ_ONLY,
        description=(
            f"{PAGING_NOTE} "
            "List tickets, newest filters first. board_id, status, "
            "ticket_type and parent_id each narrow the result; omit any you "
            f"do not want. status is one of: {TICKET_STATUSES}. ticket_type "
            f"is one of: {TICKET_TYPES}. "
            "claimed_before (YYYY-MM-DD) is the stale-claim recovery query: "
            "combined with status='agent_coding' it finds tickets stuck "
            "because the agent that claimed them died, which are otherwise "
            "invisible. Recovering one is an ordinary transition back to "
            "agent_ready, so the abandonment stays in the ticket's history. "
            "Every item carries its key (e.g. KA-12). parent_id may be the "
            "parent's UUID or its key. "
            f'limit is 1-{MAX_LIMIT}. Returns {{"items", "total", '
            '"limit", "offset"}.'
        ),
    )
    async def list_tickets(
        board_id: str | None = None,
        status: str | None = None,
        ticket_type: str | None = None,
        parent_id: str | None = None,
        claimed_before: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        all: bool = False,
        allow_truncated: bool = False,
    ) -> dict:
        params: dict = {}
        # Omitted rather than sent empty. These are compared literally in
        # SQL, so an empty string filters to nothing rather than being
        # ignored — a silently wrong answer rather than an error.
        for key, value in (
            ("board_id", board_id),
            ("status", status),
            ("type", ticket_type),
            ("parent_id", parent_id),
            ("claimed_before", claimed_before),
        ):
            if value is not None:
                params[key] = value
        return await list_request(
            client, "/tickets", params=params,
            limit=limit, offset=offset, fetch_all=all,
            allow_truncated=allow_truncated,
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            f"{PAGING_NOTE} "
            f"Get a ticket's full status history, {TICKET_HISTORY_ORDER}. Every status "
            "change is here, including the ticket's creation and any agent "
            "claim, because the API writes the history row in the same "
            "transaction as the change — so the history cannot have gaps "
            "and is safe to reason from. Each entry has from_status, "
            "to_status, actor, note, created_at and the ticket's "
            "ticket_key; the note explains why "
            'the move happened. Returns {"items", "total", "limit", '
            f'"offset"}}. {TICKET_REF}'
        ),
    )
    async def get_ticket_history(
        ticket_id: str, limit: int | None = None, offset: int = 0, all: bool = False,
        allow_truncated: bool = False
    ) -> dict:
        return await list_request(
            client,
            f"/tickets/{ticket_id}/transitions",
            limit=limit, offset=offset, fetch_all=all,
            allow_truncated=allow_truncated,
        )

    @server.tool(
        annotations=ADDITIVE,
        description=(
            "Move a ticket to a new status. This is the ONLY way status "
            f"changes — update_ticket cannot do it. to_status is one of: "
            f"{TICKET_STATUSES}, and which are legal depends on the "
            "ticket's type: solutions and epics are planning containers and "
            "never enter agent_ready, agent_coding or review. "
            f"{TRANSITION_NOTE_CONTRACT}: an empty "
            "or whitespace-only note is rejected, not accepted quietly. It "
            "is the point rather than a formality: the API writes it into "
            f"the ticket's history -- {TRANSITION_ATOMICITY_CONTRACT} -- so "
            "the history cannot have gaps, and a future reader has only "
            "your note to explain why this happened. Write why, not what. "
            "actor is who is moving it — 'owner' for a human, or the agent's "
            "session identifier. It cannot be blank either: an unattributed "
            "history row is barely better than a missing one. "
            "Use blocked, not agent_ready, when an agent cannot finish: "
            "blocked takes the ticket out of claim_ticket's reach, whereas "
            "a ticket returned to agent_ready is picked up again and fails "
            "the same way forever. Optional expected_transition_id guards against "
            "concurrent changes: supply the latest history UUID; stale values return conflict. "
            f"{TICKET_REF}"
        ),
    )
    async def transition_ticket(
        ticket_id: str, to_status: str, actor: str, note: str,
        expected_transition_id: str | None = None,
    ) -> dict:
        body = {"to_status": to_status, "actor": actor, "note": note}
        if expected_transition_id is not None:
            body["expected_transition_id"] = expected_transition_id
        return await client.request(
            "POST",
            f"/tickets/{ticket_id}/transition",
            json=body,
        )

    @server.tool(
        annotations=ADDITIVE,
        description=(
            "Take the next ticket waiting for an agent. This is the agent "
            "loop's entry point, not a browsing tool: it MUTATES — the "
            "ticket it returns is now yours, moved to agent_coding and "
            "stamped with your actor id, and no other caller can take it. "
            "Only ever returns agent_ready tickets, in board order "
            "(position, then age), because a loop drains the queue with no "
            "human to intervene between pickups. "
            'Returns {"claimed": true, "ticket": {...}} when it took one, '
            'and {"claimed": false, "reason": ...} when the queue is empty '
            "— an empty queue is SUCCESS and means there is nothing to do, "
            "not a failure to retry or report. "
            "board_id limits it to one board; omit it to take from any. "
            "When you finish, transition_ticket to review; when you cannot "
            "finish, transition_ticket to blocked with a note saying what "
            "decision you need."
        ),
    )
    async def claim_ticket(actor: str, board_id: str | None = None) -> dict:
        body: dict = {"actor": actor}
        if board_id is not None:
            body["board_id"] = board_id
        result = await client.request("POST", "/tickets/claim", json=body)
        # The API answers 204 when nothing is claimable, which for a polling
        # loop is most calls. Wrapped rather than passed through: a tool
        # returning null on its commonest path invites a model to read
        # idleness as failure and retry, which is the one behaviour a
        # polling loop must not have.
        if result is NO_CONTENT:
            return {
                "claimed": False,
                "reason": "no agent_ready tickets are waiting",
            }
        return {"claimed": True, "ticket": result}

    @server.tool(
        annotations=ADDITIVE,
        description=(
            "Create a ticket on a board. It always starts in backlog — use "
            "transition_ticket to move it, and note that creation writes "
            "the first entry in the ticket's history by itself. "
            f"ticket_type is one of: {TICKET_TYPES}. solution and epic are "
            "planning containers; story, bug and task are the work an agent "
            "can pick up. "
            "description is markdown and is what an agent reads to do the "
            "work — spell out what done looks like. "
            "parent_id nests this ticket under another on the SAME board, "
            "and the parent must strictly outrank the child: solution "
            "outranks epic, which outranks story/bug/task, which do not "
            "outrank each other. A task can hang directly off a solution "
            "when that is the honest structure. parent_id may be the "
            "parent's UUID or its key (e.g. KA-12, any case). "
            "actor is who is creating it. The id is generated server-side; "
            "never supply one. The new ticket's key is the board's "
            "key_prefix and the next number on that board, assigned with "
            "the ticket; a deleted ticket's number is never reused, so "
            "numbers can have gaps."
        ),
    )
    async def create_ticket(
        board_id: str,
        ticket_type: str,
        title: str,
        actor: str,
        description: str | None = None,
        parent_id: str | None = None,
    ) -> dict:
        body: dict = {
            "board_id": board_id,
            "type": ticket_type,
            "title": title,
            "actor": actor,
        }
        # Sent only when given. description is nullable so a null would be
        # harmless, but parent_id null means "no parent" while an empty
        # string would be looked up as a real id and rejected.
        if description is not None:
            body["description"] = description
        if parent_id is not None:
            body["parent_id"] = parent_id
        return await client.request("POST", "/tickets", json=body)

    @server.tool(
        annotations=DESTRUCTIVE,
        description=(
            "Change a ticket's title, description, parent or position. Only "
            "the fields you pass are changed. "
            f"It {TICKET_UPDATE_CONTRACT}: there is no status parameter, and "
            "sending one is rejected rather than quietly ignored. Status "
            "moves only through transition_ticket, which requires a note, "
            "so that every move is explained in the ticket's history. "
            "position orders a ticket within its column; lower comes first, "
            "and 0 bumps it to the top — which matters because "
            "claim_ticket hands out tickets in that order. "
            "parent_id must be a ticket on the same board that outranks "
            f"this one, named by UUID or key. {CLEAR_NOTE} {TICKET_REF}"
        ),
    )
    async def update_ticket(
        ticket_id: str,
        title: str | None = None,
        description: str | None = None,
        parent_id: str | None = None,
        position: int | None = None,
        clear: list[str] | None = None,
        status: str | None = None,
    ) -> dict:
        # `status` is accepted only to reject it. Without the parameter the
        # SDK drops an unknown argument before the tool runs, so
        # update_ticket(status="done") sent an empty PATCH and returned 200
        # — the model is told its update succeeded while nothing changed,
        # which is the exact failure update_entity's comment warns about and
        # worse than an error. Taking the argument lets the refusal say
        # where status actually moves.
        if status is not None:
            raise ToolError(
                "update_ticket cannot change status. Use transition_ticket "
                f"with to_status={status!r}, an actor, and a note saying why "
                "— the note is written into the ticket's history in the same "
                "transaction as the move."
            )
        # Only supplied fields are sent. This used to be the ONLY thing
        # stopping a silent no-op: the route reads
        # `body.x if body.x is not None`, so an explicit null took the same
        # branch as an absent field and returned 200 having changed nothing.
        #
        # Since ticket T-56 `TicketUpdate` refuses a body that names no field,
        # so an all-null PATCH is a 422 rather than a cheerful 200.
        # Omitting is still right, and since ticket T-17 the reason is no
        # longer about the API -- an explicit null there now MEANS clear.
        # It is about this signature: update_ticket(id) and
        # update_ticket(id, title=None) are the same Python call, so a null
        # argument carries no intent to forward. Clearing says so through
        # `clear`. See q_core_mcp/clearing.py.
        body: dict = {}
        for key, value in (
            ("title", title),
            ("description", description),
            ("parent_id", parent_id),
            ("position", position),
        ):
            if value is not None:
                body[key] = value
        apply_clear(body, clear, "update_ticket")
        return await client.request("PATCH", f"/tickets/{ticket_id}", json=body)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            f"{PAGING_NOTE} "
            "List a ticket's attachments: id, filename, size, content "
            "type and upload time for each. This is the read that makes "
            "delete_attachment and read_attachment usable, because both "
            "take an attachment_id and this is where one comes from.\n\n"
            "get_ticket also embeds attachments, but it caps the list; "
            "use this when a ticket has more than a handful, or when you "
            "need to page. There is NO path in the result and that is "
            "deliberate -- content is reached by id through "
            "read_attachment, which resolves the path server-side.\n\n"
            "size_bytes and content_type may be null on attachments "
            "stored before those were recorded. Null means unknown, not "
            f"zero and not 'plain text'. {TICKET_REF}"
        ),
    )
    async def list_attachments(
        ticket_id: str,
        limit: int | None = None,
        offset: int = 0,
        all: bool = False,
        allow_truncated: bool = False,
    ) -> dict:
        return await list_request(
            client,
            f"/tickets/{ticket_id}/attachments",
            limit=limit,
            offset=offset,
            fetch_all=all,
            allow_truncated=allow_truncated,
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Read a ticket attachment BY ID. There is no path: the server "
            "resolves it from the attachment row, which is the point — a "
            "path handed to a caller is a path a caller hands back.\n\n"
            "Returns the decoded TEXT for a text attachment. Decoding is "
            "UTF-8 strict: a file that is not valid UTF-8 is refused "
            "rather than decoded lossily, because a lossy decode would "
            "put altered bytes of a log into your context while looking "
            "like the log.\n\n"
            "A BINARY attachment — a screenshot, a PDF — is refused with "
            "its media type and byte size. It is not base64'd: a base64 "
            "screenshot in context is worse than useless. Fetch "
            "GET /attachments/{id}/content if you need the bytes "
            "themselves.\n\n"
            "A file whose text carries an account-shaped digit run is "
            "ALSO refused, and the digits are not quoted in the refusal. "
            "Uploads are checked for this, so only a file stored before "
            "that check can trip it here. The right answer for a "
            "statement or receipt is register_document, which extracts "
            "and scrubs it."
        ),
    )
    async def read_attachment(attachment_id: str) -> dict:
        payload, media_type = await client.fetch_bytes(
            f"/attachments/{attachment_id}/content"
        )
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            # Named without the path: the refusal must not leak what the
            # response deliberately stopped publishing.
            raise ToolError(
                f"attachment {attachment_id} is not UTF-8 text "
                f"({media_type or 'unknown type'}, {len(payload)} bytes). "
                "Fetch GET /attachments/{id}/content for the raw bytes; it "
                "is not returned here because a base64 blob in context is "
                "worse than useless."
            ) from None
        # The belt to the upload check's braces. Upload now refuses any
        # UTF-8-decodable payload carrying an account number, but files
        # stored BEFORE that check existed were never examined, and this
        # is the tool that would put one into a model's context --
        # exactly what CLAUDE.md forbids ("never sent to any LLM").
        # Checked here rather than trusted from upload because the two
        # run at different times against different code.
        try:
            assert_no_account_numbers(text)
        except UnscrubbedDigitsError as exc:
            raise ToolError(
                f"attachment {attachment_id} contains an account-shaped "
                "digit run and is refused rather than returned; its text "
                "is deliberately not quoted here. It predates the upload "
                "check. If it is a statement or receipt, register it with "
                "register_document, which extracts and scrubs it."
            ) from exc
        return {
            "attachment_id": attachment_id,
            "media_type": media_type,
            "bytes": len(payload),
            "text": text,
        }

    @server.tool(
        annotations=DESTRUCTIVE,
        description=(
            "Delete a ticket permanently. Its attachments and its entire "
            "status history go with it — the audit trail is not recoverable "
            "afterwards, so prefer transitioning to done over deleting "
            "anything that actually happened. "
            f"{DELETE_TICKET_CONTRACT}; re-parent or delete "
            f"the children first. Its number is not reused. {TICKET_REF}"
        ),
    )
    async def delete_ticket(ticket_id: str) -> dict:
        return await client.request("DELETE", f"/tickets/{ticket_id}")

    def _attachment_roots() -> tuple[
        list[Path], list[tuple[Path, str]], Path, set[Path]
    ]:
        """Where attach_file may read, and the holes inside it.

        An allow-list WITH HOLES, stated as such. Measured before it was
        written: every sensitive directory on this machine resolves under
        the repo root -- `intake/`, `inbox/`, `data/documents/` and the
        database itself -- so "the working tree" alone is the deny-list
        mistake #70 removed, permitting exactly what must be refused.

        And no directory is safe by LOCATION. `data/documents/` holds the
        ORIGINAL bytes of every registered document, because
        register_document moves the file rather than a scrubbed copy. So
        containment is the outer bound here, not a certificate: it stops
        arbitrary reads, and the content check at the API decides whether
        what was read may be stored.
        """
        settings = client.settings
        allowed = [Path(root).resolve() for root in settings.attachment_roots]
        # Each hole is the sensitive thing ITSELF, never something it
        # happens to sit inside. A first version derived the data hole from
        # `db_path.parent`, which is correct only when the database has a
        # directory to itself: under test the parent IS the root, so every
        # path was refused, and in production a database at ~/q-core.db
        # would have made the whole home directory a hole. Deriving a
        # boundary from a NEIGHBOUR of the thing you mean is how a guard
        # ends up refusing everything or nothing.
        #
        # The data directory BY IDENTITY, never the database's parent.
        #
        # This is the third time this defect has appeared in this file, and
        # the second time it survived directly beneath the comment warning
        # against it. `db_path` is independently configurable: a database on
        # another volume left the rest of data/ readable, and a database at
        # ~/q-core.db made the whole home directory a hole -- refusing a
        # Desktop screenshot while permitting data/. The hole was data/ only
        # by coincidence of defaults.
        #
        # Writing the rule beside the code is not applying it. That is the
        # actual lesson, and it is why this line now names the thing it
        # means rather than something reliably next to it.
        #
        # And from SETTINGS, not REPO_ROOT: installed, REPO_ROOT is the
        # read-only Nix store and the real data lives elsewhere (review of
        # q-core #10, R1-F1).
        data_dir = Path(settings.data_dir).resolve()
        # These survive ANY value of attachment_roots, including "/". That
        # is the point: roots are configurable and holes are not, so
        # widening the allow-list cannot re-expose them. The first version
        # listed only the financial surface and left `.env` readable --
        # a deny-list that enumerated some of the dangerous things, which
        # is the failure mode #70 removed.
        # Only roots that are themselves INSIDE data/ carve a hole in it.
        # Without the filter, attachment_roots = "/" made every path
        # "inside an allowed root" and the data refusal collapsed -- the
        # widest possible root silently disabling the hole that exists to
        # survive exactly that. Found by the test written for it.
        # `is_relative_to` is REFLEXIVE: data_dir.is_relative_to(data_dir)
        # is True, so attachment_roots = "data/" carved a hole covering all
        # of data/ -- a NARROWER root granting more than "/" did, which is
        # the opposite of how an allow-list is supposed to behave. The
        # explicit inequality is the whole fix.
        allowed_inside_data = {
            root for root in (Path(r).resolve() for r in allowed)
            if root != data_dir and root.is_relative_to(data_dir)
        }
        holes = [
            (Path(settings.intake_dir).resolve(), "intake/ holds real bank statements"),
            (Path(settings.inbox_dir).resolve(), "inbox/ holds unprocessed documents"),
            (
                Path(settings.documents_dir).resolve(),
                "the documents store holds the ORIGINAL bytes of every "
                "registered document, not scrubbed copies",
            ),
            (
                Path(settings.logs_dir).resolve(),
                "logs can carry request detail and file paths",
            ),
            (
                Path(settings.jyra_dir).resolve(),
                "the attachment store holds other tickets' attachments",
            ),
            (
                Path(settings.secrets_dir).resolve(),
                "the secrets directory holds credentials such as the Google "
                "refresh token",
            ),
            (
                (Path(settings.db_path).parent / "backups").resolve(),
                "backups are full copies of the database",
            ),
            (
                Path("/proc"),
                "/proc exposes process environments, which carry "
                "Q_CORE_API_TOKEN",
            ),
            (
                (REPO_ROOT / ".git").resolve(),
                ".git holds remotes, credentials helpers and every past "
                "version of every file",
            ),
        ]
        return allowed, holes, data_dir, allowed_inside_data
    @server.tool(
        annotations=ADDITIVE,
        description=(
            "Attach a local file to a ticket — a screenshot, a log, a "
            "sample export. path is a path on THIS machine and is read and "
            "copied; the original is left alone.\n\n"
            f"{ATTACHMENT_CONTRACT}.\n\n"
            "By default attach_file reads from ONE directory, "
            "data/attachments-inbox/ — put the file there first. Reading "
            "from anywhere else means widening attachment_roots, which is "
            "a deliberate act by someone who can see what it permits, not "
            "something to work around a refusal.\n\n"
            "Some paths are refused whatever attachment_roots says: the "
            "intake, inbox and documents directories, the rest of data/, "
            "logs/, .git/, the database, its backups, the secrets "
            "directory, the private redaction profile, /proc, and the "
            "environment files (.env and the service's). To "
            "bring a document into q-core use register_document, which "
            "extracts and scrubs it. "
            "Returns the attachment's id, filename and hash — no path. "
            "Read it back with read_attachment(attachment_id), or fetch "
            "GET /attachments/{id}/content for the raw bytes. Attachment "
            "content is reached BY ID and the server resolves the path: a "
            "path handed to a caller is a path a caller will hand back, "
            "which is the bypass this design removes. "
            "The filename is stored for DISPLAY only and is kept exactly as "
            "given — never join it onto a path. The file on disk is named "
            "by attachment id instead, so two uploads called screenshot.png "
            f"do not collide. {TICKET_REF}"
        ),
    )
    async def attach_file(ticket_id: str, path: str) -> dict:
        source = Path(path)
        # Checked here rather than left to read_bytes, which raises
        # FileNotFoundError or IsADirectoryError — neither is a ToolError,
        # so the SDK would drop the real message and the model would see
        # only "Error executing tool attach_file". The path comes from a
        # model, so a wrong one is the likely case, not the rare one.
        if not source.exists():
            raise ToolError(
                f"No file at {path!r} on this machine. attach_file takes a "
                "local path to read, not a URL or a remote path."
            )
        # resolve() BEFORE comparing, so a symlink cannot smuggle a path
        # out of a hole -- the same reasoning _resolve_under_roots uses.
        resolved = source.resolve()
        allowed, holes, data_dir, allowed_inside_data = _attachment_roots()
        # A configured root that does not exist is refused HERE, naming the
        # setting, rather than at startup where it would have broken every
        # database request for a feature the caller was not using.
        missing = [root for root in allowed if not root.exists()]
        if missing and not any(resolved.is_relative_to(root) for root in allowed):
            names = ", ".join(str(root) for root in missing)
            raise ToolError(
                f"attachment_roots names a directory that does not exist: "
                f"{names}. Create it, or correct the setting."
            )

        if not any(resolved.is_relative_to(root) for root in allowed):
            roots = ", ".join(str(root) for root in allowed)
            raise ToolError(
                f"attach_file may only read files under: {roots}. The path "
                "given is outside every allowed root. Copy the file into one "
                "of them, or widen attachment_roots deliberately."
            )
        # The database file itself, by identity rather than by directory.
        if resolved == Path(client.settings.db_path).resolve():
            raise ToolError(
                "attach_file will not read the q-core database. It holds "
                "every transaction and document record in plaintext."
            )

        # The env file, by identity. It carries Q_CORE_API_TOKEN, and an
        # attachment is readable back by whoever can read the ticket -- so
        # attaching it hands over the credential for the whole API. Refused
        # whatever attachment_roots says, including "/".
        env_files = {
            Path(client.settings.model_config.get("env_file") or (REPO_ROOT / ".env")).resolve()
        }
        if client.settings.environment_file:
            # The service manager's secrets file (systemd EnvironmentFile).
            env_files.add(Path(client.settings.environment_file).resolve())
        # The redaction profile, by identity: its directory is a neighbour,
        # and under test that neighbour is the whole temp root.
        if resolved == Path(client.settings.privacy_profile_path).resolve():
            raise ToolError(
                "attach_file will not read the private redaction profile. It "
                "lists the personal names and addresses the scrubber removes."
            )
        if resolved in env_files:
            raise ToolError(
                "attach_file will not read the environment file. It holds "
                "Q_CORE_API_TOKEN, and an attachment can be read back by "
                "anyone who can read the ticket."
            )

        # All of data/ EXCEPT the configured attachment roots inside it.
        # Stated this way round on purpose: data/ gains directories over
        # time -- jyra/ did, documents/ did -- and a list of the ones to
        # refuse would be a list someone has to remember to extend.
        if resolved.is_relative_to(data_dir) and not any(
            resolved.is_relative_to(root) for root in allowed_inside_data
        ):
            raise ToolError(
                f"attach_file will not read from {data_dir} except the "
                "configured attachment roots inside it. data/ holds the "
                "database, the documents store and the attachment store "
                "itself."
            )

        for hole, reason in holes:
            if resolved.is_relative_to(hole):
                # Names the ROOT, never the path's own digits: an error
                # message is model context exactly like a response body.
                raise ToolError(
                    f"attach_file will not read from {hole} -- {reason}. "
                    "Attachments are stored verbatim and never redacted. To "
                    "bring a document into q-core, use register_document, "
                    "which extracts and scrubs it."
                )

        # Type-check only after containment and privacy holes. A directory is
        # still not attachable, but a forbidden directory such as `.git`
        # should be refused for the stronger security reason even in a normal
        # checkout where `.git` is a directory rather than a worktree pointer.
        if not source.is_file():
            raise ToolError(f"{path!r} is not a file — attach_file needs one file.")

        size = resolved.stat().st_size
        if size > MAX_ATTACHMENT_BYTES:
            raise ToolError(
                f"{path!r} is {size} bytes, over attach_file's "
                f"{MAX_ATTACHMENT_BYTES}-byte limit. Checked before reading, "
                "so an enormous file is refused rather than loaded."
            )

        return await client.request(
            "POST",
            f"/tickets/{ticket_id}/attachments",
            files={"upload": (source.name, resolved.read_bytes())},
        )

    @server.tool(
        annotations=DESTRUCTIVE,
        description=(
            "Delete one attachment and its file from disk. The ticket and "
            "its history are untouched. The local file the attachment was "
            "made from is not affected — only the copy q-core stored."
        ),
    )
    async def delete_attachment(attachment_id: str) -> dict:
        return await client.request("DELETE", f"/attachments/{attachment_id}")
