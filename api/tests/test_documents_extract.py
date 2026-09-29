"""Tests for POST /documents/extract.

The endpoint exists so that reading a bank statement does not require a
model to open it. That makes one property load-bearing above all others:
**no response this endpoint produces may contain an account number** —
because a tool result is model context, and the MCP tool wrapping this
returns exactly what it returns.

Every fixture is generated in-process from invented numbers. No real
statement is in the repo, in these tests, or in any transcript.
"""

import json
import logging
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.tests.pdf_fixture import make_imageless_pdf, make_pdf

FAKE_CARD = "4532015112830366"
FAKE_ROUTING = "021000021"


def test_source_hash_is_server_computed_and_changing_file_refuses(client_with_intake, intake, monkeypatch):
    import hashlib
    from api import documents
    client, settings = client_with_intake
    path = intake / 'source.txt'
    path.write_text('CAFE 5.00')
    response = _extract(client, settings, 'source.txt')
    assert response.status_code == 200
    assert response.json()['source_id'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert response.json()['text_sha256'] == hashlib.sha256(response.json()['text'].encode()).hexdigest()
    def changing(source):
        source.write_text('changed')
        return ['CAFE 5.00']
    monkeypatch.setattr(documents, '_pages_of', changing)
    assert _extract(client, settings, 'source.txt').status_code == 409


@pytest.fixture()
def intake(tmp_path: Path) -> Path:
    directory = tmp_path / "intake"
    directory.mkdir()
    return directory


@pytest.fixture()
def client_with_intake(test_settings, intake):
    from api.config import get_settings
    from api.main import app

    settings = test_settings.model_copy(update={"intake_dir": str(intake)})
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app), settings
    app.dependency_overrides.clear()


def _auth(settings) -> dict:
    return {"Authorization": f"Bearer {settings.api_token}"}


def _extract(client, settings, path: str, **extra):
    """`extra` goes straight into the body, so a test that does not pass
    `allow_partial` sends no such key — the default under test is the
    endpoint's, not this helper's."""
    return client.post(
        "/documents/extract",
        json={"path": path, **extra},
        headers=_auth(settings),
    )


def test_an_account_number_never_reaches_the_response(client_with_intake, intake):
    """The property the whole ticket exists for."""
    client, settings = client_with_intake
    (intake / "statement.pdf").write_bytes(
        make_pdf([f"Account Number {FAKE_CARD}\nPurchase 84.99 on 2026-09-19"])
    )

    response = _extract(client, settings, "statement.pdf")

    assert response.status_code == 200
    body = response.text
    assert FAKE_CARD not in body
    assert "4532" not in body or "****0366" in response.json()["text"]


def test_the_useful_content_survives(client_with_intake, intake):
    """Redaction that ate the amounts would satisfy the test above and be
    useless. Both directions, always."""
    client, settings = client_with_intake
    (intake / "statement.pdf").write_bytes(
        make_pdf([f"Account {FAKE_CARD}\nMAPLE DONUTS 84.99 2026-09-19\nESSO 45.10"])
    )

    text = _extract(client, settings, "statement.pdf").json()["text"]

    assert "84.99" in text
    assert "45.10" in text
    assert "2026-09-19" in text
    assert "MAPLE DONUTS" in text


def test_the_response_reports_pages_and_redactions(client_with_intake, intake):
    client, settings = client_with_intake
    (intake / "two.pdf").write_bytes(
        make_pdf([f"Account {FAKE_CARD}", f"Routing {FAKE_ROUTING}"])
    )

    body = _extract(client, settings, "two.pdf").json()

    assert body["pages"] == 2
    assert body["redactions"] == 2
    assert len(body["extracted_chars"]) == 2
    assert all(count > 0 for count in body["extracted_chars"])


def test_a_document_that_extracts_nothing_is_an_error_not_an_empty_success(
    client_with_intake, intake
):
    """A scanned statement yields no text from pypdf. Returning 200 with an
    empty string would be indistinguishable from a clean document, and the
    caller would conclude there was nothing to redact. The caller must not
    have to inspect an array to notice that nothing was read.
    """
    client, settings = client_with_intake
    (intake / "scanned.pdf").write_bytes(make_imageless_pdf())

    response = _extract(client, settings, "scanned.pdf")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "no_text_extracted"


