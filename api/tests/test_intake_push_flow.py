"""A folder pushed from another machine goes through the real import (split, part 3).

The push crosses the helper, a stand-in `ssh` that runs the remote command
the way sshd would, and the real `python -m api.intake_receive`. The pushed
file is then extracted by the scrubbing endpoint and imported through source
preview/commit, the same calls the statement-intake skill makes. The
statement is invented, including a card-shaped number that must never come
back unmasked.
"""
import csv
from decimal import Decimal
import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / "plugin" / "scripts" / "q-core-server.sh"
INVENTED_CARD = "4532015112830366"  # a Luhn-valid test number, not a real card
CSV_TEXT = ("Posted,Kind,Payee,Note,Value\n"
            f"09/03/2026,DEBIT,CAFE INVENTED,Card {INVENTED_CARD},-5.25\n"
            "09/04/2026,DEBIT,GROCER INVENTED,,-42.10\n")


def _push(tmp_path, settings):
    server = tmp_path / "server"
    (server / ".venv" / "bin").mkdir(parents=True)
    python = server / ".venv" / "bin" / "python"
    python.write_text(f'#!/bin/sh\ncd "{REPO}" && exec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text('#!/bin/sh\nwhile [ "$1" != "--" ]; do shift; done; shift; shift\nexec sh -c "$1"\n')
    (bin_dir / "ssh").chmod(0o755)
    local = tmp_path / "local-intake" / "example_batch"
    local.mkdir(parents=True)
    (local / "statement.csv").write_text(CSV_TEXT)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path),
           "Q_CORE_API_TOKEN": settings.api_token, "Q_CORE_INTAKE_DIR": str(settings.intake_dir)}
    return subprocess.run([str(HELPER), "--ssh", "owner@q-core", "--checkout", str(server), "push",
                           "--intake-dir", str(local.parent), "example_batch"],
                          capture_output=True, text=True, timeout=60, env=env)


def test_a_pushed_folder_is_extracted_scrubbed_and_imported(client, test_settings, tmp_path):
    auth = {"Authorization": f"Bearer {test_settings.api_token}"}
    pushed = _push(tmp_path, test_settings)
    assert pushed.returncode == 0, pushed.stderr
    assert json.loads(pushed.stdout) == {"folder": "example_batch", "files": ["statement.csv"], "count": 1,
                                         "bytes": len(CSV_TEXT.encode())}
    assert INVENTED_CARD not in pushed.stdout + pushed.stderr

    extracted = client.post("/documents/extract", json={"path": "example_batch/statement.csv"}, headers=auth)
    assert extracted.status_code == 200, extracted.text
    text = extracted.json()["text"]
    assert INVENTED_CARD not in extracted.text  # the scrubber ran server-side
    assert "[REDACTED]" in text or "****0366" in text

    account = client.post("/entities", headers=auth, json={
        "type": "account", "name": "Invented Chequing ****9999",
        "attributes": {"institution": "Invented Bank", "account_subtype": "checking", "last4": "9999"}}).json()["id"]
    rows = []
    for record in csv.DictReader(io.StringIO(text)):
        month, day, year = record["Posted"].split("/")
        cents = int(round(Decimal(record["Amount"]) * 100))  # as the skill requires, never float
        rows.append({"txn_date": f"{year}-{month}-{day}", "description": record["Payee"], "amount_cents": cents,
                     "source_row": f"csv:v1:record:{len(rows) + 1}", "decision": "new",
                     "reason": "Distinct record in an invented pushed statement"})
    payload = {"account_id": account, "period_start": "2026-09-03", "period_end": "2026-09-04",
               "source_id": hashlib.sha256(text.encode()).hexdigest(), "actor": "intake-push-test",
               "transactions": rows}
    preview = client.post("/source_imports/preview", json=payload, headers=auth)
    assert preview.status_code == 200, preview.text
    committed = client.post("/source_imports/commit", json={**payload, "review_token": preview.json()["review_token"]},
                            headers=auth)
    assert committed.status_code == 200, committed.text
    listed = client.get("/transactions", params={"account_id": account}, headers=auth).json()["items"]
    assert sorted((t["description"], t["amount_cents"]) for t in listed) == [
        ("CAFE INVENTED", -525), ("GROCER INVENTED", -4210)]
