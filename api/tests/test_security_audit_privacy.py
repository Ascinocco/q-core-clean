"""Synthetic proof for the 2026 privacy/security audit.

No fixture is copied from a real statement. Each secret is a deliberately
invented regression token and every assertion checks both privacy and utility.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.redaction import assert_no_account_numbers
from api.tests.pdf_fixture import make_pdf


FIXTURES = Path(__file__).parent / "fixtures" / "sensitive"
FAKE_SECRETS = (
    "12345-678-1234567",
    "1234567",
    "987654321012345",
    "4532015112830366",
    "411111",
    "378282246310005",
    "123-45-6789",
    "1234 5678 9012 3456",
    "Alex Example",
    "123 Example Lane",
    "alex.example@example.test",
)


def _headers(settings):
    return {"Authorization": f"Bearer {settings.api_token}"}


@pytest.mark.parametrize(
    ("fixture", "merchant"),
    [
        ("bank-c.csv", "SAFE GROCERY"),
        ("bank-a.txt", "SAFE FUEL"),
        ("bank-b-card.txt", "SAFE SHOP"),
        ("bank-b-printout.txt", "SAFE HARDWARE"),
    ],
)
def test_synthetic_sources_cross_the_real_extraction_boundary_without_leaks(
    client, test_settings, fixture, merchant
):
    intake = Path(test_settings.intake_dir)
    intake.mkdir()
    source = (FIXTURES / fixture).read_text()
    target = intake / (fixture if fixture.endswith(".csv") else fixture[:-4] + ".pdf")
    target.write_text(source) if fixture.endswith(".csv") else target.write_bytes(
        make_pdf([source])
    )

    response = client.post(
        "/documents/extract",
        json={"path": target.name},
        headers=_headers(test_settings),
    )

    assert response.status_code == 200, response.text
    text = response.json()["text"]
    assert_no_account_numbers(text)
    assert merchant in text
    assert "2026-09-" in text
    for secret in FAKE_SECRETS:
        assert secret not in response.text


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/entities", {"type": "project", "name": "Private 4532015112830366"}),
        (
            "/source_imports/preview",
            {
                "source_id": "a" * 64,
                "account_id": "missing",
                "period_start": "2026-09-01",
                "period_end": "2026-09-30",
                "actor": "audit",
                "transactions": [
                    {
                        "source_row": "line:1",
                        "txn_date": "2026-09-01",
                        "description": "PAYMENT 4532015112830366",
                        "amount_cents": -100,
                    }
                ],
            },
        ),
    ],
)
def test_model_visible_stores_refuse_account_numbers_without_echoing_them(
    client, test_settings, path, body, caplog
):
    secret = "4532015112830366"

    response = client.post(path, json=body, headers=_headers(test_settings))

    assert response.status_code == 422, response.text
    assert secret not in response.text
    assert secret not in caplog.text
    assert "masked last four" in response.text


def test_source_locators_allow_content_hashes_but_not_unstructured_numbers(
    client, test_settings
):
    headers = _headers(test_settings)
    base = {
        "source_id": "a" * 64,
        "account_id": "missing",
        "period_start": "2026-09-01",
        "period_end": "2026-09-30",
        "actor": "audit",
        "transactions": [
            {
                "source_row": f"text-v1:{'1234567a' * 8}:10:20",
                "txn_date": "2026-09-01",
                "description": "SAFE MERCHANT",
                "amount_cents": -100,
            }
        ],
    }

    structured = client.post("/source_imports/preview", json=base, headers=headers)
    secret = "4532015112830366"
    unstructured = client.post(
        "/source_imports/preview",
        json={
            **base,
            "transactions": [{**base["transactions"][0], "source_row": secret}],
        },
        headers=headers,
    )

    # The structured request reaches account validation; the locator itself is
    # valid. An unstructured number is rejected and never echoed.
    assert structured.status_code == 400, structured.text
    assert unstructured.status_code == 422, unstructured.text
    assert secret not in unstructured.text


def test_document_metadata_never_returns_the_raw_path(client, test_settings):
    inbox = Path(test_settings.inbox_dir)
    inbox.mkdir()
    (inbox / "safe.pdf").write_bytes(make_pdf(["SAFE MERCHANT 12.34"]))

    created = client.post(
        "/documents",
        json={"file_path": "safe.pdf", "title": "Safe document"},
        headers=_headers(test_settings),
    ).json()
    fetched = client.get(
        f"/documents/{created['id']}", headers=_headers(test_settings)
    ).json()
    listed = client.get("/documents", headers=_headers(test_settings)).json()

    assert "file_path" not in created
    assert "file_path" not in fetched
    assert all("file_path" not in item for item in listed["items"])


def test_entity_attributes_and_jyra_descriptions_use_the_same_store_guard(
    client, test_settings
):
    headers = _headers(test_settings)
    secret = "4532015112830366"
    unsafe_entity = client.post(
        "/entities",
        json={
            "type": "project",
            "name": "Safe project",
            "attributes": {"description": f"account {secret}"},
        },
        headers=headers,
    )
    entity = client.post(
        "/entities",
        json={"type": "project", "name": "Safe project"},
        headers=headers,
    ).json()
    board = client.post(
        "/boards",
        json={"entity_id": entity["id"], "title": "Safe board"},
        headers=headers,
    ).json()
    unsafe_ticket = client.post(
        "/tickets",
        json={
            "board_id": board["id"],
            "type": "task",
            "title": "Safe title",
            "description": f"account {secret}",
            "actor": "audit",
        },
        headers=headers,
    )

    assert unsafe_entity.status_code == 422, unsafe_entity.text
    assert unsafe_ticket.status_code == 422, unsafe_ticket.text
    assert secret not in unsafe_entity.text
    assert secret not in unsafe_ticket.text


def test_intake_skills_require_server_side_extraction_and_forbid_raw_reads():
    root = Path(__file__).parents[2]
    for relative in (
        "plugin/skills/statement-intake/SKILL.md",
        "plugin/skills/document-intake/SKILL.md",
    ):
        text = (root / relative).read_text().lower()
        assert "never open" in text
        assert "extract_document_text" in text
        assert "server-side" in text
    email = (root / "plugin/skills/email-triage/SKILL.md").read_text().lower()
    assert "never read an attachment's contents" in email
    assert "document-intake" in email


def test_google_status_withholds_provider_ids_and_account_email(
    client, test_settings
):
    import sqlite3

    provider_id = "private-calendar-id@group.calendar.google.com"
    account_email = "private-owner@example.test"
    connection = sqlite3.connect(test_settings.db_path)
    connection.execute(
        "INSERT INTO google_calendar_connection "
        "(id, calendar_id, account_email, keychain_service) VALUES (1, ?, ?, ?)",
        (provider_id, account_email, "synthetic-keychain"),
    )
    connection.commit()
    connection.close()

    response = client.get(
        "/integrations/google/status", headers=_headers(test_settings)
    )

    assert response.status_code == 200, response.text
    assert response.json()["connected"] is True
    assert response.json()["calendar"] == "q-core Reminders"
    assert provider_id not in response.text
    assert account_email not in response.text


def test_fixture_manifest_certifies_invented_inputs():
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    assert manifest["invented_only"] is True
    assert sorted(manifest["sources"]) == sorted(
        path.name for path in FIXTURES.iterdir() if path.suffix in {".csv", ".txt"}
    )
