"""Tests for api/redaction.py — scrubbing account numbers out of text.

The rule this enforces is CLAUDE.md's: full account and routing numbers are
never sent to any LLM. The thing that makes it hard is *where* the scrub has
to happen. A tool result is model context, so the scrub must complete
server-side and the raw text must never be returned — which means these
tests are the last line before an account number reaches a model.

Both directions are asserted throughout. A scrubber that redacted
everything would satisfy "the number is gone" and destroy the document, so
every case that must survive is pinned as explicitly as every case that
must be removed.

No real statement text appears here. Every number is invented.
"""

import re

import pytest

from api.redaction import UnscrubbedDigitsError, assert_no_account_numbers, scrub_text


@pytest.mark.parametrize('card', [
    '411111******1234', '411111XXXXXXXX1234', '411111xxxxxx1234',
    '411111\u2022\u2022\u2022\u2022\u2022\u20221234', '411111 ****** 1234',
    '411111\n******\n1234', '41111111-****-1234',
])
def test_pre_masked_card_keeps_only_last_four(card):
    with pytest.raises(UnscrubbedDigitsError):
        assert_no_account_numbers(card)
    result = scrub_text('CARD ' + card + '\nCAFE 19.99 2026-06-15')
    assert result.text == 'CARD ****1234\nCAFE 19.99 2026-06-15'
    assert result.redactions == 1
    assert_no_account_numbers(result.text)
    assert scrub_text(result.text).text == result.text

# Invented, not drawn from any real document.
FAKE_CARD = "4532015112830366"
FAKE_ACCOUNT = "1234567890"
FAKE_ROUTING = "021000021"
FAKE_SSN = "123-45-6789"


@pytest.mark.parametrize('date_text', [
    '2026-09-19', '09/19/2026', '2026/09/19', 'September 19, 2026',
    'September\n19,\n2026', 'Sep 19 2026',
])
@pytest.mark.parametrize('number', ['****1234', '4532 0151\n1283 0366'])
@pytest.mark.parametrize('before', [True, False])
def test_complete_dates_are_boundaries_not_parts_of_adjacent_accounts(date_text, number, before):
    text = f'Account {date_text}\n{number}' if before else f'Account {number}\n{date_text}'
    result = scrub_text(text)
    expected_number = '****1234' if number.startswith('*') else '****0366'
    expected = f'Account {date_text}\n{expected_number}' if before else f'Account {expected_number}\n{date_text}'
    assert result.text == expected
    assert result.redactions == (0 if number.startswith('*') else 1)
    assert_no_account_numbers(result.text)
    assert scrub_text(result.text).text == result.text


@pytest.mark.parametrize('number', ['1234567', '1234\n567', '4532\n0151\n1283\n0366'])
def test_checker_rejects_wrapped_accounts_next_to_dates(number):
    text = f'Account September\n19,\n2026\n{number}'
    with pytest.raises(UnscrubbedDigitsError):
        assert_no_account_numbers(text)
    assert_no_account_numbers(scrub_text(text).text)


def test_masked_tail_does_not_swallow_next_short_reference():
    text = 'Card ****1234\n567890\nMerchant 12.34'
    assert scrub_text(text).text == text
    assert_no_account_numbers(text)


@pytest.mark.parametrize('text', [
    'Account 2026-09-19-1234', 'Account 1234-2026-09-19',
    'Account September 99, 2026\n1234567',
    'Account 4532\n0151\n1283\n0366',
    'Reference TX-AB123\n456789',
])
def test_ambiguous_runs_are_not_exempted_as_dates_or_references(text):
    assert scrub_text(text).text != text
    with pytest.raises(UnscrubbedDigitsError):
        assert_no_account_numbers(text)
    assert_no_account_numbers(scrub_text(text).text)


def _redacted(text: str) -> str:
    return scrub_text(text).text


def test_a_bare_account_number_is_removed():
    out = _redacted(f"Account Number {FAKE_CARD}")

    assert FAKE_CARD not in out
    assert "****0366" in out


def test_a_spaced_card_number_is_removed():
    out = _redacted("Card 4532 0151 1283 0366 ending")

    assert "4532 0151 1283 0366" not in out
    assert "****0366" in out


