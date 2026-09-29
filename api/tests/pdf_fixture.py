"""Build a minimal text PDF in-process, for tests.

Written by hand rather than with reportlab so the test suite gains no
dependency, and — more to the point — so no real statement ever has to
live in the repo to exercise the extractor. Every fixture is generated
from invented numbers at test time.
"""

from __future__ import annotations

import io


def make_pdf(pages: list[str]) -> bytes:
    """A PDF with one page per entry, each rendering its text in Helvetica."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    content_ids = []
    for text in pages:
        stream = b"BT /F1 11 Tf 1 0 0 1 40 750 Tm 14 TL\n"
        for line in text.split("\n"):
            escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            stream += b"(" + escaped.encode("latin-1", "replace") + b") Tj T*\n"
        stream += b"ET"
        content_ids.append(
            add(
                b"<< /Length "
                + str(len(stream)).encode()
                + b" >>\nstream\n"
                + stream
                + b"\nendstream"
            )
        )

    pages_id = len(objects) + len(pages) + 1
    page_ids = [
        add(
            b"<< /Type /Page /Parent "
            + str(pages_id).encode()
            + b" 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 "
            + str(font_id).encode()
            + b" 0 R >> >> /Contents "
            + str(content_id).encode()
            + b" 0 R >>"
        )
        for content_id in content_ids
    ]

    kids = b" ".join(str(page_id).encode() + b" 0 R" for page_id in page_ids)
    add(
        b"<< /Type /Pages /Kids ["
        + kids
        + b"] /Count "
        + str(len(page_ids)).encode()
        + b" >>"
    )
    catalog_id = add(b"<< /Type /Catalog /Pages " + str(pages_id).encode() + b" 0 R >>")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n")

    xref_at = out.tell()
    out.write(b"xref\n0 " + str(len(objects) + 1).encode() + b"\n")
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root "
        + str(catalog_id).encode()
        + b" 0 R >>\nstartxref\n"
        + str(xref_at).encode()
        + b"\n%%EOF\n"
    )
    return out.getvalue()


def make_imageless_pdf() -> bytes:
    """A structurally valid PDF whose page carries no text at all.

    Stands in for a scanned statement: pypdf extracts nothing from it, which
    is the case that must be reported as an error rather than as a clean
    document with empty text.
    """
    return make_pdf([""])
