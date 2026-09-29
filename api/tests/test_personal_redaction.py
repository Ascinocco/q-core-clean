"""Invented identities only. Exercise both privacy and financial utility."""

import csv
import io
import json
import logging
import re

import pytest

from api.personal_redaction import (PersonalRedactionError, PrivacyProfile,
                                    load_profile, scrub_document, scrub_personal)
from api.redaction import assert_no_account_numbers
from api.tests.pdf_fixture import make_pdf


@pytest.fixture
def profile():
    return PrivacyProfile(('Alex Example', 'Alex Q Example'),
                          ('123 Example Lane', 'Unit 4', 'Fictiontown XY A1A 1A1'))


@pytest.mark.parametrize('extra', ['54321', '12345678901', '****2345', '[REDACTED]', '',
                                 '12345\n54321'])
def test_statement_print_metadata_only_removed(profile, extra):
    code = 'BKAST54321_123456789_007'
    header = (f'#42Z654321#\n{code}\n-\n123456789\nPRN\n-\n-\n12\n-\n34\n-\n56\n'
              f'-\n-\n654321\n{code}\nX\nZ\n{extra}\n')
    financial = ('Your\naccount\nnumber:\n****9876\nTransactions\n'
                 '2026-06-15 MEGA-MART 54321 -12.34\nReference TX-AB123\n'
                 'Reference 654321\nSTEAM 5.00\nClosing Balance $1,234.56\n')
    source = f'Page 1 of 2\n{header}Alex Example\n{financial}{code}\n-\n12345\n654321\n'
    result = scrub_document(source, '.pdf', profile)
    assert 'BKAST' not in result.text and 'PRN' not in result.text
    assert '#42Z654321#' not in result.text
    assert financial in result.text
    assert 'Page 1 of 2' in result.text
    assert len(re.findall(r'\b54321\b', result.text)) == 1
    assert result.text.count('654321') == 1
    assert result.text == scrub_document(result.text, '.pdf', profile).text


@pytest.mark.parametrize('middle', ['UNKNOWN', 'CAFE 12.34', '2026-06-15', ''])
def test_unrecognized_print_header_refuses_instead_of_swallowing_financial_data(profile, middle):
    with pytest.raises(PersonalRedactionError, match='Unrecognized statement print metadata'):
        scrub_document(f'#42Z654321#\nBKAST54321_123456789_007\n{middle}\nX\nZ\n', '.pdf', profile)


def test_header_cleanup_reaches_extraction_api(client, test_settings):
    from pathlib import Path
    root = Path(test_settings.intake_dir)
    root.mkdir()
    code = 'BKAST54321_123456789_007'
    (root / 'print.pdf').write_bytes(make_pdf([
        f'#42Z654321#\n{code}\n-\n123456789\nPRN\n-\n12\n{code}\nX\nZ\n54321\n'
        'Your account number: 4111111111119876\nMEGA-MART 12.34'
    ]))
    response = client.post('/documents/extract', json={'path': 'print.pdf'},
                           headers={'Authorization': 'Bearer test-token'})
    assert response.status_code == 200
    text = response.json()['text']
    assert 'BKAST' not in text and '54321' not in text and 'PRN' not in text
    assert '****9876' in text and 'MEGA-MART 12.34' in text


def test_continuation_header_year_and_account_tail_survive_api(client, test_settings):
    from pathlib import Path
    root = Path(test_settings.intake_dir)
    root.mkdir()
    (root / 'dates.pdf').write_bytes(make_pdf([
        'Alex Example\nYour account\nMarch\n1\nto\nMarch\n31,\n2026\n'
        '4532 0151\n1283 0366\nTransactions\n2026-03-17 CAFE 12.34',
        'Alex Example\nMarch\n31,\n2026\n****0366\nClosing Balance 12.34'
    ]))
    response = client.post('/documents/extract', json={'path': 'dates.pdf'},
                           headers={'Authorization': 'Bearer test-token'})
    assert response.status_code == 200
    text = response.json()['text']
    assert text.count('March\n31,\n2026\n****0366') == 2
    assert '2026-03-17 CAFE 12.34' in text
    assert 'Alex' not in text and '4532' not in text


def test_known_values_case_punctuation_wraps_and_substrings(profile):
    text = "ALEX Q.\nEXAMPLE\n123 EXAMPLE LANE\nUNIT 4\nFICTIONTOWN XY A1A1A1\nAlex Examples Cafe 19.99"
    result = scrub_personal(text, profile)
    assert 'ALEX Q' not in result.text
    assert '123' not in result.text
    assert 'FICTIONTOWN' not in result.text
    assert 'Alex Examples Cafe 19.99' in result.text