def test_a_hyphenated_account_number_is_removed():
    out = _redacted("Acct 4532-0151-1283-0366")

    assert "4532-0151-1283-0366" not in out
    assert "****0366" in out


def test_a_number_split_across_lines_is_still_removed():
    """pypdf inserts newlines mid-number, so a rule that stopped at line
    ends would miss account numbers in exactly the documents this exists
    for. Measured against real extraction behaviour, not assumed."""
    out = _redacted(f"Account\n4532 0151\n1283 0366\nend")

    assert "1283 0366" not in out
    assert "****0366" in out


@pytest.mark.parametrize(
    "text",
    [
        f"Routing {FAKE_ROUTING} for transfers",
        f"ABA: {FAKE_ROUTING}",
        f"SSN {FAKE_SSN} on file",
        f"SSN: {FAKE_SSN}",
    ],
)
def test_nine_digit_runs_are_removed_whole_with_no_tail(text):
    """No last4 for these, unlike a card.

    A bare nine-digit run is ambiguous between a routing number and an SSN,
    and an SSN's last four is precisely the part used for identity
    verification — so preserving a tail here would leak the useful half of
    the very thing being protected.
    """
    out = _redacted(text)

    assert FAKE_ROUTING not in out
    assert FAKE_SSN not in out
    assert "6789" not in out
    assert "0021" not in out
    assert "[REDACTED]" in out


def test_a_ten_digit_account_keeps_only_its_last_four():
    out = _redacted(f"Account: {FAKE_ACCOUNT}")

    assert FAKE_ACCOUNT not in out
    assert "****7890" in out


def test_two_accounts_on_one_line_are_both_removed():
    out = _redacted(f"From {FAKE_CARD} to 5100123412341234")

    assert FAKE_CARD not in out
    assert "5100123412341234" not in out
    assert out.count("****") == 2


# --- the other direction: what must survive --------------------------------


@pytest.mark.parametrize(
    "amount",
    ["84.99", "1,234.56", "12,345.67", "999,999.99", "1234.56", "0.01"],
)
def test_transaction_amounts_survive(amount):
    """The boundary, measured rather than asserted: no amount that occurs in
    these statements reaches nine digits. 999,999.99 is eight, and a comma
    breaks a run anyway since the separator class is space and hyphen only.
    An unseparated figure over $1,000,000 would be over-redacted — accepted
    deliberately, because one shared rule that cannot disagree with itself
    beats two rules that can.
    """
    text = f"Purchase {amount} at STORE"

    assert _redacted(text) == text


@pytest.mark.parametrize(
    "date", ["2026-09-19", "09/19/2026", "19 Sep 2026", "2026/09/19"]
)
def test_dates_survive(date):
    """Safe by construction: no date component reaches nine digits."""
    text = f"Posted {date} to the account"

    assert _redacted(text) == text


def test_short_reference_numbers_survive():
    text = "Ref 12345 confirmed, cheque 6789"

    assert _redacted(text) == text


def test_merchant_names_and_descriptions_survive():
    text = "MAPLE DONUTS #4521 ANYTOWN XY — debit purchase"

    assert _redacted(text) == text


# --- the count must describe the text --------------------------------------


def test_the_redaction_count_matches_the_markers_in_the_text():
    """Guards a bug the prototype actually had: counting matches *after*
    substituting them away reported zero while the redaction had happened.
    The output was right and the number was wrong, which is worse than
    either being wrong alone — a caller trusting `redactions == 0` would
    conclude the document was clean.
    """
    text = (
        f"Card {FAKE_CARD}\nRouting {FAKE_ROUTING}\nSSN {FAKE_SSN}\n"
        f"Account {FAKE_ACCOUNT}\nAmount 84.99"
    )

    result = scrub_text(text)

    markers = result.text.count("[REDACTED]") + len(
        re.findall(r"\*{4}\d{4}", result.text)
    )
    assert result.redactions == markers
    assert result.redactions == 4


def test_a_clean_document_reports_zero():
    result = scrub_text("Purchase 84.99 at STORE on 2026-09-19")

    assert result.redactions == 0
    assert result.text == "Purchase 84.99 at STORE on 2026-09-19"


# --- the fail-closed post-condition ----------------------------------------