def test_a_partially_extracted_document_is_refused_by_default(
    client_with_intake, intake
):
    """This test used to assert the opposite, and the docstring said why:
    "one blank page among several is normal", reported per page so a
    caller *can* tell. That is true and it was the wrong default.

    "Can tell" is not "will tell". The per-page counts were already in a
    200 response and nothing was obliged to read them, so a statement
    whose page 2 failed to extract came back as success carrying half a
    statement — and a partial statement is worse than none, because
    importing it produces totals that are wrong and look right.

    The genuine blank cover page is still a real case. It is now
    `allow_partial`, so the caller's acceptance is recorded in the request
    rather than assumed on their behalf.
    """
    client, settings = client_with_intake
    (intake / "mixed.pdf").write_bytes(make_pdf([f"Account {FAKE_CARD}", ""]))

    response = _extract(client, settings, "mixed.pdf")

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "partial_extraction"
    assert error["extracted_chars"][1] == 0
    assert error["zero_pages"] == [2], "1-based, as a human reads a PDF"


def test_the_partial_refusal_withholds_the_text_it_did_extract(
    client_with_intake, intake
):
    """An error body is model context exactly like a successful one.

    Returning the page that *did* extract would hand over the text while
    calling it a refusal — the caller ends up holding the account number
    either way, which is the outcome redaction_incomplete already refuses
    to produce. The counts say what happened; the text is withheld.
    """
    client, settings = client_with_intake
    (intake / "mixed.pdf").write_bytes(make_pdf([f"Account {FAKE_CARD}", ""]))

    response = _extract(client, settings, "mixed.pdf")

    serialised = json.dumps(response.json())
    assert FAKE_CARD not in serialised
    assert "Account" not in serialised, "no extracted text in a refusal"
    assert "text" not in response.json()["error"]


def test_allow_partial_returns_the_text_and_flags_it(client_with_intake, intake):
    """The cover-page case, opted into explicitly."""
    client, settings = client_with_intake
    (intake / "mixed.pdf").write_bytes(make_pdf([f"Account {FAKE_CARD}", ""]))

    response = _extract(client, settings, "mixed.pdf", allow_partial=True)

    assert response.status_code == 200
    body = response.json()
    assert body["partial"] is True
    assert body["zero_pages"] == [2]
    assert body["extracted_chars"][1] == 0
    assert FAKE_CARD not in body["text"], "opting into partial is not opting out of redaction"


def test_a_fully_extracted_document_is_not_flagged_partial(
    client_with_intake, intake
):
    """The ordinary case must not acquire a warning it does not deserve.

    A flag that is set on documents which are fine is a flag nobody reads.
    """
    client, settings = client_with_intake
    (intake / "whole.pdf").write_bytes(
        make_pdf([f"Account {FAKE_CARD}", "PAYMENT 84.99"])
    )

    body = _extract(client, settings, "whole.pdf").json()

    assert body["partial"] is False
    assert body["zero_pages"] == []
    assert all(count > 0 for count in body["extracted_chars"])


def test_a_document_with_every_page_empty_is_still_no_text_extracted(
    client_with_intake, intake
):
    """Order of the two checks, asserted rather than assumed.

    A scanned document has zero characters on every page, so it satisfies
    "some page is empty" as well — if the partial check ran first it would
    answer partial_extraction, and the caller would be told a document
    that yielded nothing at all is merely incomplete. All-empty is the
    more specific diagnosis and has to win.
    """
    client, settings = client_with_intake
    (intake / "both-blank.pdf").write_bytes(make_pdf(["", ""]))

    response = _extract(client, settings, "both-blank.pdf")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "no_text_extracted"


