"""Extract text from a local document and scrub it before returning it.

The reason this is an API endpoint rather than something the intake skill
does for itself: a tool result is model context. If a model opens the PDF,
or if raw extracted text comes back in a response, the account number has
already been sent to an LLM no matter what happens next. So extraction and
redaction both complete here, in the server process, and only scrubbed text
crosses the boundary.

CLAUDE.md's rule ("never sent to any LLM") is therefore enforced at this
function, not at the caller. Callers cannot opt out, and there is no
endpoint that returns the raw text — deliberately, because one would
become the path everything used.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

import sqlite3

from fastapi.exceptions import RequestValidationError

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from api.auth import require_token
from api.config import Settings
from api.db import (
    MAX_LIMIT,
    get_connection,
    get_settings,
    paginate,
    refuse_real_path_under_pytest,
    require_reference,
)
from api.errors import ConflictError, InvalidReferenceError, NotFoundError
from api.models import DOC_TYPES, DocumentCreate, DocumentResponse
from api.redaction import UnscrubbedDigitsError, assert_no_account_numbers
from api.personal_redaction import load_profile, scrub_document
from api.spreadsheet import UnsupportedDocumentError, xlsx_to_csv_text

router = APIRouter(dependencies=[Depends(require_token)])

#: Suffixes read as plain text.
TEXT_SUFFIXES = {".csv", ".tsv", ".txt", ".md"}
#: Read by api.spreadsheet as CSV text, then scrubbed as a table.
SPREADSHEET_SUFFIXES = {".xlsx"}


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    #: Accept a document some of whose pages yielded no text. Off by
    #: default: a partially extracted statement is worse than an
    #: unreadable one, because its totals are wrong and look right. The
    #: caller opts in per request, so the acceptance is recorded rather
    #: than inferred from the fact that nobody objected.
    allow_partial: bool = False


class ExtractStoredRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_partial: bool = False


class ExtractResponse(BaseModel):
    source_id: str
    text_sha256: str
    text: str
    pages: int
    extracted_chars: list[int]
    redactions: int
    #: True only when `allow_partial` let an incomplete extraction through.
    partial: bool = False
    #: 1-based page numbers that yielded nothing, as a human reads a PDF.
    zero_pages: list[int] = []


class NoTextExtractedError(Exception):
    """Every page yielded zero characters."""


class PartialExtractionError(Exception):
    """Some pages extracted and some did not, without `allow_partial`.

    **Zero is a floor, not a definition of completeness.** This catches a
    page that yielded *nothing*; it does not catch a page that yielded
    too little. A continuation page extracting only a few dozen characters
    passes `all(extracted_chars)` quite
    legitimately, because a continuation stub genuinely
    carries about that much text.

    No character threshold is imposed, deliberately. Any number would be
    invented rather than measured — a cover page, a notice page and a
    continuation stub are all short and all correct, so a floor high
    enough to catch a failed extraction would reject real documents, and
    one low enough to spare them would catch nothing. Zero is the only
    value that means "the extractor returned nothing at all" rather than
    "this page is shorter than I expected".

    So a caller must still read the per-page counts for anything where
    completeness matters. This refusal narrows the gap; it does not close
    it, and saying so here is better than implying a guarantee that the
    test suite would then appear to confirm.
    """

    def __init__(self, extracted_chars: list[int], zero_pages: list[int]) -> None:
        super().__init__(
            f"{len(zero_pages)} of {len(extracted_chars)} pages yielded no text"
        )
        self.extracted_chars = extracted_chars
        self.zero_pages = zero_pages


class RedactionIncompleteError(Exception):
    """Scrubbed text still contained an account-shaped run."""


def _resolve_under_roots(candidate: str, settings: Settings) -> Path:
    """Resolve `candidate` and confirm it lands inside an allowed root.

    `resolve()` on both sides before comparing, so a symlink cannot smuggle
    a path out of the root and a symlinked root is compared like for like —
    the same approach `api/jyra.py` uses for attachments.

    Outside a root is a 400 rather than a 404: a caller reaching out of the
    tree is a different event from a typo, and the two should not look the
    same afterwards.
    """
    # inbox_dir is included, and the omission it fixes is worth recording:
    # this endpoint originally allowed intake_dir and documents_dir while
    # `/documents` allowed inbox_dir and intake_dir. Neither set was wrong
    # on its own, both had passing tests, and the intersection left inbox/
    # extractable by nothing -- so the document-intake skill could not
    # perform the first step of its own flow. Found by running that flow
    # end to end; testing either endpoint again would not have shown it.
    #
    # A RELATIVE path still resolves against intake_dir, deliberately.
    # statement-intake documents and relies on that, and resolving against
    # whichever root happens to hold a matching filename would make the
    # meaning of a bare name depend on the filesystem. Callers working in
    # inbox/ pass an absolute path.
    # Each root carries its own label in one structure. They were two
    # parallel sequences joined by zip(), and adding inbox_dir to the
    # roots without adding a third label silently dropped it: zip() stops
    # at the shorter input and reports nothing. The guard then ran over
    # two of three roots — and the missing one defaults to a real
    # directory this system moves files out of.
    #
    # Every root here needs the guard, because this call site opens files
    # under all of them and the function is named for none of them, which
    # is how a per-reader hook gets applied to part of it. intake_dir
    # defaults to REPO_ROOT/intake, which holds real statements;
    # documents_dir being guarded where init_db creates it is not the same
    # as being guarded here.
    intake_root = Path(settings.intake_dir).resolve()
    allowed = [
        (intake_root, "the intake directory"),
        (Path(settings.documents_dir).resolve(), "the documents directory"),
        (Path(settings.inbox_dir).resolve(), "the inbox directory"),
    ]
    for root, what in allowed:
        refuse_real_path_under_pytest(root, what=what)

    path = Path(candidate)
    # `intake_root` by name, not by index. The relative base used to be
    # roots[0], so reordering the list for any reason would have quietly
    # changed what a bare filename means — the same positional coupling
    # that produced the zip() defect, one line further down.
    resolved = (path if path.is_absolute() else intake_root / path).resolve()

    if not any(resolved == root or root in resolved.parents for root, _ in allowed):
        raise InvalidReferenceError(
            "path must be inside the intake, documents or inbox directory"
        )
    if not resolved.is_file():
        raise NotFoundError("no such document")
    return resolved


#: The largest attachment the API will store. Attachments are screenshots
#: and small notes by design, so this is generous rather than tight: the
#: cap exists to bound a mistake, not to ration legitimate use. A
#: model-supplied path makes a wrong file the likely case rather than the
#: rare one, and a wrong path is usually wrong by orders of magnitude.
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024


#: What attach_file's description must tell a model, stated once beside
#: the code that enforces it. Pinned exactly, per #107: a lower bound
#: catches a dropped clause and misses an ADDED one.
ATTACHMENT_CONTRACT = (
    "attachments are stored VERBATIM and never redacted; a file whose "
    "text carries an account number is refused rather than scrubbed. What "
    "counts as text is decided by CONTENT, not by suffix -- anything that "
    "decodes as UTF-8 is checked whatever it is named -- and only a file "
    "that genuinely cannot be read, a binary or a scanned PDF, is treated "
    "as unknown rather than clean, so it is allowed only because its "
    "directory is"
)


class AttachmentTooLargeError(Exception):
    """The upload exceeded MAX_ATTACHMENT_BYTES."""


class AttachmentNotRedactedError(Exception):
    """The attachment's readable text carries an account-shaped run."""