def test_the_post_condition_accepts_scrubbed_text():
    assert_no_account_numbers(_redacted(f"Card {FAKE_CARD}"))


def test_the_post_condition_rejects_a_surviving_run():
    """The load-bearing guard. The failure that matters is a format the
    scrubber does not recognise, because it leaks silently — the output
    looks fine. This turns that into a refusal.
    """
    with pytest.raises(UnscrubbedDigitsError):
        assert_no_account_numbers(f"Account {FAKE_CARD} still here")


def test_a_dotted_run_is_scrubbed_rather_than_withheld():
    """This test used to assert the opposite, and that was the bug.

    It read the post-condition knowing a separator class the scrubber did
    not as a *feature* — "a wider net catches detector gaps". It is not: a
    class the checker knows and the scrubber does not means every document
    containing that shape is withheld permanently, with a reason that
    deliberately cannot say what it found. Fail-closed protects against a
    leak; it does nothing for availability, and a statement that can never
    be processed is not a safe statement.

    The two now share one class list by derivation, so the post-condition
    can only fire when the scrubber genuinely failed to substitute —
    which is a bug worth refusing over, rather than a shape nobody taught
    the scrubber about.
    """
    out = scrub_text("Account 4532.0151.1283.0366")

    assert "4532.0151.1283.0366" not in out.text
    assert_no_account_numbers(out.text)


def test_a_phone_number_on_a_letterhead_does_not_withhold_the_document():
    """The case that made this a defect rather than a curiosity: one dotted
    phone number anywhere in a statement blocked the entire document."""
    out = scrub_text("Call 555.555.1234 for service")

    assert_no_account_numbers(out.text)
    assert "555.555.1234" not in out.text


def test_an_amount_beside_a_redacted_reference_keeps_its_digits():
    """A run may not begin immediately after a decimal point, so the
    fractional part of an amount cannot be swallowed into a following
    run."""
    out = scrub_text("AMT 1234.56 7890123 ref")

    assert "1234.56" in out.text
    assert "7890123" not in out.text


def test_the_refusal_never_echoes_the_digits_it_found():
    """An error body is model context too. A guard that reports the leak by
    quoting it has not prevented anything."""
    with pytest.raises(UnscrubbedDigitsError) as exc_info:
        assert_no_account_numbers(f"Account {FAKE_CARD} still here")

    message = str(exc_info.value)
    assert FAKE_CARD not in message
    assert not re.search(r"\d{5,}", message), f"digits leaked into: {message!r}"


def test_the_refusal_says_where_without_saying_what():
    with pytest.raises(UnscrubbedDigitsError) as exc_info:
        assert_no_account_numbers(f"line one\nline two {FAKE_CARD}")

    assert "2" in str(exc_info.value)


def test_scrubbed_output_always_passes_its_own_post_condition():
    """Scrubber and post-condition share one rule, so they cannot disagree
    — the property that makes the pair safe rather than merely layered."""
    text = (
        f"Card {FAKE_CARD} Routing {FAKE_ROUTING} SSN {FAKE_SSN} "
        f"Acct 4532-0151-1283-0366 amount 1,234.56 date 2026-09-19"
    )

    assert_no_account_numbers(scrub_text(text).text)


def test_a_run_longer_than_a_card_number_is_still_redacted():
    """No upper bound: a longer run is more suspicious, not less.

    An earlier version capped the rule at sixteen digits, on the theory
    that sixteen is the longest card. That left longer runs matched by
    neither the scrubber nor the post-condition. Verified rather than
    argued: under that ceiling `1234 5678 9012 3456 7890` survived intact,
    the whole being too long to match and each group too short.
    """
    long_run = "123456789012345678"

    result = scrub_text(f"Reference {long_run} posted")

    assert long_run not in result.text
    assert result.redactions == 1


def test_the_post_condition_also_has_no_upper_bound():
    with pytest.raises(UnscrubbedDigitsError):
        assert_no_account_numbers("Reference 123456789012345678 posted")


# --- the seven-digit floor (The owner's decision, ticket T-10) ---------------


@pytest.mark.parametrize("run", ["1234567", "12345678"])
def test_a_contiguous_seven_or_eight_digit_run_is_redacted(run):
    """Canadian bank account numbers are commonly seven digits.

    A contiguous 7-8 digit run, or one within 40 characters of an
    account-ish label, could be an account number reaching a model — the same exposure ticket T-53 closed,
    one digit-length further down.
    """
    out = _redacted(f"Account {run} shown")

    assert run not in out
    assert "[REDACTED]" in out