def test_allow_partial_does_not_rescue_a_document_with_no_text_at_all(
    client_with_intake, intake
):
    """allow_partial concedes a blank page, not a blank document.

    Otherwise the opt-in becomes a way to make a scanned statement return
    200 with an empty string — reintroducing, behind a flag, the exact
    confusion no_text_extracted exists to prevent.
    """
    client, settings = client_with_intake
    (intake / "scanned2.pdf").write_bytes(make_imageless_pdf())

    response = _extract(client, settings, "scanned2.pdf", allow_partial=True)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "no_text_extracted"


def test_a_csv_is_extracted_and_scrubbed_too(client_with_intake, intake):
    """Intake documents are not all PDFs. A tool that handled only
    PDFs would leave the skill opening CSVs directly, which is the same
    exposure this endpoint exists to close."""
    client, settings = client_with_intake
    (intake / "export.csv").write_text(
        f"date,description,amount\n2026-09-19,PAYMENT {FAKE_CARD},84.99\n"
    )

    body = _extract(client, settings, "export.csv").json()

    assert FAKE_CARD not in body["text"]
    assert "84.99" in body["text"]
    assert body["redactions"] == 1


# --- path containment ------------------------------------------------------


def test_a_path_outside_the_allowed_roots_is_rejected(client_with_intake, tmp_path):
    """400 rather than 404: a caller reaching outside the roots is a
    different event from a typo, and the log should be able to tell them
    apart."""
    client, settings = client_with_intake
    outside = tmp_path / "elsewhere.pdf"
    outside.write_bytes(make_pdf(["nothing secret"]))

    response = _extract(client, settings, str(outside))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_reference"


def test_traversal_out_of_the_intake_directory_is_rejected(client_with_intake):
    client, settings = client_with_intake

    response = _extract(client, settings, "../../etc/passwd")

    assert response.status_code == 400


def test_a_missing_file_inside_the_root_is_a_not_found(client_with_intake):
    client, settings = client_with_intake

    response = _extract(client, settings, "no-such-file.pdf")

    assert response.status_code == 404


def test_a_symlink_pointing_outside_the_root_is_rejected(
    client_with_intake, intake, tmp_path
):
    """resolve() before comparing, so a symlink cannot smuggle a path out."""
    client, settings = client_with_intake
    target = tmp_path / "outside.pdf"
    target.write_bytes(make_pdf([f"Account {FAKE_CARD}"]))
    (intake / "link.pdf").symlink_to(target)

    response = _extract(client, settings, "link.pdf")

    assert response.status_code == 400


# --- the log must not become the leak --------------------------------------


def test_nothing_account_shaped_reaches_the_log(
    client_with_intake, intake, test_settings, tmp_path
):
    """The request-logging middleware writes every request to api.log. An
    endpoint whose whole purpose is keeping account numbers out of model
    context would be self-defeating if it wrote them to disk instead —
    and a log file is read by a model during debugging.
    """
    from api.logging_config import configure_logging

    client, settings = client_with_intake
    logs = tmp_path / "logs"
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    root.handlers = []
    try:
        configure_logging(settings.model_copy(update={"logs_dir": str(logs)}))
        (intake / f"stmt-{FAKE_CARD}.pdf").write_bytes(
            make_pdf([f"Account {FAKE_CARD}\nTotal 84.99"])
        )
        _extract(client, settings, f"stmt-{FAKE_CARD}.pdf")
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers, root.level = saved_handlers, saved_level

    log_text = (logs / "api.log").read_text()
    assert FAKE_CARD not in log_text

    # Machine-generated fields are excluded before the digit sweep, and the
    # reason is worth stating so nobody "fixes" the exclusion back out:
    # request_id is uuid4().hex[:12], and a 12-character hex string carries
    # a run of nine or more digits far more often than intuition suggests.
    # Measured over 200,000 ids: 0.35% are entirely digits, and many more
    # carry a long digit prefix (0123456789ab contains a ten-digit run).
    # Sweeping the raw file therefore failed roughly one run in three,
    # flagging a REQUEST ID as an account number. Everything else is still
    # swept, so a leak into any document-derived field is still caught —
    # verified by mutating the endpoint to log an unscrubbed filename,
    # which turns this red.
    machine_generated = {"request_id", "timestamp"}

    # Pinned exactly. The danger with an exclusion list is that it grows by
    # one word at a time — "and also the path field" — until the sweep
    # covers nothing that matters. Adding a name here has to be a
    # deliberate edit to this assertion, not a quiet addition above.
    assert machine_generated == {"request_id", "timestamp"}, (
        "the exclusion set has changed; every name in it is a field this "
        "test no longer checks for leaked account numbers"
    )
    for line in log_text.splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        payload = json.dumps(
            {k: v for k, v in record.items() if k not in machine_generated}
        )
        runs = re.findall(r"(?<!\d)\d{9,}(?!\d)", payload)
        assert not runs, f"account-shaped run reached the log: {runs}"


