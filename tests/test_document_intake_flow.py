"""The document-intake skill's flow, executed end to end.

A prose transcript of a dry run is not verifiable — nobody can re-run it,
and it goes stale the moment either endpoint moves. This is the dry run as
a test: the same steps `plugin/skills/document-intake/SKILL.md` tells a
model to take, driven through the real MCP tools against the real API,
with synthetic files in a tmp inbox.

It exists because the flow is a test subject in its own right. Every
endpoint it touches had passing tests while the flow itself was
impossible: `/documents/extract` allowed `intake_dir` and `documents_dir`,
`/documents` allowed `inbox_dir` and `intake_dir`, and nothing allowed a
file in `inbox/` to be *extracted* — so step 2 of the skill's own
procedure could not run. Neither endpoint was wrong. Testing either one
again would not have found it.

Every document here is invented. No real statement or receipt is used.
"""

import json

import anyio
import httpx
import pytest

from mcp.server.mcpserver.exceptions import ToolError

from api.config import REPO_ROOT, Settings, get_settings
from api.main import app
from api.tests.pdf_fixture import make_pdf
from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server

FAKE_CARD = "4532015112830366"


@pytest.fixture()
def intake_flow(tmp_path):
    """A tmp inbox, a tmp non-inbox source, and the MCP tools wired to the
    real app — the arrangement the skill actually runs in."""
    inbox = tmp_path / "inbox"
    elsewhere = tmp_path / "elsewhere"
    for directory in (inbox, elsewhere):
        directory.mkdir()

    receipt_bytes = make_pdf(
            [
                "AUTOPARTS DEPOT #0123 ANYTOWN XY\nWINTER TIRES TOYOTA COROLLA\n"
                f"Card {FAKE_CARD}\nTOTAL 612.30\nSep 19, 2026"
            ]
    )
    (inbox / "tire-receipt.pdf").write_bytes(receipt_bytes)
    (elsewhere / "policy.pdf").write_bytes(
        make_pdf(["AUTO POLICY 2026 ANNUAL PREMIUM 1105.00"])
    )

    # Built here rather than borrowed from a package conftest: this test
    # spans api/ and q_core_mcp/ and belongs to neither. Every writable
    # path is under tmp_path, which is the same rule the settings guards
    # enforce on the fixtures.
    profile = tmp_path / 'privacy.json'
    profile.write_text(json.dumps({'version': 1, 'reviewed': True,
                                   'names': ['Alex Example'], 'addresses': ['123 Example Lane']}))
    profile.chmod(0o600)
    settings = Settings(
        _env_file=None,
        api_token="flow-test-token",
        db_path=str(tmp_path / "q-core.db"),
        documents_dir=str(tmp_path / "documents"),
        inbox_dir=str(inbox),
        intake_dir=str(elsewhere),
        jyra_dir=str(tmp_path / "jyra"),
        logs_dir=str(tmp_path / "logs"),
        privacy_profile_path=str(profile),
        schema_path=str(REPO_ROOT / "db" / "schema.sql"),
        seed_categories_path=str(REPO_ROOT / "db" / "seed_categories.sql"),
        migrations_dir=str(REPO_ROOT / "db" / "migrations"),
    )
    app.dependency_overrides[get_settings] = lambda: settings
    server = build_server(
        QCoreClient(settings, transport=httpx.ASGITransport(app=app))
    )

    def call(tool: str, args: dict):
        """Return the tool's payload, not the CallToolResult wrapper.

        `structured_content` exists but is None for some tools, so a
        `getattr` default never fires — the payload is then the JSON in
        the first text block. Same extraction as
        `q_core_mcp/tests/conftest.py`'s `call_tool`, duplicated rather
        than imported because both `api/` and `q_core_mcp/` have a
        `tests` package and importing across them by module path is
        ambiguous.
        """
        result = anyio.run(lambda: server.call_tool(tool, args))
        if result.structured_content is not None:
            return result.structured_content
        return json.loads(result.content[0].text)

    yield {
        "call": call,
        "inbox": inbox,
        "elsewhere": elsewhere,
        "receipt_bytes": receipt_bytes,
    }
    app.dependency_overrides.clear()