@pytest.mark.parametrize("run", ["1234567", "12345678"])
def test_a_seven_or_eight_digit_run_keeps_no_tail(run):
    """Unlike a card. Seven digits is short enough that a last-four leaks a
    meaningful fraction of the number, and nothing downstream matches an
    account on a seven-digit tail."""
    out = _redacted(f"Acct {run}")

    assert "****" not in out
    assert run[-4:] not in out


def test_a_separator_joined_eight_digit_run_survives_outside_label_context():
    """The floor stays at nine for separated runs, which is what keeps ISO
    dates and grouped amounts intact. `2026-09-19` is eight digits joined by
    hyphens; dropping the floor for separated runs too would eat every date
    in every statement."""
    text = "Posted 2026-09-19 and 1234 567 reference"

    assert _redacted(text) == text


@pytest.mark.parametrize(
    "label", ["Account", "Acct", "A/C", "No.", "Transit", "Branch", "Routing", "Card"]
)
def test_a_separated_seven_digit_run_in_label_context_is_redacted(label):
    """A separated run is left alone in
    general — but next to an account label it is the thing being protected,
    not a reference number."""
    out = _redacted(f"{label} 1234 567 on file")

    assert "1234 567" not in out
    assert "[REDACTED]" in out


def test_an_iso_date_next_to_an_account_label_still_survives():
    """The label rule applies to 7-8 digit runs; a date is eight digits and
    could be caught by it. Dates appear beside account labels constantly in
    statement headers, so this is the case where the label rule would do
    real damage if it were not shape-aware."""
    text = "Account statement period 2026-09-19 to 2026-10-19"

    assert "2026-09-19" in _redacted(text)


@pytest.mark.parametrize("amount", ["84.99", "1,234.56", "1,234,567.89", "999,999.99"])
def test_amounts_still_survive_at_the_lower_floor(amount):
    text = f"Purchase {amount} at STORE"

    assert _redacted(text) == text


def test_the_post_condition_floor_moves_with_the_scrubber():
    """Both drop to seven for contiguous runs, so the pair still shares one
    definition per class — the property that makes a surviving run provably
    a detector gap rather than a disagreement between two rules."""
    with pytest.raises(UnscrubbedDigitsError):
        assert_no_account_numbers("Account 1234567 still here")


def test_the_post_condition_does_not_fire_on_a_separated_eight_digit_run():
    assert_no_account_numbers("Posted 2026-09-19 fine")


@pytest.mark.parametrize("brand", ["VISA", "MASTERCARD", "AMEX", "MC", "American Express"])
def test_card_brand_names_count_as_label_context(brand):
    """Tech lead's decision on ticket T-53: brands widen the rule only
    in the fail-closed direction, and nothing downstream needs a 7-8 digit
    run beside one."""
    out = _redacted(f"{brand} 1234 567 on file")

    assert "1234 567" not in out
    assert "[REDACTED]" in out


@pytest.mark.parametrize("word", ["NOVEMBER", "NOTE", "CARDINAL HEALTH", "MCDONALDS"])
def test_a_word_merely_containing_a_label_is_not_label_context(word):
    """`no` inside NOVEMBER and `card` inside CARDINAL used to match, so
    label context fired on nearly every statement page — collapsing the
    separator-joined floor from nine to seven everywhere and quietly
    undoing the per-class split. Word boundaries fixed it; this pins it."""
    text = f"{word} 1234 567 reference"

    assert _redacted(text) == text


def test_a_hyphenated_account_fragment_cannot_hide_behind_the_date_exemption():
    """The date exemption matches a plausible date, not any 4-2-2 shape.
    `4532-01-51` is not a date, and beside an account label is precisely
    where it would matter."""
    out = _redacted("Account 4532-01-51 listed")

    assert "4532-01-51" not in out


@pytest.mark.parametrize("date", ["2026-09-19", "1999-12-31", "2001-01-01"])
def test_a_real_date_beside_a_label_still_survives(date):
    assert date in _redacted(f"Account statement period {date} onward")
