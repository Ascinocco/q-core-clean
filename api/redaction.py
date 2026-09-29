"""Remove account, routing and card numbers from extracted document text.

CLAUDE.md's rule is that full account/routing numbers are never sent to any
LLM. The subtlety this module exists for is *where* that has to happen: a
tool result is model context, so redaction has to complete server-side,
before the text is returned to anything. Redacting "at the intake-skill
level" cannot satisfy the rule if the intake skill is a model reading the
document — by then the number has already been sent.

OUTPUT DETERMINISM IS A DE-DUPLICATION PRECONDITION (ticket T-24).
`description` is part of the account-scoped dedup key
`(account_id, txn_date, description, amount_cents)`, and it comes from
this module's output. If scrubbing varied between runs -- a label-context
window sensitive to page-join whitespace, a marker carrying a per-run
value, set-iteration order leaking into the text -- then re-importing the
same statement would create duplicates and nothing would report it.
Pinned by `api/tests/test_scrubber_determinism.py`, which runs a second
pass in a separate process under a different `PYTHONHASHSEED`.

The last four digits survive on purpose and that is NOT a determinism
defect. Two different cards must scrub to different text, because `last4`
is how an account entity is recognised. Determinism means the same input
gives the same output, never that different inputs converge.

Two functions, and the pairing is the design:

- `scrub_text` removes what it recognises.
- `assert_no_account_numbers` refuses text that still contains an
  account-shaped run.

The second is the load-bearing one. A regex can only remove formats it
knows, and the failure mode of an unknown format is silent: the output
looks clean and the number is still in it. The post-condition converts that
into a loud refusal, so the worst case is a document that will not process
rather than a number that reaches a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

#: Floors, per run shape (ticket T-10).
#:
#: CONTIGUOUS runs start at seven: Canadian bank account numbers are
#: commonly seven digits, and account numbers are usually printed
#: contiguously. Over-redacting cheque and reference
#: numbers is the accepted cost — nothing downstream reads them, since the
#: aggregates use only date, description and amount.
#:
#: SEPARATOR-JOINED runs stay at nine, and that is what protects the
#: document: `2026-09-19` is eight digits joined by hyphens, and a
#: seven-digit floor applied to separated runs would eat every date in
#: every statement.
CONTIGUOUS_MIN_DIGITS = 7
JOINED_MIN_DIGITS = 9

#: A separated 7-8 digit run beside one of these is the thing being
#: protected, not a reference, and the general separated-run floor would
#: miss it.
#: Bounded by LETTERS only, on review-1's form, and the choice of boundary
#: is load-bearing rather than stylistic.
#:
#: Unbounded, `no` matched inside NOTE, NOVEMBER, ANOTHER and KNOW, and
#: `card` inside CARDINAL and CARDIOLOGY — so label context fired on nearly
#: every statement page, silently collapsing the separator-joined floor from
#: nine to seven everywhere and undoing the per-class split this rule is
#: built on. Measured before changing it.
#:
#: `\b` fixes that too, but differs from this form on 396 of 1320 probed
#: inputs, and the difference runs the wrong way for us: `\b` treats digits
#: as word characters, so `Account1234567` is NOT label context under `\b`
#: and IS under this. pypdf routinely drops the space between a label and
#: its number, so that is the common real shape rather than an edge case.
#: Lookarounds also avoid `\b`'s subtleties around `A/C` and `No.`, which
#: end on non-word characters.
#:
#: Card brands are listed explicitly, per the tech lead's decision. Before
#: boundaries existed, MASTERCARD matched only because it contains "card" —
#: an accidental behaviour that made brands inconsistent (MASTERCARD yes,
#: VISA no). Now each is true by statement.
LABEL_CONTEXT = re.compile(
    r"(?<![A-Za-z])"
    r"(account|acct|a/c|no\.?|transit|branch|routing|card"
    r"|visa|mastercard|master card|mc|amex|american express)"
    r"(?![A-Za-z])",
    re.IGNORECASE,
)

LABEL_WINDOW = 40

#: Dotted runs are scrubbed like any other joined run, so a phone number
#: written 555.555.1234 is over-redacted to ****1234. Accepted: nothing
#: downstream reads a phone number, and the alternative — a checker wider
#: than the scrubber — withholds the whole document instead.
#:
#: A hyphenated 4-2-2 run is a date, and label context must not override
#: that. Statement headers put "Account" within a few characters of the
#: period dates on nearly every page, so without this the label rule eats
#: `2026-09-19` — which the approved rule explicitly wanted kept. Applied
#: by BOTH the scrubber and the post-condition, so the two still agree.
#: A PLAUSIBLE date, not any 4-2-2 digit shape. `\d{4}-\d{2}-\d{2}` would
#: also exempt `4532-01-51`, letting a hyphenated account fragment hide
#: behind the date rule in exactly the context — beside an account label —
#: where it matters most. Year 19xx/20xx, month 01-12, day 01-31.
ISO_DATE = re.compile(r"^(19|20)\d{2}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

#: Runs at or above this keep a last-four tail; below it they go whole. A
#: seven-digit number's last four is too large a fraction of it to keep,
#: and nothing matches an account on a seven-digit tail.
TAIL_MIN_DIGITS = 10
#:
#: There is deliberately NO upper bound. An earlier version capped this at
#: sixteen, reasoning that sixteen is the longest card number — which left
#: longer runs matched by neither the scrubber nor the post-condition.
#: Demonstrated, not theorised: under the old ceiling `1234 5678 9012 3456
#: 7890` survived intact, because the whole run was too long to match and
#: each four-digit group was too short. A run longer than an account number
#: is more suspicious than one exactly the right length, not less, and
#: over-redacting a long reference costs nothing downstream.


#: Separators a document may use *inside* one number, grouped by class. A
#: run must use ONE class throughout, which is what stops independent
#: tokens merging: `84.99 2026-09-19` is a dot-run and a hyphen-run sitting
#: next to each other, four digits and eight, not one twelve-digit number.
#: Mixing the classes is how an early version of this refused every real
#: statement line it saw.
#:
#: Whitespace is a class because pypdf inserts a newline when a number
#: wraps, so `4532 0151\n1283 0366` is genuinely one account number.
#: Commas belong to no class: they only ever appear in amounts.
_SCRUB_SEPARATORS = (r"\s", r"\-", r"\.")

#: DERIVED, not restated. The post-condition must check exactly the classes
#: the scrubber scrubs, or the pair can fail in a direction worse than a
#: leak: a class the checker knows and the scrubber does not means every
#: document containing that shape is withheld forever, with a deliberately
#: unquoted reason. For example, if the checker knows dotted runs and the
#: scrubber does not, any document with a dotted phone number
#: (555.555.1234) becomes permanently unprocessable. A property check
#: catches this where per-function tests do not, because the defect lives
#: in the composition rather than in either part.
#:
#: Deriving is also why the per-class floors below are shared rather than
#: copied: with two floors per class, a restated list is a second place to
#: forget.
_SURVIVOR_SEPARATORS = _SCRUB_SEPARATORS


_DATE_TOKEN = re.compile(
    r'(?<![\w./-])(?:(?P<year>(?:19|20)\d{2})(?P<sep>[-/])'
    r'(?P<month>\d{2})(?P=sep)(?P<day>\d{2})|'
    r'(?P<us_month>\d{2})/(?P<us_day>\d{2})/(?P<us_year>(?:19|20)\d{2})|'
    r'(?P<word_month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
    r'\s+(?P<word_day>\d{1,2}),?\s+(?P<word_year>(?:19|20)\d{2}))'
    r'(?![\w/-])', re.I)
_MONTHS = ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec')
_MASKED_TAIL = re.compile(r'\*{4}\d{4}(?!\d)')


def _numeric_scan_text(text: str, *, mask_tails: bool = True) -> str:
    """Exclude complete dates and already-masked tails from numeric joining.

    Same-length placeholders keep match offsets valid in the original text.
    A newline alone is NOT a boundary: real account numbers wrap. Only these
    recognizable tokens break a run; ambiguous numeric references still scrub.
    """
    chars = list(text)
    spans = [m.span() for m in _MASKED_TAIL.finditer(text)] if mask_tails else []
    for match in _DATE_TOKEN.finditer(text):
        if match['word_month']:
            year, month, day = (match['word_year'], _MONTHS.index(match['word_month'][:3].lower()) + 1,
                                match['word_day'])
        else:
            year, month, day = (match['year'] or match['us_year'], match['month'] or match['us_month'],
                                match['day'] or match['us_day'])
        try:
            date(int(year), int(month), int(day))
        except ValueError:
            continue
        spans.append(match.span())
    for start, end in spans:
        chars[start:end] = '\0' * (end - start)
    return ''.join(chars)


def _runs(text: str, separator: str) -> "list[re.Match[str]]":
    """Digit runs joined by one separator class, bounded by known safe tokens."""
    return list(
        re.finditer(rf"(?<![\d.,])(\d+(?:[{separator}]\d+)*)(?![\d])", _numeric_scan_text(text))
    )


#: Checked before the generic run so its shape is never reduced to a tail.
_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")

# Banks may already hide a card's middle digits. The remaining BIN prefix
# is too short for the generic numeric floor, but must not survive merely
# because the source was partially masked. Keep only the intended last four.
_MASKED_CARD = re.compile(
    r"(?<![\w.])\d{4,8}[\s-]*(?:[*xX\u2022][\s-]*){3,}(\d{4})(?!\d)"
)

REDACTED = "[REDACTED]"


class UnscrubbedDigitsError(Exception):
    """Text still contains an account-shaped run after scrubbing.

    Deliberately carries the line number and nothing else. An error body is
    model context exactly like a tool result, so a guard that reported the
    leak by quoting it would defeat itself — `api/tests/test_redaction.py`
    asserts no digit run appears in the message.
    """


@dataclass(frozen=True)
class ScrubResult:
    text: str
    redactions: int


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def _floor_for(run: str, labelled: bool) -> int:
    """The minimum digit count that makes this run account-shaped."""
    if labelled and not ISO_DATE.match(run.strip()):
        # Label context overrides shape: a separated seven-digit run next to
        # "Transit" is not a reference number.
        return CONTIGUOUS_MIN_DIGITS
    return CONTIGUOUS_MIN_DIGITS if run.isdigit() else JOINED_MIN_DIGITS


def _replacement(run: str, labelled: bool = False) -> str | None:
    """`None` when the run is not account-shaped and must be left alone."""
    digits = _digits(run)
    if len(digits) < _floor_for(run, labelled):
        return None
    if len(digits) < TAIL_MIN_DIGITS:
        # Seven to nine digits go whole. Nine is ambiguous between a routing
        # number and an SSN, and an SSN's last four is the part used to
        # verify identity; seven and eight are too short to give a tail away
        # from safely.
        return REDACTED
    return f"****{digits[-4:]}"


def _is_labelled(text: str, start: int, end: int) -> bool:
    """Is an account-ish label within LABEL_WINDOW characters either side?

    Both directions, deliberately: statement layouts put the number before
    the label about as often as after, so a backwards-only search misses
    the runs this rule exists for.
    """
    before = text[max(0, start - LABEL_WINDOW) : start]
    after = text[end : end + LABEL_WINDOW]
    return bool(LABEL_CONTEXT.search(before) or LABEL_CONTEXT.search(after))


def scrub_text(text: str) -> ScrubResult:
    """Redact account-shaped runs. Amounts and dates are left intact.

    Amounts survive by arithmetic rather than by a special case: the largest
    realistic one, `999,999.99`, is eight digits, and a comma is not a
    separator here so `1,234,567.89` never forms a single run either. An
    unseparated figure over a million would be over-redacted. That is
    accepted on purpose — one rule shared with `assert_no_account_numbers`
    cannot disagree with itself, whereas an amount carve-out would make the
    scrubber skip exactly what the post-condition refuses, leaving a
    document that can never process.

    Also over-redacted, and likewise accepted: phone numbers, zip+4, and
    cheque or confirmation numbers of nine digits or more. None are needed
    downstream, and over-redacting a confirmation number is cheap next to
    under-redacting an account.
    """
    redactions = 0

    # Counted before substituting. Counting matches afterwards reports zero
    # while the redaction has in fact happened — right text, wrong number,
    # and a caller trusting `redactions == 0` would call the document clean.
    redactions += len(_SSN.findall(text))
    text = _SSN.sub(REDACTED, text)

    # A year immediately before ****1234 is a date, not an exposed card BIN.
    # Make that distinction before the prefix pass, not only in generic runs.
    scan = _numeric_scan_text(text, mask_tails=False)
    def masked_card(match):
        nonlocal redactions
        if scan[match.start()] == '\0':
            return match[0]
        redactions += 1
        return '****' + match[1]
    text = _MASKED_CARD.sub(masked_card, text)

    for separator in _SCRUB_SEPARATORS:
        pieces: list[str] = []
        cursor = 0
        for match in _runs(text, separator):
            replacement = _replacement(
                match.group(1), _is_labelled(text, match.start(), match.end())
            )
            if replacement is None:
                continue
            pieces.append(text[cursor : match.start()])
            pieces.append(replacement)
            cursor = match.end()
            redactions += 1
        pieces.append(text[cursor:])
        text = "".join(pieces)

    return ScrubResult(text=text, redactions=redactions)


def assert_no_account_numbers(text: str) -> None:
    """Raise `UnscrubbedDigitsError` if any account-shaped run survives.

    Call this on anything about to leave the process. It is the difference
    between "the scrubber handled every format we thought of" and "no
    account number is in this text", and only the second is worth promising.
    """
    scan = _numeric_scan_text(text, mask_tails=False)
    if any(scan[m.start()] != '\0' for m in _MASKED_CARD.finditer(text)):
        raise UnscrubbedDigitsError('a partially masked card prefix survived scrubbing; value withheld')
    # Check the whole document, like the scrubber: line-by-line checking would
    # miss wrapped accounts and lose the date context that bounds safe tokens.
    for separator in _SURVIVOR_SEPARATORS:
        for match in _runs(text, separator):
            labelled = _is_labelled(text, match.start(), match.end())
            if len(_digits(match.group(1))) >= _floor_for(match.group(1), labelled):
                offset = text.count('\n', 0, match.start()) + 1
                raise UnscrubbedDigitsError(
                    f"an account-shaped number survived scrubbing on line "
                    f"{offset}; refusing to return this text. The value is "
                    "deliberately not quoted here."
                )
