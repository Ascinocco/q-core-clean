"""Read a bank's single-sheet .xlsx export as CSV text, stdlib only.

Some banks offer activity downloads only as Excel. This reader exists so
such an export can pass through the same tabular scrubber as a
CSV (`api.personal_redaction.scrub_document`), with cell boundaries intact.
It deliberately understands a narrow shape and refuses everything else,
rather than being a general spreadsheet library:

- exactly one worksheet;
- stored values only: a formula, error cell or merged range is refused;
- no DTD/entity declarations in any XML part (entity-expansion defence);
- bounded uncompressed size (zip-bomb defence).

Numbers are rendered in their shortest round-trip form, so a stored
`-45.060000000000002` reads as `-45.06` and a whole `3000.0` as `3000`.
Dates are not interpreted: a bank export that stores dates as serial
numbers reaches the layout check as a number and is refused there.

Refusals carry fixed messages only, never cell content.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from pathlib import Path

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_T = "{%s}t" % _NS["m"]
#: Generous for a bank activity export (the real ones are a few kilobytes)
#: while refusing a decompression bomb before any XML is parsed.
MAX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
MAX_ROWS = 20_000
MAX_COLUMNS = 64


class UnsupportedDocumentError(Exception):
    """The file is not in a format extraction can read. Fixed messages only."""


def _column_index(ref: str) -> int:
    letters = re.match(r"([A-Z]+)\d+$", ref or "")
    if not letters:
        raise UnsupportedDocumentError("Spreadsheet cell reference is not recognized.")
    index = 0
    for char in letters.group(1):
        index = index * 26 + (ord(char) - 64)
    return index - 1


def _parse(archive: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        data = archive.read(name)
    except KeyError:
        raise UnsupportedDocumentError("Spreadsheet is missing a required part.") from None
    # Checked on the raw bytes, so the check must see the real encoding:
    # a UTF-16 part would hide `<!DOCTYPE` from a byte search, hence the NUL
    # and BOM refusals. Without a DTD there are no entities to expand or
    # resolve, which is what XXE and billion-laughs both need.
    if data.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in data:
        raise UnsupportedDocumentError("Spreadsheet XML must be UTF-8.")
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise UnsupportedDocumentError("Spreadsheet contains XML declarations that are not allowed.")
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        raise UnsupportedDocumentError("Spreadsheet XML could not be parsed.") from None


def _number(text: str) -> str:
    try:
        value = Decimal(repr(float(text)))
    except (ValueError, InvalidOperation):
        raise UnsupportedDocumentError("Spreadsheet numeric cell is not a number.") from None
    if value == value.to_integral_value():
        return str(int(value))
    return format(value.normalize(), "f")


def xlsx_to_csv_text(path: Path) -> str:
    """The single worksheet as CSV text, one CSV record per spreadsheet row."""
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise UnsupportedDocumentError("File is not a readable .xlsx workbook.") from None
    with archive:
        if sum(info.file_size for info in archive.infolist()) > MAX_UNCOMPRESSED_BYTES:
            raise UnsupportedDocumentError("Spreadsheet is too large to extract.")
        workbook = _parse(archive, "xl/workbook.xml")
        if len(workbook.findall("m:sheets/m:sheet", _NS)) != 1:
            raise UnsupportedDocumentError("Spreadsheet must contain exactly one sheet.")
        sheets = [n for n in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)]
        if len(sheets) != 1:
            raise UnsupportedDocumentError("Spreadsheet must contain exactly one sheet.")

        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            for item in _parse(archive, "xl/sharedStrings.xml").findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in item.iter(_T)))

        sheet = _parse(archive, sheets[0])
        if sheet.find("m:mergeCells", _NS) is not None:
            raise UnsupportedDocumentError("Spreadsheet merged cells are not supported.")

        rows: list[list[str]] = []
        for row in sheet.findall("m:sheetData/m:row", _NS):
            if len(rows) >= MAX_ROWS:
                raise UnsupportedDocumentError("Spreadsheet has too many rows.")
            cells: dict[int, str] = {}
            for cell in row.findall("m:c", _NS):
                if cell.find("m:f", _NS) is not None:
                    raise UnsupportedDocumentError("Spreadsheet formulas are not supported.")
                kind = cell.get("t", "n")
                stored = cell.find("m:v", _NS)
                if kind == "inlineStr":
                    value = "".join(t.text or "" for t in cell.iter(_T))
                elif stored is None or stored.text is None:
                    value = ""
                elif kind == "s":
                    try:
                        value = shared[int(stored.text)]
                    except (ValueError, IndexError):
                        raise UnsupportedDocumentError("Spreadsheet string reference is invalid.") from None
                elif kind == "n":
                    value = _number(stored.text)
                elif kind == "str":
                    value = stored.text
                else:
                    # b (boolean), e (error), d (ISO date) never appear in the
                    # reviewed bank exports; an unexpected type is a refusal.
                    raise UnsupportedDocumentError("Spreadsheet cell type is not supported.")
                column = _column_index(cell.get("r", ""))
                if column >= MAX_COLUMNS:
                    raise UnsupportedDocumentError("Spreadsheet has too many columns.")
                cells[column] = value
            width = max(cells) + 1 if cells else 0
            rows.append([cells.get(i, "") for i in range(width)])

    width = max((len(r) for r in rows), default=0)
    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator="\n")
    for row in rows:
        writer.writerow(row + [""] * (width - len(row)))
    return stream.getvalue()
