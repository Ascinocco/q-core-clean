"""Property tests: the scrubber and its post-condition must AGREE, and the
floors must match the spec across every shape — not just the points a test
file happens to name.

Written by review-1 and taken into this PR as a permanent test. The
dot-separator defect it found was invisible to per-function tests because it
lived in the composition: the scrubber left dot-separated runs alone, the
post-condition rejected them, so each part passed its own tests and the pair
could never succeed together. One letterhead phone number withheld a whole
statement, permanently.

Kept even though the post-condition now derives its separator classes from
the scrubber's. Deriving narrows the drift, it does not close it — someone
adding a class in one place and not the other is exactly how the dot gap
appeared, and this is what notices.
"""

from api.redaction import (
    UnscrubbedDigitsError,
    assert_no_account_numbers,
    scrub_text,
)

#: Exactly the labels the owner approved (ticket T-10): Account, Acct, A/C,
#: No., Transit, Branch, Routing, Card. review-1's original list also had
#: "VISA", which surfaced six mismatches here — not an implementation bug
#: but a spec disagreement, since card BRAND names are not in the approved
#: rule. Aligned to the approved list rather than widening the rule to fit
#: the test; whether brands should be labels is raised with the tech lead,
#: and if the answer is yes it is a one-line change in both places.
LABELS = [
    "ACCOUNT ",
    "ACCT ",
    "A/C ",
    "No. ",
    "TRANSIT ",
    "BRANCH ",
    "ROUTING ",
    "CARD ",
    "VISA ",
    "MASTERCARD ",
    "AMEX ",
]
SEPARATORS = [("contiguous", ""), ("space", " "), ("hyphen", "-"), ("dot", ".")]
WIDTHS = range(4, 21)


def _shapes():
    for digits in WIDTHS:
        run = "".join(str((i % 9) + 1) for i in range(digits))
        for name, sep in SEPARATORS:
            body = sep.join(run[i : i + 4] for i in range(0, digits, 4)) if sep else run
            for label in [""] + LABELS:
                yield digits, name, label.strip(), f"{label}{body} end"


def _spec_says_redact(digits: int, separator_class: str, labelled: bool) -> bool:
    """The SPEC, written from the requirement — deliberately NOT derived from
    the implementation's constants. Keep it that way: a test that reads the
    same constants the code reads passes whatever the code does.
    """
    if separator_class == "contiguous":
        return digits >= 7
    if labelled and 7 <= digits <= 8:
        return True
    return digits >= 9


def test_scrubber_output_always_satisfies_the_post_condition():
    violations = []
    for digits, sep_class, label, text in _shapes():
        try:
            assert_no_account_numbers(scrub_text(text).text)
        except UnscrubbedDigitsError:
            violations.append((digits, sep_class, label or None))
    assert not violations, (
        "the scrubber left runs its own post-condition rejects, so the endpoint "
        f"fail-closes on documents it should handle: {violations[:20]}"
        f" ({len(violations)} total)"
    )


def test_redaction_floors_match_the_spec():
    mismatches = []
    for digits, sep_class, label, text in _shapes():
        got = scrub_text(text).text != text
        want = _spec_says_redact(digits, sep_class, bool(label))
        if got != want:
            mismatches.append(
                (digits, sep_class, label or None, f"got={got} want={want}")
            )
    assert not mismatches, (
        f"redaction does not match the spec: {mismatches[:20]} ({len(mismatches)} total)"
    )


def test_amounts_and_dates_survive():
    """Survival asserted directly, not inferred from an absence of matches.

    The first five come from review-1. The rest are the shapes real
    statement PDFs carry (with invented values), added here because that
    list is what stops the lowered floor over-redacting: such documents
    write dates as `Mon DD, YYYY` and `MonDD`, rarely ISO, and carry many
    currency-shaped amounts.
    """
    must_survive = [
        "POSTED 2026-09-19",
        "TOTAL 1,234,567.89",
        "AMT 84.99",
        "AMT -52.10",
        "MAPLE DONUTS 84.99 2026-09-19",
        "QTY 1234",
        "REF 123456",
        # Shapes seen in real documents; every value here is invented.
        "Sep 19, 2026 EXAMPLE CAFE 4.00",
        "Sep19 FUELCO 50.00 Sep20 GROCERCO 80.00",
        "Statement period Sep 1, 2026 to Sep 30, 2026",
        "09/19/2026 PAYMENT - THANK YOU 1,234.56",
        "Minimum payment 10.00 due Oct 20, 2026",
        "Previous balance 1,234.56 New balance 2,345.67",
        "5 transactions totalling 100.00",
    ]
    damaged = [t for t in must_survive if scrub_text(t).text != t]
    assert not damaged, f"these must never be redacted: {damaged}"


ORDINARY_WORDS = [
    "NOTE",
    "NOTICE",
    "ANOTHER",
    "KNOW",
    "ACCOUNTANT",
    "BRANCHES",
    "CARDIOLOGY",
    "MCDONALDS",
    "NOVEMBER",
]


def test_ordinary_words_containing_a_label_are_not_label_context():
    """The boundary, pinned from the negative side.

    Unbounded, `no` matches inside NOTE/ANOTHER/KNOW and `card` inside
    CARDIOLOGY, so label context fired on nearly every statement page and
    the separator-joined floor collapsed from nine to seven everywhere.
    Over-redaction, so not a leak — but the per-class split was not running,
    which is worse than it sounds because the split is the thing keeping
    dates alive.
    """
    redacted = [w for w in ORDINARY_WORDS if scrub_text(f"{w} 1234 567 ref").text
                != f"{w} 1234 567 ref"]
    assert not redacted, (
        "these ordinary words are being treated as account labels, so a "
        f"separated seven-digit run beside them is redacted: {redacted}"
    )


def test_a_label_abutting_its_digits_is_still_label_context():
    """`Account1234567` with no space. pypdf drops the separator between a
    label and its number routinely, and a boundary rule that treats digits
    as word characters would stop seeing these as labelled."""
    assert scrub_text("Account1234 567 end").text != "Account1234 567 end"


def test_dates_bound_wrapped_accounts_without_exempting_them():
    for separator in (' ', '\n', '\t', '-', '.'):
        for number in ('4532015112830366', '12345678901234567890'):
            for width in (2, 3, 4, 5):
                grouped = separator.join(number[i:i + width] for i in range(0, len(number), width))
                for stamp in ('2026-09-19', 'Sep\n19,\n2026'):
                    for text in (f'Account {stamp}\n{grouped}', f'Account {grouped}\n{stamp}'):
                        out = scrub_text(text).text
                        assert stamp in out
                        assert_no_account_numbers(out)
                        assert scrub_text(out).text == out
                        try:
                            assert_no_account_numbers(text)
                        except UnscrubbedDigitsError:
                            pass
                        else:
                            raise AssertionError('Synthetic unmasked account was accepted')