def test_step_2_a_file_in_the_inbox_can_be_extracted(intake_flow):
    """The step that was impossible. Absolute path, because a bare name
    resolves against `intake/` — see the skill and api/documents.py."""
    source = intake_flow["inbox"] / "tire-receipt.pdf"

    result = intake_flow["call"]("extract_document_text", {"path": str(source)})

    assert result["pages"] == 1
    assert result["extracted_chars"][0] > 0


def test_step_2_the_extracted_text_is_already_scrubbed(intake_flow):
    """The skill never sees the card number: redaction completes server-side
    before the text is returned."""
    source = intake_flow["inbox"] / "tire-receipt.pdf"

    result = intake_flow["call"]("extract_document_text", {"path": str(source)})

    assert FAKE_CARD not in result["text"]
    assert result["redactions"] >= 1
    # …while the content the skill classifies on survives.
    assert "AUTOPARTS DEPOT" in result["text"]
    assert "TOYOTA" in result["text"]
    assert "612.30" in result["text"]


def test_step_6_registering_from_the_inbox_moves_the_source(intake_flow):
    source = intake_flow["inbox"] / "tire-receipt.pdf"

    intake_flow["call"](
        "register_document",
        {
            "path": str(source),
            "title": "Toyota winter tires receipt",
            "doc_type": "receipt",
        },
    )

    assert not source.exists()


def test_step_6_registering_from_elsewhere_leaves_the_source(intake_flow):
    source = intake_flow["elsewhere"] / "policy.pdf"

    intake_flow["call"](
        "register_document",
        {
            "path": str(source),
            "title": "Auto policy 2026",
            "doc_type": "insurance_policy",
        },
    )

    assert source.exists(), "a non-inbox source must be copied, not moved"


def test_a_duplicate_returns_the_existing_record_and_moves_nothing(intake_flow):
    first = intake_flow["call"](
        "register_document",
        {
            "path": str(intake_flow["inbox"] / "tire-receipt.pdf"),
            "title": "Toyota winter tires receipt",
            "doc_type": "receipt",
        },
    )
    duplicate = intake_flow["inbox"] / "again.pdf"
    duplicate.write_bytes(intake_flow["receipt_bytes"])

    second = intake_flow["call"](
        "register_document",
        {"path": str(duplicate), "title": "Second attempt", "doc_type": "receipt"},
    )

    assert second["id"] == first["id"]
    assert duplicate.exists(), "a duplicate must leave its source untouched"


def test_after_registering_the_original_path_is_dead_and_the_stored_copy_works(
    intake_flow,
):
    """The skill's closing rule, asserted rather than asserted-about.

    Re-extracting the original path after a move fails in a way that looks
    like the file was never there. The stored copy is therefore addressed
    by document id, without handing the raw path to the model.

    The refusal is asserted by message, not merely by "something raised".
    A bare `raises(Exception)` passes when the call fails for any reason —
    a renamed tool, a broken fixture, a settings error — and in this
    file it did exactly that: before inbox_dir was an allowed root, this
    test went green on the path-containment refusal, which says nothing
    about whether the file moved. "no such document" is the only outcome
    that means what the docstring claims.
    """
    source = intake_flow["inbox"] / "tire-receipt.pdf"
    created = intake_flow["call"](
        "register_document",
        {"path": str(source), "title": "Toyota winter tires receipt", "doc_type": "receipt"},
    )

    with pytest.raises(ToolError) as excinfo:
        intake_flow["call"]("extract_document_text", {"path": str(source)})
    assert "no such document" in str(excinfo.value), (
        f"the path must be gone, not unreachable: {excinfo.value}"
    )

    again = intake_flow["call"](
        "extract_registered_document_text", {"document_id": created["id"]}
    )
    assert again["extracted_chars"][0] > 0


def test_step_7_filed_documents_are_read_by_id_without_a_raw_path(intake_flow):
    intake_flow["call"](
        "register_document",
        {
            "path": str(intake_flow["inbox"] / "tire-receipt.pdf"),
            "title": "Toyota winter tires receipt",
            "doc_type": "receipt",
        },
    )

    listing = intake_flow["call"]("list_documents", {})

    assert listing["total"] == 1
    for document in listing["items"]:
        assert "file_path" not in document
        extracted = intake_flow["call"](
            "extract_registered_document_text", {"document_id": document["id"]}
        )
        assert extracted["extracted_chars"][0] > 0