def test_profile_address_matches_word_per_line_pdf_output():
    profile = PrivacyProfile(('Alex Example',), ('42 Imaginary Circle Dr', 'Fictiontown XY Z9Z 9Z9'))
    source = 'Alex\nExample\n42\nIMAGINARY\nCIRCLE\nDR\nFICTIONTOWN\nXY\nZ9Z\n9Z9\nCAFE 5.00'
    result = scrub_document(source, '.pdf', profile)
    assert 'IMAGINARY' not in result.text
    assert 'FICTIONTOWN' not in result.text
    assert 'CAFE 5.00' in result.text


def test_unconfigured_address_and_po_box_split_word_per_line(profile):
    source = ('42\nIMAGINARY\nSTREET,\nP.O.\nBOX\n123\nFICTIONTOWN\nREGIONLAND\n'
              'Z9Z\n9Z9\n2026-06-15 CAFE -5.00')
    result = scrub_document(source, '.pdf', profile)
    for fragment in ('IMAGINARY', 'FICTIONTOWN', 'Z9Z', '9Z9'):
        assert fragment not in result.text
    assert '2026-06-15 CAFE -5.00' in result.text


def test_wrapped_address_must_not_consume_transactions(profile):
    with pytest.raises(PersonalRedactionError, match='Ambiguous address'):
        scrub_document('42\nIMAGINARY\nSTREET\nCAFE 5.00\nTown XY Z9Z\n9Z9', '.pdf', profile)


def test_profile_street_only_does_not_detach_unconfigured_postal_lines(profile):
    source = '123\nEXAMPLE\nLANE\nFICTIONTOWN\nXY\nZ9Z\n9Z9\nCAFE 5.00'
    result = scrub_document(source, '.pdf', profile)
    assert 'FICTIONTOWN' not in result.text and 'Z9Z' not in result.text
    assert 'CAFE 5.00' in result.text


def test_browser_urls_with_identifiers_removed_without_changing_merchants(profile):
    source = ('https://bank.example/account?accountKey=PRIVATEKEY/2\n'
              'https://bank.example/account?accountKey=****9876/2\n'
              'https://bank.example/account/#/details\n'
              'https://bank.example/help\n2026-06-15 MEGA-MART 12.34\nSTEAM 5.00')
    result = scrub_document(source, '.pdf', profile)
    assert 'accountKey' not in result.text
    assert 'PRIVATEKEY' not in result.text and '9876' not in result.text
    assert '#/details' not in result.text
    assert 'https://bank.example/help' in result.text
    assert '2026-06-15 MEGA-MART 12.34' in result.text
    assert 'STEAM 5.00' in result.text
    assert result.text == scrub_document(result.text, '.pdf', profile).text


def test_card_and_url_fix_reaches_api(client, test_settings):
    from pathlib import Path
    root = Path(test_settings.intake_dir)
    root.mkdir()
    (root / 'masked.pdf').write_bytes(make_pdf([
        'CARD 411111******1234\nhttps://bank.example/?accountKey=PRIVATEKEY\nMEGA-MART 12.34'
    ]))
    response = client.post('/documents/extract', json={'path': 'masked.pdf'},
                           headers={'Authorization': 'Bearer test-token'})
    assert response.status_code == 200
    assert '411111' not in response.text and 'PRIVATEKEY' not in response.text
    assert '****1234' in response.json()['text']
    assert 'MEGA-MART 12.34' in response.json()['text']


def test_labelled_unknown_name_is_removed_from_later_page(profile):
    source = 'Customer name: Jamie Fiction\nCAFE 5.00\nPage 2\nJamie Fiction\nSHELL 20.00'
    result = scrub_personal(source, profile)
    assert 'Jamie' not in result.text
    assert 'CAFE 5.00' in result.text
    assert 'SHELL 20.00' in result.text


def test_unlabelled_postal_block_and_repeated_addressee(profile):
    source = 'Jamie Fiction\n42 Imaginary Street\nFictiona XY Z9Z 9Z9\nCAFE 5.00\nJamie Fiction'
    result = scrub_personal(source, profile)
    for private in ('Jamie', 'Imaginary', 'Fictiona', 'Z9Z'):
        assert private not in result.text
    assert 'CAFE 5.00' in result.text


@pytest.mark.parametrize('source', [
    'Mailing address: Unfamiliar region without a postal terminator',
    '42 Unknown Street\nCAFE 5.00\nFictiona XY Z9Z 9Z9',
    'Fictiona XY Z9Z 9Z9',
    'Customer name:\nBalance 5.00',
])
def test_ambiguous_sections_fail_without_echoing_source(profile, source):
    with pytest.raises(PersonalRedactionError) as exc:
        scrub_personal(source, profile)
    assert source not in str(exc.value)


