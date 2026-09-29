"""Bank A .xlsx and Bank B headerless CSV activity exports (ticket T-28),
and the named refusal for unreadable formats (ticket T-09).

Invented rows and invented layouts only.
"""

import csv
import io
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from api.personal_redaction import (PersonalRedactionError, PrivacyProfile,
                                    scrub_document)
from api.redaction import assert_no_account_numbers
from api.spreadsheet import UnsupportedDocumentError, xlsx_to_csv_text

AUTH = {"Authorization": "Bearer test-token"}
BANK_A_HEADER = ["Tag", "Posted", "Details", "More details",
                 "Entry type", "Value", "Balance"]


@pytest.fixture
def profile():
    return PrivacyProfile(("Alex Example", "Alex Q Example"),
                          ("123 Example Lane", "Unit 4", "Fictiontown XY A1A 1A1"))


def _col(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def make_xlsx(path: Path, rows, *, sheets=1, formula=False, merged=False,
              doctype=False, bool_cell=False) -> Path:
    """A minimal single-sheet workbook: strings shared, numbers numeric."""
    shared: list[str] = []
    sheet_rows = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row):
            ref = f"{_col(c)}{r}"
            if isinstance(value, (int, float)):
                cells.append(f'<c r="{ref}"><v>{value!r}</v></c>')
            elif value is None:
                continue
            else:
                shared.append(value)
                cells.append(f'<c r="{ref}" t="s"><v>{len(shared) - 1}</v></c>')
        if formula and r == 2:
            cells.append(f'<c r="{_col(len(row))}{r}"><f>SUM(F2:F3)</f><v>1</v></c>')
        if bool_cell and r == 2:
            cells.append(f'<c r="{_col(len(row))}{r}" t="b"><v>1</v></c>')
        sheet_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    head = '<!DOCTYPE x [<!ENTITY a "b">]>' if doctype else ""
    sheet = (f'<?xml version="1.0" encoding="UTF-8"?>{head}<worksheet {ns}><sheetData>'
             f'{"".join(sheet_rows)}</sheetData>'
             f'{"<mergeCells><mergeCell ref=\"A1:B1\"/></mergeCells>" if merged else ""}</worksheet>')
    strings = (f'<?xml version="1.0" encoding="UTF-8"?><sst {ns}>'
               + "".join(f"<si><t>{escape(s)}</t></si>" for s in shared) + "</sst>")
    sheet_entries = "".join(f'<sheet name="S{i}" sheetId="{i}"/>' for i in range(1, sheets + 1))
    workbook = f'<?xml version="1.0" encoding="UTF-8"?><workbook {ns}><sheets>{sheet_entries}</sheets></workbook>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/sharedStrings.xml", strings)
        for i in range(1, sheets + 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", sheet)
    return path


def bank_a_rows():
    return [
        list(BANK_A_HEADER),
        ["Chequing 1234567890123 Invented", "2026-09-19", "PAY DEPOSIT", "INVENTED EMPLOYER LTD",
         "Credit", 1234.56, 3000.01],
        ["", "2026-09-20", "Withdrawal", "EXAMPLE TRANSFER OUT Alex Example", "Debit", -37.29, 2962.72],
        ["", "2026-09-21", "ONLINE PAYMENT TO CARD", "", "Debit", -1000, 1954.95],
    ]


# ---------------------------------------------------------------- xlsx reader

def test_xlsx_reads_values_as_csv_with_shortest_numbers(tmp_path):
    text = xlsx_to_csv_text(make_xlsx(tmp_path / "s.xlsx", bank_a_rows()))
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == BANK_A_HEADER
    assert rows[2][5] == "-37.29" and rows[3][5] == "-1000" and rows[1][6] == "3000.01"
    assert len({len(r) for r in rows}) == 1


def test_xlsx_float_artifacts_render_as_shortest_round_trip(tmp_path):
    path = make_xlsx(tmp_path / "s.xlsx", [["Amount"], ["PLACEHOLDER"]])
    with zipfile.ZipFile(path) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    parts["xl/worksheets/sheet1.xml"] = parts["xl/worksheets/sheet1.xml"].replace(
        b'<c r="A2" t="s"><v>1</v></c>', b'<c r="A2"><v>-37.289999999999999</v></c>')
    with zipfile.ZipFile(path, "w") as z:
        for name, data in parts.items():
            z.writestr(name, data)
    assert xlsx_to_csv_text(path).splitlines()[1] == "-37.29"


@pytest.mark.parametrize("kwargs, message", [
    ({"sheets": 2}, "exactly one sheet"),
    ({"formula": True}, "formulas"),
    ({"merged": True}, "merged"),
    ({"doctype": True}, "declarations"),
    ({"bool_cell": True}, "cell type"),
])
def test_xlsx_refuses_shapes_outside_the_reviewed_export(tmp_path, kwargs, message):
    path = make_xlsx(tmp_path / "s.xlsx", bank_a_rows(), **kwargs)
    with pytest.raises(UnsupportedDocumentError, match=message) as exc:
        xlsx_to_csv_text(path)
    assert "Invented" not in str(exc.value)


def test_xlsx_refuses_non_zip_and_utf16_parts(tmp_path):
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(b"not a zip")
    with pytest.raises(UnsupportedDocumentError):
        xlsx_to_csv_text(bad)
    path = make_xlsx(tmp_path / "s.xlsx", bank_a_rows())
    with zipfile.ZipFile(path) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    parts["xl/workbook.xml"] = parts["xl/workbook.xml"].decode().encode("utf-16")
    with zipfile.ZipFile(path, "w") as z:
        for name, data in parts.items():
            z.writestr(name, data)
    with pytest.raises(UnsupportedDocumentError, match="UTF-8"):
        xlsx_to_csv_text(path)


# ------------------------------------------------------------ Bank A layout

def test_bank_a_layout_withholds_tag_and_scrubs_descriptions(tmp_path, profile):
    text = xlsx_to_csv_text(make_xlsx(tmp_path / "s.xlsx", bank_a_rows()))
    result = scrub_document(text, ".xlsx", profile)
    rows = list(csv.DictReader(io.StringIO(result.text)))
    assert [r["Tag"] for r in rows] == ["[REDACTED]", "", ""]
    assert "1234567890123" not in result.text and "Invented" not in result.text.split("\n", 1)[1][:40]
    assert "Alex Example" not in result.text
    assert rows[0]["Details"] == "PAY DEPOSIT" and rows[0]["Entry type"] == "Credit"
    assert rows[1]["Value"] == "-37.29" and rows[1]["Posted"] == "2026-09-20"
    assert rows[2]["Details"] == "ONLINE PAYMENT TO CARD"
    assert_no_account_numbers(result.text)


@pytest.mark.parametrize("mutate", [
    lambda rows: rows[1].__setitem__(4, "Pending"),
    lambda rows: rows[1].__setitem__(1, "09/19/2026"),
    lambda rows: rows[1].__setitem__(5, "lots"),
    lambda rows: rows[0].__setitem__(3, "More-details"),
    lambda rows: [r.append("x") for r in rows],
])
def test_bank_a_near_misses_are_refused(tmp_path, profile, mutate):
    rows = bank_a_rows()
    mutate(rows)
    text = xlsx_to_csv_text(make_xlsx(tmp_path / "s.xlsx", rows))
    with pytest.raises(PersonalRedactionError):
        scrub_document(text, ".xlsx", profile)


# ---------------------------------------------------------------- Bank B layout

@pytest.mark.parametrize("date_a, date_b", [("09/19/2026", "09/20/2026"),
                                            ("2026-09-19", "2026-09-20")])
def test_bank_b_headerless_activity_gets_a_header_and_scrubbed_description(profile, date_a, date_b):
    source = (f"{date_a},INVENTED GROCER 4411,37.29,,1234.56\n"
              f"{date_b},PAYMENT Alex Example,,300.00,934.56\n")
    result = scrub_document(source, ".csv", profile)
    rows = list(csv.reader(io.StringIO(result.text)))
    assert rows[0] == ["Date", "Description", "Debit", "Credit", "Balance"]
    assert rows[1] == [date_a, "INVENTED GROCER 4411", "37.29", "", "1234.56"]
    assert rows[2][3] == "300.00" and "Alex Example" not in result.text
    assert_no_account_numbers(result.text)


@pytest.mark.parametrize("source", [
    "09/19/2026,MIXED,1.00,,5.00\n2026-09-20,MIXED,1.00,,4.00\n",   # two date formats
    "09/19/2026,BOTH,1.00,2.00,5.00\n",                              # debit and credit
    "09/19/2026,NEITHER,,,5.00\n",                                   # neither
    "09/19/2026,SHORT,1.00,,\n",                                     # no balance
    "09/19/2026,WIDE,1.00,,5.00,extra\n",                            # six columns
    "Date,Description,Debit,Credit,Balance\n09/19/2026,HDR,1.00,,5.00\n",  # real header: unreviewed
    "Sept 19,WORDS,1.00,,5.00\n",
])
def test_bank_b_near_misses_are_refused(profile, source):
    with pytest.raises(PersonalRedactionError):
        scrub_document(source, ".csv", profile)


def test_existing_bank_c_layout_is_unchanged(profile):
    source = ("Posted,Kind,Payee,Note,Value\n"
              "06/15/2026,DEBIT,MAPLE DONUTS,private memo,-19.99\n")
    rows = list(csv.DictReader(io.StringIO(scrub_document(source, ".csv", profile).text)))
    assert rows[0]["Note"] == "[REDACTED]" and rows[0]["Payee"] == "MAPLE DONUTS"


# ----------------------------------------------------------------------- API

def _intake(test_settings) -> Path:
    root = Path(test_settings.intake_dir)
    root.mkdir(exist_ok=True)
    return root


def test_api_extracts_bank_a_xlsx(client, test_settings):
    make_xlsx(_intake(test_settings) / "bank-a.xlsx", bank_a_rows())
    response = client.post("/documents/extract", json={"path": "bank-a.xlsx"}, headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pages"] == 1 and "1234567890123" not in body["text"]
    assert "PAY DEPOSIT,INVENTED EMPLOYER LTD,Credit,1234.56" in body["text"]


def test_api_extracts_bank_b_csv(client, test_settings):
    (_intake(test_settings) / "activity-export.csv").write_text(
        "2026-09-19,INTEREST,12.34,,8000.00\n2026-09-20,PAYMENT,,55.50,7944.50\n")
    response = client.post("/documents/extract", json={"path": "activity-export.csv"}, headers=AUTH)
    assert response.status_code == 200, response.text
    assert response.json()["text"].startswith("Date,Description,Debit,Credit,Balance\n")


@pytest.mark.parametrize("name, payload", [
    ("notes.docx", b"PK\x03\x04 invented"),
    ("fake.pdf", b"this is not a pdf, Alex Example"),
    ("broken.xlsx", b"not a zip at all"),
])
def test_api_unreadable_formats_are_named_refusals_not_500(client, test_settings, name, payload):
    (_intake(test_settings) / name).write_bytes(payload)
    response = client.post("/documents/extract", json={"path": name}, headers=AUTH)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "unsupported_document_format"
    assert "Alex" not in response.text and "invented" not in response.text


def test_api_unreviewed_xlsx_layout_is_withheld(client, test_settings):
    make_xlsx(_intake(test_settings) / "other.xlsx", [["Who", "What"], ["Alex Example", "secret"]])
    response = client.post("/documents/extract", json={"path": "other.xlsx"}, headers=AUTH)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "personal_redaction_required"
    assert "secret" not in response.text


def test_bank_a_blank_balance_is_accepted_but_a_word_is_not(tmp_path, profile):
    rows = bank_a_rows()
    rows[2][6] = ""
    text = xlsx_to_csv_text(make_xlsx(tmp_path / "s.xlsx", rows))
    parsed = list(csv.DictReader(io.StringIO(scrub_document(text, ".xlsx", profile).text)))
    assert parsed[1]["Balance"] == "" and parsed[1]["Value"] == "-37.29"
    rows[2][6] = "pending"
    text = xlsx_to_csv_text(make_xlsx(tmp_path / "t.xlsx", rows))
    with pytest.raises(PersonalRedactionError):
        scrub_document(text, ".xlsx", profile)