def attachment_text_or_none(payload: bytes, filename: str) -> str | None:
    """The attachment's text, or None if it has none to read.

    Deliberately the SAME extractor the documents path uses, so an
    attachment cannot be checked by a weaker reader than the one that
    decides whether a document is safe.

    Returns None for a binary, for a file type the extractor does not
    handle, and for a PDF that yields nothing — a scanned page. That last
    case is the one worth naming: a scanned statement is a PDF the
    extractor cannot read, and treating "no text found" as "no account
    numbers present" would wave through the single most sensitive thing
    on this machine. None means UNKNOWN, not clean, and the caller must
    treat it as a binary rather than as a pass.
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in TEXT_SUFFIXES and suffix != ".pdf":
        # Not a type the extractor claims, but the bytes may still BE
        # text: `.log` is the example that prompted this (D107 follow-up).
        # Suffix is a claim by the caller; whether the bytes decode is a
        # property of the bytes. Checking the claim and not the bytes let
        # an account number through under any suffix not on the list --
        # and read_attachment would then decode and return exactly that
        # text to a model, which is the case CLAUDE.md forbids.
        #
        # Strict, not errors="replace": a lossy decode turns arbitrary
        # binary into mojibake that scans as clean text, which would
        # report UNKNOWN files as checked.
        try:
            decoded = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return decoded if decoded.strip() else None

    import tempfile

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as handle:
        handle.write(payload)
        handle.flush()
        try:
            pages = _pages_of(Path(handle.name))
        except Exception:
            # An unreadable or malformed file is UNKNOWN, not clean.
            return None

    text = "\n".join(pages)
    return text if text.strip() else None


def refuse_unredacted_attachment(payload: bytes, filename: str) -> None:
    """Refuse an attachment whose readable text carries an account number.

    REFUSE rather than scrub, because an attachment is stored verbatim:
    there is no redacted form to keep, so the only safe answers are "store
    it as it is" or "do not store it".

    This is the PRIMARY control, not defence in depth. Path containment
    cannot certify that a file is clean, because no directory on this
    machine holds only clean files -- `data/documents/` stores the
    ORIGINAL bytes of every registered document, and `intake/` holds real
    statements. Safety here is a property of the content, so the content
    is what is checked.
    """
    text = attachment_text_or_none(payload, filename)
    if text is None:
        return
    try:
        assert_no_account_numbers(text)
    except UnscrubbedDigitsError as exc:
        # The message deliberately does not echo what it found: an error
        # body is model context exactly like a successful response, which
        # is the rule redaction_incomplete already follows.
        raise AttachmentNotRedactedError(
            "this file's text contains an account-shaped digit run, and "
            "attachments are stored verbatim rather than redacted, so it "
            "is refused. If it is a statement or receipt, register it with "
            "register_document instead, which extracts and scrubs it."
        ) from exc


def _pages_of(path: Path) -> list[str]:
    """Per-page text. One entry per page; a text or spreadsheet file is one page.

    Only the listed formats are read. Anything else used to be handed to the
    PDF reader and surfaced as a 500 (ticket T-09); it is now a named
    refusal, as is a `.pdf` whose content is not a readable PDF.
    """
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return [path.read_text(encoding="utf-8", errors="replace")]
    if suffix in SPREADSHEET_SUFFIXES:
        return [xlsx_to_csv_text(path)]
    if suffix != ".pdf":
        raise UnsupportedDocumentError(
            "Unsupported document format; supported: .pdf, .csv, .tsv, .txt, .md, .xlsx."
        )

    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(str(path))
        return [page.extract_text() or "" for page in reader.pages]
    except (PyPdfError, ValueError, KeyError, TypeError):
        raise UnsupportedDocumentError("File is not a readable PDF.") from None


def _extract_document(path: Path, allow_partial: bool, settings: Settings) -> dict:
    """`_extract_document_untraced` in an `intake.extract` span: kind and counts only."""
    from api.telemetry import file_kind, tracer

    with tracer.start_as_current_span("intake.extract", attributes={"q_core.file.kind": file_kind(path)}) as span:
        result = _extract_document_untraced(path, allow_partial, settings)
        span.set_attributes({
            "q_core.pages": result["pages"],
            "q_core.redactions": result["redactions"],
            "q_core.partial": result["partial"],
        })
        return result


def _extract_document_untraced(path: Path, allow_partial: bool, settings: Settings) -> dict:
    """Extract a document's text, redacted, with no raw text in the response.

    Over-redaction accepted by design, since none of it is needed
    downstream: phone numbers, zip+4, and cheque or confirmation numbers
    all match the same rule as an account number. The floor is seven digits
    for a contiguous run — Canadian bank account numbers are commonly seven
    — and nine for a separator-joined one, which is what keeps `2026-09-19`
    and grouped amounts intact. See api/redaction.py for the census behind
    those numbers (ticket T-10).

    A document whose every page extracts zero characters is an error rather
    than an empty success. A scanned statement produces exactly that, and
    "no text" and "no account numbers" must never be indistinguishable to
    the caller.

    A document where only *some* pages extract is refused too, unless the
    caller passes `allow_partial`. The per-page counts were always in the
    response, but being able to notice is not the same as noticing: a
    statement whose second page failed came back as a 200 and imported as
    though it were whole. Refusing by default moves that obligation off
    every future caller and into this function, which is the only place it
    can be discharged once.
    """
    profile = load_profile(settings.privacy_profile_path)
    source_id = hashlib.sha256(path.read_bytes()).hexdigest()
    pages = _pages_of(path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != source_id:
        raise ConflictError('Source changed during extraction; retry without using partial output')
    extracted_chars = [len(page) for page in pages]

    # All-empty is checked first and deliberately. A scanned document also
    # satisfies "some page is empty", so the more specific diagnosis has to
    # win — otherwise a document that yielded nothing at all is reported as
    # merely incomplete, and `allow_partial` would become a way to turn it
    # into a 200 with an empty string.
    if not any(extracted_chars):
        raise NoTextExtractedError(
            "no text could be extracted — the document may be scanned images"
        )

    zero_pages = [
        number for number, count in enumerate(extracted_chars, start=1) if count == 0
    ]
    if zero_pages and not allow_partial:
        # Raised before scrubbing, so nothing extracted is in scope to be
        # returned by accident. The refusal carries the counts, never the
        # text — an error body is model context exactly like a successful
        # one.
        raise PartialExtractionError(extracted_chars, zero_pages)

    result = scrub_document("\n".join(pages), path.suffix, profile)

    # The last line before the text leaves this process. If the scrubber met
    # a format it does not know, this refuses rather than returning it.
    try:
        assert_no_account_numbers(result.text)
    except UnscrubbedDigitsError as exc:
        logging.getLogger("api.documents").error(
            "refusing to return partially-redacted text: %s", exc,
        )
        raise RedactionIncompleteError(str(exc)) from exc

    # No filename: even a number-scrubbed basename can contain a personal
    # name or address. Request IDs provide correlation without leaking it.
    logging.getLogger("api.documents").info(
        "extracted document",
        extra={
            "pages": len(pages),
            "extracted_chars": extracted_chars,
            "redactions": result.redactions,
            "partial": bool(zero_pages),
            "zero_pages": zero_pages,
        },
    )

    return {
        "source_id": source_id,
        "text_sha256": hashlib.sha256(result.text.encode()).hexdigest(),
        "text": result.text,
        "pages": len(pages),
        "extracted_chars": extracted_chars,
        "redactions": result.redactions,
        "partial": bool(zero_pages),
        "zero_pages": zero_pages,
    }


@router.post("/documents/extract", response_model=ExtractResponse)
def extract_document_text(
    body: ExtractRequest,
    settings: Settings = Depends(get_settings),
) -> dict:
    """Extract and scrub a source path inside one of the allowed roots."""
    return _extract_document(
        _resolve_under_roots(body.path, settings), body.allow_partial, settings
    )


@router.post("/documents/{document_id}/extract", response_model=ExtractResponse)
def extract_registered_document_text(
    document_id: str,
    body: ExtractStoredRequest,
    settings: Settings = Depends(get_settings),
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    """Extract a registered document by id without disclosing its raw path."""
    row = connection.execute(
        "SELECT file_path FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"No document with id {document_id!r}")
    return _extract_document(
        _resolve_under_roots(row["file_path"], settings), body.allow_partial, settings
    )


#: How long a title-derived slug may be. Long enough to stay recognisable
#: in a directory listing, short enough that the whole filename -- slug,
#: timestamp and a uuid -- stays well inside any filesystem's limit.
MAX_SLUG = 60


def _slug(title: str) -> str:
    """Lowercase, non-alphanumerics collapsed to single hyphens, trimmed."""
    collapsed = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return collapsed[:MAX_SLUG].strip("-") or "document"


def _source_roots(settings: Settings) -> list[Path]:
    """Where a document may be registered FROM."""
    return [Path(settings.inbox_dir).resolve(), Path(settings.intake_dir).resolve()]


def _resolve_source(candidate: str, settings: Settings) -> Path:
    roots = _source_roots(settings)
    path = Path(candidate)
    resolved = (path if path.is_absolute() else roots[0] / path).resolve()
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise InvalidReferenceError(
            "file_path must be inside the inbox or intake directory"
        )
    if not resolved.is_file():
        raise NotFoundError("no such file")
    return resolved


def _is_under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


@router.post("/documents", response_model=DocumentResponse)
def register_document(
    body: DocumentCreate,
    settings: Settings = Depends(get_settings),
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    """Take a local file into `data/documents/` and record it.

    **Move from `inbox/`, copy from anywhere else**, and the asymmetry is
    deliberate rather than an inconsistency. `inbox/` is transient staging
    for mobile capture, so processing it should empty it -- a file left
    behind there would be re-processed. `intake/` holds the real statements
    and is under a standing do-not-modify constraint for this run, so a
    source there is copied and the original stays exactly where it was.

    If that constraint is ever lifted, the copy branch should be removed
    deliberately rather than tidied away as a redundant special case, and
    `api/tests/test_documents_crud.py` is what will have to change to do it.

    De-duplication is by SHA-256 of the file's bytes. An identical document
    returns the existing record and moves **nothing** -- the case where a
    move would be destructive and silent at once, since the caller gets a
    valid-looking record back while its source has quietly vanished.
    """
    source = _resolve_source(body.file_path, settings)

    if body.entity_id is not None:
        entity = connection.execute(
            "SELECT 1 FROM entities WHERE id = ?", (body.entity_id,)
        ).fetchone()
        if entity is None:
            raise InvalidReferenceError(f"No entity with id {body.entity_id!r}")

    content_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    existing = connection.execute(
        "SELECT * FROM documents WHERE content_hash = ?", (content_hash,)
    ).fetchone()
    if existing is not None:
        # Nothing is moved, copied or written. Returning the existing record
        # is the whole response.
        return dict(existing)

    documents_dir = Path(settings.documents_dir)
    refuse_real_path_under_pytest(documents_dir, what="the documents directory")
    documents_dir.mkdir(parents=True, exist_ok=True)

    document_id = str(uuid.uuid4())
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = (
        documents_dir / f"{_slug(body.title)}_{stamp}_{document_id}{source.suffix}"
    )

    if _is_under(source, Path(settings.inbox_dir).resolve()):
        shutil.move(str(source), destination)
    else:
        shutil.copy2(source, destination)

    connection.execute(
        "INSERT INTO documents (id, entity_id, title, doc_type, file_path, "
        "content_hash) VALUES (?, ?, ?, ?, ?, ?)",
        (
            document_id,
            body.entity_id,
            body.title,
            body.doc_type,
            str(destination.resolve()),
            content_hash,
        ),
    )
    connection.commit()
    return dict(
        connection.execute(
            "SELECT * FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
    )


@router.get("/documents/{document_id}", response_model=DocumentResponse)
def get_document(
    document_id: str,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    row = connection.execute(
        "SELECT * FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"No document with id {document_id!r}")
    return dict(row)


#: The order this endpoint returns, stated once beside the query
#: that produces it (ORDER BY imported_at DESC, rowid DESC). The tool description
#: interpolates this rather than retyping it, and a behavioural
#: test asserts the rows actually come back this way -- a flipped
#: ORDER BY is invisible to every caller who believed the sentence.
LIST_DOCUMENTS_ORDER = "newest first"


@router.get("/documents")
def list_documents(
    entity_id: str | None = None,
    doc_type: str | None = None,
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    # A filter naming something that does not exist is refused, not
    # answered with an empty page: the two are indistinguishable to
    # the caller, and only one of them is true (ticket T-08).
    require_reference(connection, "entities", entity_id, "entity")
    # Paths identify the raw local files and make bypassing the scrubber the
    # obvious next action. Content is extracted by document id instead.
    query = (
        "SELECT id, entity_id, title, doc_type, content_hash, imported_at "
        "FROM documents"
    )
    filters, params = [], []
    if entity_id is not None:
        filters.append("entity_id = ?")
        params.append(entity_id)
    # The same enum register_document enforces, and refused the same way.
    # The WRITER named all nine permitted values while this reader took
    # anything and answered with an empty page, so a caller who wrote
    # "policy" for "insurance_policy" was told there are none of those
    # rather than what the nine are (ticket T-11).
    if doc_type is not None and doc_type not in DOC_TYPES:
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("query", "doc_type"),
                    "msg": f"doc_type must be one of {', '.join(DOC_TYPES)}",
                    "input": doc_type,
                }
            ]
        )
    if doc_type is not None:
        filters.append("doc_type = ?")
        params.append(doc_type)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    # imported_at has one-second resolution, so it ties readily; rowid is
    # monotonic in insertion order and keeps a same-second pair stable.
    query += " ORDER BY imported_at DESC, rowid DESC"
    return paginate(connection, query, tuple(params), limit=limit, offset=offset)


@router.delete("/documents/{document_id}")
def delete_document(
    document_id: str,
    settings: Settings = Depends(get_settings),
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    """Remove the row and the file together, leaving no orphan either way.

    The stored path is re-checked against `documents_dir` before anything is
    unlinked. It was written by this API, but it is still data in a table,
    and trusting it would turn a single bad row into an arbitrary file
    deletion.
    """
    row = connection.execute(
        "SELECT * FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"No document with id {document_id!r}")

    stored = Path(row["file_path"]).resolve()
    documents_dir = Path(settings.documents_dir).resolve()
    if not _is_under(stored, documents_dir):
        raise InvalidReferenceError(
            "this document's file is outside the documents directory; "
            "refusing to delete it"
        )

    refuse_real_path_under_pytest(documents_dir, what="the documents directory")
    stored.unlink(missing_ok=True)
    connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))
    connection.commit()
    return {"deleted": True}
