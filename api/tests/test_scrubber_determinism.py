"""Scrubber output must be byte-identical across runs (ticket T-24).

WHY THIS IS NOT A NICETY. `description` is part of the account-scoped
de-duplication key `(account_id, txn_date, description, amount_cents)`,
and `description` comes from scrubbed text. A scrubber whose output
varied between runs would defeat de-duplication silently: every
re-import would create duplicates and nothing would report a problem.

Determinism is a precondition of re-import de-duplication, not a detail.

WHAT THIS FILE DELIBERATELY DOES NOT ASSERT. The ticket originally asked
that two documents differing only in a card number scrub to *identical*
output, on the theory that a redaction marker carrying per-input residue
would be a bug. That was wrong, and it is inverted below. The scrubber
preserves the last four digits on purpose -- that is what makes an
account recognisable, and how find-or-create matches an account entity
(D16). Demanding identical output would push someone to "fix"
determinism by destroying account identity.

Per-INPUT variation is correct and necessary. Per-RUN variation is the
risk. Those are different properties and only the second is a bug.
"""

from __future__ import annotations

import hashlib
import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.config import REPO_ROOT
from api.tests.pdf_fixture import make_pdf

# Invented numbers, never a real one: a card, a long account run, and a
# separator-joined run, so more than one redaction rule is exercised.
STATEMENT_TEXT = (
    "EXAMPLE BANK CARD STATEMENT\n"
    "Card 4111111111111111\n"
    "Account 123456789012\n"
    "Ref 987 654 3210\n"
    "03/12/2026 ZORBLAX COFFEE #4821  -19.99\n"
)
OTHER_CARD_TEXT = STATEMENT_TEXT.replace("4111111111111111", "5500005555555559")


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


def _extract(client, settings, path: str) -> dict:
    response = client.post(
        "/documents/extract",
        json={"path": path},
        headers={"Authorization": f"Bearer {settings.api_token}"},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("kind", ["pdf", "csv"])
def test_extracting_the_same_document_twice_is_byte_identical(
    client_with_intake, intake, kind
):
    """Text, redaction count and per-page extracted_chars, all three."""
    client, settings = client_with_intake
    if kind == "pdf":
        (intake / "s.pdf").write_bytes(make_pdf([STATEMENT_TEXT, "page two 111.11"]))
        name = "s.pdf"
    else:
        # Real CSV structure: arbitrary prose with a .csv suffix is now
        # deliberately refused by the personal-data column guard.
        with (intake / "s.csv").open('w', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['date', 'description', 'amount'])
            writer.writerow(['2026-03-12', STATEMENT_TEXT, '-19.99'])
        name = "s.csv"

    first = _extract(client, settings, name)
    second = _extract(client, settings, name)

    assert first["text"] == second["text"]
    assert first["redactions"] == second["redactions"]
    assert first["extracted_chars"] == second["extracted_chars"]
    assert first["pages"] == second["pages"]


def test_scrubbing_is_stable_across_processes_and_hash_seeds():
    """Run-dependence is EXERCISED here, not assumed.

    An in-process repeat cannot see order that leaks from a set or dict
    built at import time -- the same interpreter reuses the same layout.
    A separate process under a different PYTHONHASHSEED can.
    """
    program = (
        "import sys, hashlib, json;"
        "sys.path.insert(0, %r);"
        "from api.redaction import scrub_text;"
        "r = scrub_text(%r);"
        "print(json.dumps([hashlib.sha256(r.text.encode()).hexdigest(), r.redactions]))"
        % (str(REPO_ROOT), STATEMENT_TEXT)
    )
    results = []
    for seed in ("0", "1", "12345"):
        out = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True, text=True, check=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": seed},
        )
        results.append(json.loads(out.stdout))
    assert results[0] == results[1] == results[2], (
        f"scrubber output depends on the run: {results}"
    )


def test_two_different_cards_scrub_differently_and_keep_their_last_four():
    """The INVERSE of what ticket T-24 originally asked for.

    It asked that these be identical. They must not be: the last four
    digits survive on purpose, because that is what makes an account
    recognisable and how the intake skill matches an account entity
    (D16). If this test ever fails by the two becoming equal, the fix is
    NOT to relax it -- account identification is broken.
    """
    from api.redaction import scrub_text

    one = scrub_text(STATEMENT_TEXT).text
    two = scrub_text(OTHER_CARD_TEXT).text

    assert one != two, "last4 must survive; identical output means identity is gone"
    assert "****1111" in one
    assert "****5559" in two
    # And the rest of the document is untouched by the difference.
    assert one.replace("****1111", "X") == two.replace("****5559", "X")


def test_determinism_is_not_confused_with_stability_across_inputs():
    """Guards the distinction the original ticket got wrong.

    Same input twice -> identical. Different input -> different. A test
    suite that asserted only the first could be satisfied by a scrubber
    that returned a constant.
    """
    from api.redaction import scrub_text

    digest = lambda text: hashlib.sha256(scrub_text(text).text.encode()).hexdigest()
    assert digest(STATEMENT_TEXT) == digest(STATEMENT_TEXT)
    assert digest(STATEMENT_TEXT) != digest(OTHER_CARD_TEXT)
