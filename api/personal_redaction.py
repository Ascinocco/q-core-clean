"""Deterministic, local-only personal-data redaction before model exposure.

This is NOT general named-entity recognition. A reviewed private profile covers
unlabelled household names/address variants. Recognizable personal fields and
postal blocks supplement it; ambiguous address sections are withheld. Unknown
unlabelled names cannot be distinguished reliably from merchants by regex.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from api.redaction import REDACTED, ScrubResult, scrub_text
from api.db import refuse_real_path_under_pytest


class PersonalRedactionError(Exception):
    """Fixed messages only: exceptions must never quote input/profile values."""


@dataclass(frozen=True, repr=False)
class PrivacyProfile:
    names: tuple[str, ...]
    addresses: tuple[str, ...]


def load_profile(path: str) -> PrivacyProfile:
    try:
        source = Path(path)
        refuse_real_path_under_pytest(source, what='the private redaction profile')
        # The profile itself is more sensitive than the output. Fail closed
        # rather than reading a world-readable or unreviewed configuration.
        if source.is_symlink() or source.stat().st_mode & 0o077:
            raise ValueError
        value = json.loads(source.read_text())
        if set(value) != {'version', 'reviewed', 'names', 'addresses'}:
            raise ValueError
        if value['version'] != 1 or value['reviewed'] is not True:
            raise ValueError
        for field in ('names', 'addresses'):
            items = value[field]
            if not isinstance(items, list) or not items or len(items) > 100:
                raise ValueError
            if any(not isinstance(v, str) or not 3 <= len(v.strip()) <= 300 or
                   not re.search(r'[^\W\d_]', v) or REDACTED in v for v in items):
                raise ValueError
        return PrivacyProfile(tuple(value['names']), tuple(value['addresses']))
    except Exception:
        raise PersonalRedactionError(
            'Document privacy profile is missing, invalid, unreviewed or not private; '
            'configure it locally using runbooks/document-privacy.md. Text withheld.'
        ) from None


def _normalize(text):
    return unicodedata.normalize('NFKC', text).replace('\u200b', '').replace('\ufeff', '')


def _literal(value):
    # Case, repeated whitespace, line wraps and punctuation must not make a
    # known identity reappear. Aliases/abbreviations still require explicit
    # profile entries; matching "Ann" must not remove "Annual".
    words = re.findall(r'[^\W_]+', _normalize(value), flags=re.UNICODE)
    return re.compile(r'(?<!\w)' + r'[\W_]*'.join(map(re.escape, words)) + r'(?!\w)', re.I)


_NAME_LABEL = re.compile(
    r'^[ \t]*(?:account[ \t]*holder(?:[ \t]+name)?|card[ \t]*holder(?:[ \t]+name)?|'
    r'customer[ \t]+name|full[ \t]+name|name(?=[ \t]*:))(?!\w)[ \t]*:?[ \t]*(.*)$', re.I)
_ADDRESS_LABEL = re.compile(
    r'^[ \t]*(?:(?:mailing|billing|residential|home|shipping|postal)[ \t]+)?address(?!\w)[ \t]*:?[ \t]*(.*)$', re.I)
_STREET = re.compile(
    r'^\s*(?:(?:apt|unit|suite)\.?\s+[\w-]+[,\s]+)?(?:\d+[A-Za-z]?(?:-\d+)?\s+.+?\b'
    r'(?:street|st|road|rd|avenue|ave|drive|dr|lane|ln|court|ct|crescent|cres|'
    r'boulevard|blvd|way|place|pl|terrace|trail|parkway|pkwy|highway|hwy)\b|'
    r'p\.?\s*o\.?\s+box\s+\d+)', re.I)
_POSTAL = re.compile(r'\b[A-Z]\d[A-Z][\s-]*\d[A-Z]\d\b|\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b', re.I)
_EMAIL = re.compile(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b')
_URL = re.compile(r'https?://[^\s<>"\']+', re.I)
_FINANCIAL = re.compile(r'\d[.,]\d{2}\b|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}')


def _name_like(value):
    return bool(re.fullmatch(r"[^\W\d_]+(?:[ '\u2019.-]+[^\W\d_]+){1,6}", value.strip()))


def _address_blocks(text, *, defer_incomplete=False):
    """Recognize blocks before literal masking can erase their street anchor."""
    lines, names, count = text.split('\n'), [], 0
    consumed = set()
    for index, line in enumerate(lines):
        label = _ADDRESS_LABEL.match(line)
        wrapped = line.strip().isdigit() and _STREET.match(' '.join(lines[index:index + 8]))
        if index in consumed or not (_STREET.match(line) or label or wrapped):
            continue
        if label and (label[1].strip() == REDACTED or (
                not label[1].strip() and index + 1 < len(lines) and lines[index + 1].strip() == REDACTED)):
            continue
        end = next((j for j in range(index, min(len(lines), index + 20))
                    if _POSTAL.search(' '.join(lines[index:j + 1]))), None)
        if end is None or any(_FINANCIAL.search(lines[j]) for j in range(index, end + 1)):
            if defer_incomplete:
                continue  # explicit profile entries may safely cover these
            raise PersonalRedactionError('Ambiguous address section; text withheld for local review.')
        start = index
        for previous in range(index - 1, max(-1, index - 3), -1):
            if _name_like(lines[previous]) and not _NAME_LABEL.match(lines[previous]):
                start = previous
                names.append(lines[previous].strip())
            else:
                break
        for j in range(start, end + 1):
            lines[j] = REDACTED
            consumed.add(j)
            count += 1
    return '\n'.join(lines), names, count


def scrub_personal(text: str, profile: PrivacyProfile) -> ScrubResult:
    text = _normalize(text)
    # Printed browser headers can include account keys or session tokens.
    # Drop the whole URL if it has a query/fragment: keeping a masked tail
    # still leaks unnecessary identifiers, and these URLs are not merchants.
    url_count = 0
    def redact_url(match):
        nonlocal url_count
        if '?' in match[0] or '#' in match[0]:
            url_count += 1
            return REDACTED
        return match[0]
    text = _URL.sub(redact_url, text)
    names = list(profile.names)
    lines = text.split('\n')
    # Learn explicitly labelled names across the whole document so subsequent
    # page headers get the same treatment, without persisting discovered PII.
    for index, line in enumerate(lines):
        match = _NAME_LABEL.match(line)
        if not match:
            continue
        value = match[1].strip()
        if not value:
            if index + 1 >= len(lines) or not _name_like(lines[index + 1]):
                raise PersonalRedactionError('Ambiguous personal name field; text withheld for local review.')
            value = lines[index + 1].strip()
        if value == REDACTED:
            continue
        if not _name_like(value):
            raise PersonalRedactionError('Ambiguous personal name field; text withheld for local review.')
        names.append(value)

    # First remove recognizable complete blocks in the original layout. If
    # an exact profile entry masks just a street first, its city/postal lines
    # otherwise become detached and cannot be safely associated afterwards.
    text, discovered, count = _address_blocks(text, defer_incomplete=True)
    names.extend(discovered)
    count += url_count
    for value in sorted(set(names + list(profile.addresses)), key=lambda x: (-len(x), x)):
        text, removed = _literal(value).subn(REDACTED, text)
        count += removed
    text, removed = _EMAIL.subn(REDACTED, text)
    count += removed
    text, discovered, removed = _address_blocks(text)
    names.extend(discovered)
    count += removed
    for value in sorted(set(names), key=lambda x: (-len(x), x)):
        text, removed = _literal(value).subn(REDACTED, text)
        count += removed
    # A detached postal line could mean a wrapped/unsupported address block.
    # Masking only the postal code would leave the rest of the address behind.
    if _POSTAL.search(text):
        raise PersonalRedactionError('Unresolved postal address; text withheld for local review.')
    for value in names + list(profile.addresses):
        if _literal(value).search(text):
            raise PersonalRedactionError('Personal redaction post-condition failed; text withheld.')
    return ScrubResult(text, count)


_PRINT_CODE = r'BKAST\d+_(?:\d+|\[REDACTED\])_\d+'
_PRINT_VALUE = r'(?:\d+|\*{4}\d{4}|\[REDACTED\])'
_PRINT_HEADER = re.compile(
    r'^\s*#\d{2}[A-Z]\d{6}#\s+' + _PRINT_CODE +
    r'\s+-\s+' + _PRINT_VALUE + r'\s+PRN\s+'
    r'(?:-\s+|\d+\s+){1,20}' + _PRINT_CODE + r'\s+X\s+Z'
    r'(?:[ \t]*\n[ \t]*(?:\d{5,}|\*{4}\d{4}|\[REDACTED\]))?[ \t]*(?=\n|$)', re.M)
_PRINT_FOOTER = re.compile(
    r'^[ \t]*' + _PRINT_CODE + r'\s+-\s+' + _PRINT_VALUE + r'[ \t]*(?=\n|$)', re.M)


def scrub_print_metadata(text: str) -> ScrubResult:
    """Recognized bank production blocks only; never generic short-ID masking.

    Run after personal and numeric redaction: address street numbers are gone,
    and wrapped numeric identifiers have already collapsed to a single token.
    Preserve labelled account identifiers and transaction content elsewhere.
    """
    text, headers = _PRINT_HEADER.subn(REDACTED, text)
    text, footers = _PRINT_FOOTER.subn(REDACTED, text)
    # An unfamiliar version must not silently leave metadata behind or cause
    # a broad deletion through the first transaction. Ask for local review.
    if re.search(r'^[ \t]*BKAST\d+_', text, re.M):
        raise PersonalRedactionError('Unrecognized statement print metadata; text withheld for local review.')
    return ScrubResult(text, headers + footers)


#: Bank A's activity export format (read from .xlsx by api.spreadsheet).
#: `Tag` carries the account label and number on its first row, so it is
#: withheld whole, like the Note column of the Bank C CSV format.
BANK_A_ACTIVITY_HEADER = ['tag', 'posted', 'details', 'more details',
                          'entry type', 'value', 'balance']
#: Header written in front of a Bank B activity export, which has none. Data
#: records keep their positions: CSV record N is still the file's Nth row.
BANK_B_ACTIVITY_HEADER = ['Date', 'Description', 'Debit', 'Credit', 'Balance']
_DECIMAL = re.compile(r'-?\d+(\.\d+)?')
_BANK_B_DATE = (re.compile(r'\d{2}/\d{2}/\d{4}'), re.compile(r'\d{4}-\d{2}-\d{2}'))


def _is_bank_b_activity(rows: list[list[str]]) -> bool:
    """Bank B's headerless shape: date, description, debit, credit, balance.

    Every row must match, with one date format throughout and exactly one of
    debit/credit filled. Anything else is not Bank B's layout and falls through
    to the header checks, which refuse it.
    """
    if not rows or any(len(row) != 5 for row in rows):
        return False
    formats = {i for row in rows for i, p in enumerate(_BANK_B_DATE) if p.fullmatch(row[0].strip())}
    if len(formats) != 1 or not all(any(p.fullmatch(r[0].strip()) for p in _BANK_B_DATE) for r in rows):
        return False
    for _, _, debit, credit, balance in rows:
        filled = [v.strip() for v in (debit, credit) if v.strip()]
        if len(filled) != 1 or not all(_DECIMAL.fullmatch(v) for v in filled):
            return False
        if not _DECIMAL.fullmatch(balance.strip()):
            return False
    return True


def _check_bank_a_activity(rows: list[list[str]]) -> None:
    # Balance may be blank on some rows of this format (for example a row not
    # yet posted). An empty
    # cell carries nothing to redact; intake decides what the row means.
    for _, date, _, _, kind, amount, balance in rows[1:]:
        if not (re.fullmatch(r'\d{4}-\d{2}-\d{2}', date.strip()) and kind.strip() in {'Debit', 'Credit'}
                and _DECIMAL.fullmatch(amount.strip())
                and (balance.strip() == '' or _DECIMAL.fullmatch(balance.strip()))):
            raise PersonalRedactionError('Unreviewed tabular values; text withheld for local review.')


def scrub_document(text: str, suffix: str, profile: PrivacyProfile) -> ScrubResult:
    """Personal data first, then the existing account-number guard's scrubber."""
    count = 0
    if suffix.lower() in {'.csv', '.tsv', '.xlsx'}:
        # Keep cell boundaries: scrubbing joined CSV can change row shape or
        # accidentally form an account number from adjacent numeric cells.
        # An .xlsx arrives here already rendered as CSV by api.spreadsheet.
        delimiter = '\t' if suffix.lower() == '.tsv' else ','
        try:
            rows = list(csv.reader(io.StringIO(text.lstrip('\ufeff')), delimiter=delimiter, strict=True))
        except csv.Error:
            raise PersonalRedactionError('Malformed tabular input; text withheld.') from None
        if not rows or any(len(row) != len(rows[0]) for row in rows):
            raise PersonalRedactionError('Inconsistent tabular input; text withheld.')
        bank_b_activity = _is_bank_b_activity(rows)
        if bank_b_activity:
            rows.insert(0, list(BANK_B_ACTIVITY_HEADER))
        headers = rows[0]
        allowed = {'posted', 'kind', 'payee', 'note', 'value', 'amount', 'date', 'description',
                   'account holder', 'customer name', 'address', 'email', 'phone'}
        normalized = [h.strip().lower() for h in headers]
        if normalized == BANK_A_ACTIVITY_HEADER:
            _check_bank_a_activity(rows)
        elif bank_b_activity:
            pass
        elif len(set(normalized)) != len(normalized) or not set(normalized) <= allowed:
            raise PersonalRedactionError('Unreviewed tabular columns; text withheld for local review.')
        if 'payee' in normalized and set(normalized) != {'posted', 'kind', 'payee', 'note', 'value'}:
            raise PersonalRedactionError('Ambiguous tabular Payee column; text withheld for local review.')
        sensitive = {'note', 'account holder', 'customer name', 'address', 'email', 'phone', 'tag'}
        for row in rows[1:]:
            for index, cell in enumerate(row):
                if normalized[index] in sensitive:
                    row[index] = REDACTED if cell.strip() else cell
                    count += bool(cell.strip())
                else:
                    result = scrub_personal(cell, profile)
                    numeric = scrub_text(result.text)
                    row[index] = numeric.text
                    count += result.redactions + numeric.redactions
        stream = io.StringIO()
        csv.writer(stream, delimiter=delimiter, lineterminator='\n').writerows(rows)
        return ScrubResult(stream.getvalue(), count)
    personal = scrub_personal(text, profile)
    numeric = scrub_text(personal.text)
    metadata = scrub_print_metadata(numeric.text)
    return ScrubResult(metadata.text, personal.redactions + metadata.redactions + numeric.redactions)


def main():
    """Local setup only. Never print profile values or accept them as arguments."""
    import argparse
    from api.config import REPO_ROOT

    parser = argparse.ArgumentParser(description='Create/check a private document-redaction profile locally.')
    parser.add_argument('command', choices=('init', 'check'))
    parser.add_argument('--path', default=str(REPO_ROOT / 'data/privacy/redaction.json'))
    args = parser.parse_args()
    try:
        if args.command == 'init':
            path = Path(args.path)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump({'version': 1, 'reviewed': False, 'names': [], 'addresses': []}, stream, indent=2)
                stream.write('\n')
            print('Created private unreviewed template. Edit locally; do not paste personal values into chat.')
        else:
            load_profile(args.path)
            print('Profile structure, review flag and file permissions passed. This is not a source-content privacy audit.')
    except Exception:
        raise SystemExit('Profile setup/check refused. Check file permissions, review flag, nonempty names/addresses and unused init path locally.') from None


if __name__ == '__main__':
    main()