def test_the_log_still_records_something_useful(
    client_with_intake, intake, tmp_path
):
    """Guards the vacuous version of the test above: a log containing
    nothing would pass it."""
    from api.logging_config import configure_logging

    client, settings = client_with_intake
    logs = tmp_path / "logs"
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    root.handlers = []
    try:
        configure_logging(settings.model_copy(update={"logs_dir": str(logs)}))
        (intake / "statement.pdf").write_bytes(make_pdf([f"Account {FAKE_CARD}"]))
        _extract(client, settings, "statement.pdf")
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers, root.level = saved_handlers, saved_level

    lines = [
        json.loads(line)
        for line in (logs / "api.log").read_text().splitlines()
        if line.strip()
    ]
    assert any(line.get("path") == "/documents/extract" for line in lines)


def test_a_path_that_is_only_inside_after_resolution_is_accepted(
    client_with_intake, intake, tmp_path
):
    """The accept direction of the containment check.

    A rejection test alone cannot show that `.resolve()` runs: a lexically
    outside path that also resolves outside is refused with or without it.
    This path reads as outside the root as text — it starts with a `..`
    segment — and lands inside only once resolved, so it fails if the guard
    compares strings instead of resolved paths.

    Added after runbooks/team-workflow.md recorded the same blind spot in
    another PR's traversal test, where the case chosen was lexically inside
    and therefore passed with or without resolution.
    """
    client, settings = client_with_intake
    (intake / "statement.pdf").write_bytes(make_pdf([f"Account {FAKE_CARD}"]))
    (tmp_path / "elsewhere").mkdir()

    # Routed through a SIBLING directory on purpose. `intake/../intake/x`
    # would not discriminate: pathlib leaves `..` in `parents`, so the root
    # is still a lexical ancestor and the guard matches with or without
    # resolution. Going out through a sibling makes the root genuinely
    # absent from the unresolved parents, so this fails if `.resolve()` is
    # dropped. The first version of this test used the non-discriminating
    # shape and passed under that mutation.
    detour = tmp_path / "elsewhere" / ".." / "intake" / "statement.pdf"

    response = _extract(client, settings, str(detour))

    assert response.status_code == 200, response.text
    assert FAKE_CARD not in response.text


def test_a_file_in_the_inbox_can_be_extracted(test_settings, tmp_path):
    """The composition case, missed because the two halves were tested apart.

    `/documents/extract` allowed `intake_dir` and `documents_dir`;
    `/documents` (register) allowed `inbox_dir` and `intake_dir`. Neither
    endpoint was wrong on its own and both had passing tests, but the
    intersection left `inbox/` extractable by nothing — so the
    document-intake skill, whose entire territory is `inbox/`, could not
    perform the first step of its own flow.

    Found by running the skill's flow end to end rather than by testing
    either endpoint again.
    """
    from api.config import get_settings
    from api.main import app
    from api.tests.pdf_fixture import make_pdf

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "receipt.pdf").write_bytes(make_pdf(["AUTOPARTS DEPOT TOTAL 84.99"]))
    settings = test_settings.model_copy(update={"inbox_dir": str(inbox)})

    app.dependency_overrides[get_settings] = lambda: settings
    try:
        response = TestClient(app).post(
            "/documents/extract",
            json={"path": str(inbox / "receipt.pdf")},
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert "AUTOPARTS DEPOT" in response.json()["text"]
