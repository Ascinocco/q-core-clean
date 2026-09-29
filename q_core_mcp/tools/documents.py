"""Tools over api/documents.py: reading a local document safely.

The point of routing this through the API rather than letting a model open
the file is that a tool result IS model context. Extraction and redaction
both complete server-side, so what comes back here is already scrubbed and
there is no tool that returns the raw text.
"""

from api.documents import LIST_DOCUMENTS_ORDER
from q_core_mcp.paging import MAX_LIMIT, PAGING_NOTE, list_request
from mcp.server import MCPServer

from q_core_mcp.annotations import (
    ADDITIVE,
    DESTRUCTIVE,
    READ_ONLY,
)
from q_core_mcp.client import QCoreClient


DOC_TYPES = (
    "statement, receipt, tax, insurance_policy, lease, title_deed, "
    "warranty, correspondence, other"
)


def register_document_tools(server: MCPServer, client: QCoreClient) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Read a statement or receipt from intake/ or data/documents/ as "
            "text, with account, routing, card and SSN numbers already "
            "removed server-side. Use this INSTEAD OF opening the file "
            "yourself: opening a bank PDF directly puts the account number "
            "into this conversation, which is exactly what this tool exists "
            "to prevent. path is relative to the intake directory, or an "
            "absolute path inside intake/, inbox/ or data/documents/. "
            "Amounts, "
            "dates and merchant names are preserved; long digit runs such "
            "as phone or confirmation numbers are removed along with "
            "account numbers. Returns the redacted text, the page count, "
            "the characters extracted per page, and how many redactions "
            "were made. A document whose pages all extract zero characters "
            "is an error, not empty text — that usually means a scanned "
            "image, which needs OCR this system does not have. A document "
            "where only SOME pages extract is also refused, with "
            "partial_extraction and the page numbers that came back empty: "
            "a half-extracted statement imports as though it were whole, so "
            "its totals are wrong and look right. Pass allow_partial=true "
            "to accept one — appropriate for a blank cover or notice page, "
            "never for a bank or card statement, where a missing page means "
            "missing transactions."
            " Personal names/address variants are removed using a locally reviewed "
            "private profile and recognizable labelled/postal blocks; this is not "
            "universal name recognition. CSV Note is withheld. Readable formats: "
            ".pdf, .csv, .tsv, .txt, .md and a single-sheet .xlsx (a spreadsheet "
            "activity export, returned as CSV text with its Tag column withheld); "
            "anything else is refused with unsupported_document_format. Tabular "
            "files must match a reviewed column layout. Missing/invalid "
            "profile or ambiguous personal sections return personal_redaction_required. "
            "On that refusal, STOP and ask the user to review their local privacy "
            "configuration; never open the raw source or weaken the profile yourself."
        ),
    )
    async def extract_document_text(path: str, allow_partial: bool = False) -> dict:
        body: dict = {"path": path}
        # Omitted rather than sent as false: the fail-closed default belongs
        # to the endpoint, and restating it here would be a second place for
        # it to live — which is how two defaults come to disagree.
        if allow_partial:
            body["allow_partial"] = True
        return await client.request("POST", "/documents/extract", json=body)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Extract and server-side scrub a document that is already "
            "registered, using its document id. This is the safe way to "
            "read stored content: raw filesystem paths are deliberately not "
            "returned by document tools. The same fail-closed redaction and "
            "partial-page rules as extract_document_text apply."
        ),
    )
    async def extract_registered_document_text(
        document_id: str, allow_partial: bool = False
    ) -> dict:
        body: dict = {}
        if allow_partial:
            body["allow_partial"] = True
        return await client.request(
            "POST", f"/documents/{document_id}/extract", json=body
        )

    @server.tool(
        annotations=ADDITIVE,
        description=(
            "Take a local file into the documents store and record it. "
            "path is relative to the inbox directory, or an absolute path "
            "inside inbox/ or intake/. A file in inbox/ is MOVED (that "
            "directory is transient staging and should empty as it is "
            "processed); a file anywhere else, including intake/, is "
            "COPIED and its original left exactly where it was. "
            "De-duplication is by file content: registering a document "
            "whose bytes are already stored returns the existing record "
            "and moves nothing at all. title is required and becomes part "
            f"of the stored filename. doc_type is one of: {DOC_TYPES}. "
            "entity_id optionally links the document to a person, "
            "property, vehicle, pet, account or project. Returns the "
            "record metadata. The raw file path is deliberately withheld; "
            "use extract_registered_document_text with the returned id."
        ),
    )
    async def register_document(
        path: str,
        title: str,
        doc_type: str | None = None,
        entity_id: str | None = None,
    ) -> dict:
        body: dict = {"file_path": path, "title": title}
        # Omitted rather than sent as null: the API forbids unknown keys and
        # an explicit null is a different statement from "not supplied".
        if doc_type is not None:
            body["doc_type"] = doc_type
        if entity_id is not None:
            body["entity_id"] = entity_id
        return await client.request("POST", "/documents", json=body)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Get one stored document's metadata by id. The raw filesystem "
            "path is deliberately withheld; read content through "
            "extract_registered_document_text."
        ),
    )
    async def get_document(document_id: str) -> dict:
        return await client.request("GET", f"/documents/{document_id}")

    @server.tool(
        annotations=READ_ONLY,
        description=(
            f"{PAGING_NOTE} "
            f"List stored documents, {LIST_DOCUMENTS_ORDER}. entity_id filters to one "
            f"entity's documents; doc_type filters to one of: {DOC_TYPES}. "
            f"limit is 1-{MAX_LIMIT}. Returns "
            '{"items", "total", "limit", "offset"}. Raw filesystem paths '
            "are deliberately withheld; extract a selected item by id."
        ),
    )
    async def list_documents(
        entity_id: str | None = None,
        doc_type: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        all: bool = False,
        allow_truncated: bool = False,
    ) -> dict:
        params: dict = {}
        if entity_id is not None:
            params["entity_id"] = entity_id
        if doc_type is not None:
            params["doc_type"] = doc_type
        return await list_request(
            client, "/documents", params=params,
            limit=limit, offset=offset, fetch_all=all,
            allow_truncated=allow_truncated,
        )

    @server.tool(
        annotations=DESTRUCTIVE,
        description=(
            "Permanently delete a stored document: both its record and the "
            "file on disk. There is no undo and no copy kept. The file is "
            "only deleted if it is inside the documents store."
        ),
    )
    async def delete_document(document_id: str) -> dict:
        return await client.request("DELETE", f"/documents/{document_id}")