def test_csv_memo_is_suppressed_and_financial_fields_survive(profile):
    source = ('Posted,Kind,Payee,Note,Value\n'
              '06/15/2026,DEBIT,MAPLE DONUTS,"Unknown Person, 14 Other Road",-19.99\n'
              '06/16/2026,CREDIT,REFUND,alex@example.invalid,1.15\n')
    result = scrub_document(source, '.csv', profile)
    rows = list(csv.DictReader(io.StringIO(result.text)))
    assert rows[0] == {'Posted': '06/15/2026', 'Kind': 'DEBIT',
                       'Payee': 'MAPLE DONUTS', 'Note': '[REDACTED]', 'Value': '-19.99'}
    assert rows[1]['Value'] == '1.15'
    assert 'Unknown' not in result.text
    assert 'example.invalid' not in result.text


@pytest.mark.parametrize('source', ['Name,Amount\nJamie Fiction,-1.00',
                                    'date,mystery\n2026-06-01,Private',
                                    'date,amount\n2026-06-01,5.00,Private',
                                    'date,date\na,b', 'date,amount\n"unterminated'])
def test_unreviewed_csv_shapes_refused(profile, source):
    with pytest.raises(PersonalRedactionError):
        scrub_document(source, '.csv', profile)


def test_numbers_dates_merchants_references_and_determinism(profile):
    source = ('Alex Example\nAccount 4532015112830366\n2026-06-15 MAPLE DONUTS -19.99\n'
              'Reference TX-AB123\nalex@example.invalid')
    result = scrub_document(source, '.txt', profile)
    assert 'Alex Example' not in result.text
    assert '45320151' not in result.text
    assert '2026-06-15 MAPLE DONUTS -19.99' in result.text
    assert 'TX-AB123' in result.text
    assert_no_account_numbers(result.text)
    assert result == scrub_document(source, '.txt', profile)
    assert result.text == scrub_document(result.text, '.txt', profile).text


def test_profile_missing_invalid_unreviewed_or_public_is_refused(tmp_path):
    target = tmp_path / 'profile.json'
    for content in (None, '{"names": "SECRET"}', '{invalid PRIVATE DATA',
                    json.dumps({'version': 1, 'reviewed': False, 'names': ['Jamie Fiction'], 'addresses': ['42 Fake Road']})):
        if content is not None:
            target.write_text(content)
            target.chmod(0o600)
        with pytest.raises(PersonalRedactionError) as exc:
            load_profile(str(target))
        assert 'SECRET' not in str(exc.value) and 'Jamie' not in str(exc.value)
    target.write_text(json.dumps({'version': 1, 'reviewed': True, 'names': ['Jamie Fiction'], 'addresses': ['42 Fake Road']}))
    target.chmod(0o644)
    with pytest.raises(PersonalRedactionError):
        load_profile(str(target))
    target.chmod(0o600)
    assert load_profile(str(target)).names == ('Jamie Fiction',)
    assert 'Jamie Fiction' not in repr(load_profile(str(target)))


def test_pdf_api_removes_pii_across_pages_and_from_logs(client, test_settings, caplog):
    from pathlib import Path
    root = Path(test_settings.intake_dir)
    root.mkdir()
    source = root / 'Alex Example 123 Example Lane.pdf'
    source.write_bytes(make_pdf([
        'Alex Example\n123 Example Lane\nAccount 4532015112830366\nCAFE 19.99',
        'Alex Example\nCustomer name: Jamie Fiction\nMAPLE DONUTS 1.15',
    ]))
    with caplog.at_level(logging.INFO, logger='api.documents'):
        response = client.post('/documents/extract', json={'path': str(source)},
                               headers={'Authorization': 'Bearer test-token'})
    assert response.status_code == 200
    body = response.json()
    assert body['pages'] == 2
    assert 'CAFE 19.99' in body['text']
    assert 'MAPLE DONUTS 1.15' in body['text']
    for private in ('Alex Example', '123 Example Lane', 'Jamie Fiction', '45320151'):
        assert private not in response.text
        assert private not in caplog.text


def test_api_missing_profile_has_no_raw_text_even_with_allow_partial(client, test_settings):
    from pathlib import Path
    Path(test_settings.privacy_profile_path).unlink()
    root = Path(test_settings.intake_dir)
    root.mkdir()
    (root / 'example.txt').write_text('PRIVATE NAME AND ADDRESS')
    response = client.post('/documents/extract', json={'path': 'example.txt', 'allow_partial': True},
                           headers={'Authorization': 'Bearer test-token'})
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'personal_redaction_required'
    assert 'PRIVATE NAME' not in response.text


def test_privacy_changes_do_not_echo_exception_details(test_settings, monkeypatch):
    from api import personal_redaction
    def fail(*args, **kwargs):
        raise OSError('SECRET PROFILE CONTENT')
    monkeypatch.setattr(personal_redaction.json, 'loads', fail)
    # Call loader directly: patching the shared json module would interfere
    # with the HTTP client's request parsing as well.
    with pytest.raises(PersonalRedactionError) as exc:
        load_profile(test_settings.privacy_profile_path)
    assert 'SECRET' not in str(exc.value)
