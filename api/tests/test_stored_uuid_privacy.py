"""Only synthetic UUIDs/numbers; no statements or live persisted data."""
import pytest
from pydantic import ValidationError

from api.models import TicketUpdate
from api.privacy import STORED_TEXT_REFUSAL, refuse_sensitive_numbers
from api.redaction import UnscrubbedDigitsError, assert_no_account_numbers, scrub_text


UUIDS = [
    'bbbbbbbb-bbbb-4bbb-8bbb-ab1234567890',
    '12345678-abcd-4abc-8abc-abcdefabcdef',
    'abcdefab-abcd-4abc-8000-123456abcdef',
]


@pytest.mark.parametrize('identifier', UUIDS + [UUIDS[0].upper()])
def test_ticket_update_preserves_complete_uuid_tokens(identifier):
    description = f'Parent epic `{identifier}`. See /tickets/{identifier} and ({identifier}).'
    assert TicketUpdate(description=description).description == description
    assert refuse_sensitive_numbers(description) == description


@pytest.mark.parametrize('text', [
    'bbbbbbbb-bbbb-3bbb-8bbb-ab1234567890',  # not UUIDv4
    'bbbbbbbb-bbbb-4bbb-7bbb-ab1234567890',  # not RFC variant
    'bbbbbbbb-bbbb-4bbb-8bbb-ab123456789',   # short tail
    'bbbbbbbb-bbbb-4bbb-8bbb-ab12345678900', # long tail
    'xbbbbbbbb-bbbb-4bbb-8bbb-ab1234567890',
    'bbbbbbbb-bbbb-4bbb-8bbb-ab1234567890x',
    '_bbbbbbbb-bbbb-4bbb-8bbb-ab1234567890',
    'bbbbbbbb-bbbb-4bbb-8bbb-ab1234567890_',
    '-bbbbbbbb-bbbb-4bbb-8bbb-ab1234567890',
    'bbbbbbbb-bbbb-4bbb-8bbb-ab1234567890-',
    'ébbbbbbbb-bbbb-4bbb-8bbb-ab1234567890',
    'bbbbbbbbbbbb4bbb8bbbab1234567890',      # no generic hex exemption
    'abcdef1234567890abcdef1234567890abcdef1234',  # no SHA exemption
])
def test_malformed_or_embedded_identifiers_still_refused(text):
    with pytest.raises(ValidationError, match=STORED_TEXT_REFUSAL):
        TicketUpdate(description=text)


@pytest.mark.parametrize('number', ['1234567', '123 456 789', '4532015112830366'])
@pytest.mark.parametrize('template', ['{number} `{uuid}`', '`{uuid}` {number}', '{number}\n{uuid}'])
def test_real_numeric_runs_adjacent_to_uuid_still_refused(number, template):
    with pytest.raises(ValueError, match=STORED_TEXT_REFUSAL):
        refuse_sensitive_numbers(template.format(number=number, uuid=UUIDS[0]))


@pytest.mark.parametrize('identifier', UUIDS)
def test_document_scanner_and_scrubber_remain_strict(identifier):
    with pytest.raises(UnscrubbedDigitsError):
        assert_no_account_numbers(identifier)
    result = scrub_text(identifier)
    assert result.redactions > 0
    assert result.text != identifier
    assert_no_account_numbers(result.text)


def test_api_ticket_description_update_roundtrips_uuid_and_refuses_adjacent_number(client, test_settings):
    headers = {'Authorization': f'Bearer {test_settings.api_token}'}
    entity = client.post('/entities', headers=headers,
                         json={'type': 'project', 'name': 'Synthetic project'}).json()
    board = client.post('/boards', headers=headers,
                        json={'entity_id': entity['id'], 'title': 'Synthetic board'}).json()
    created = client.post('/tickets', headers=headers, json={
        'board_id': board['id'], 'type': 'task', 'title': 'Synthetic task',
        'description': 'Original', 'actor': 'test',
    })
    assert created.status_code == 200
    url = '/tickets/' + created.json()['id']
    description = '\n'.join(f'- Related ticket `{identifier}`' for identifier in UUIDS)
    updated = client.patch(url, headers=headers, json={'description': description})
    assert updated.status_code == 200, updated.text
    assert updated.json()['description'] == description
    assert client.get(url, headers=headers).json()['description'] == description
    unsafe = client.patch(url, headers=headers,
                          json={'description': description + '\nAccount 4532015112830366'})
    assert unsafe.status_code == 422
    assert '4532015112830366' not in unsafe.text
    assert client.get(url, headers=headers).json()['description'] == description
