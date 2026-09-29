"""Real API compare-and-swap guards, with disposable SQLite only."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest


@pytest.fixture
def ticket(client, test_settings):
    headers = {'Authorization': f'Bearer {test_settings.api_token}'}
    entity = client.post('/entities', headers=headers, json={'type':'project','name':'Synthetic'}).json()
    board = client.post('/boards', headers=headers, json={'entity_id':entity['id'],'title':'Synthetic'}).json()
    row = client.post('/tickets', headers=headers, json={'board_id':board['id'],'type':'task','title':'Synthetic','actor':'worker'}).json()
    return '/tickets/' + row['id'], headers


def history(client, ticket):
    url, headers = ticket
    return client.get(url + '/transitions', headers=headers).json()['items']


def move(client, ticket, expected=None, status='review', actor='worker'):
    url, headers = ticket
    body = {'to_status':status,'actor':actor,'note':'Synthetic transition'}
    if expected is not None:
        body['expected_transition_id'] = expected
    return client.post(url + '/transition', headers=headers, json=body)


def test_stale_history_refuses_after_same_second_human_roundtrip(client, ticket, test_settings):
    old = history(client, ticket)[-1]['id']
    assert move(client, ticket, status='in_progress', actor='human').status_code == 200
    assert move(client, ticket, status='backlog', actor='human').status_code == 200
    import sqlite3
    with sqlite3.connect(test_settings.db_path) as connection:
        connection.execute("UPDATE ticket_transitions SET created_at = '2026-09-22 00:00:00'")
    before = history(client, ticket)
    assert len({row['created_at'] for row in before}) == 1
    assert move(client, ticket, expected=old).status_code == 409
    assert history(client, ticket) == before
    url, headers = ticket
    assert client.get(url, headers=headers).json()['status'] == 'backlog'


def test_matching_guard_and_unguarded_compatibility(client, ticket):
    latest = history(client, ticket)[-1]['id']
    assert move(client, ticket, expected=latest).status_code == 200
    assert move(client, ticket, status='in_progress').status_code == 200
    assert len(history(client, ticket)) == 3


def test_racing_same_token_has_one_winner(client, ticket):
    latest = history(client, ticket)[-1]['id']
    barrier = Barrier(2)
    def race(_):
        barrier.wait(timeout=10)
        return move(client, ticket, expected=latest).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(race, range(2))) == [200,409]
    assert len(history(client, ticket)) == 2


def test_malformed_expected_token_refused(client, ticket):
    assert move(client, ticket, expected='not-a-uuid').status_code == 422
    assert len(history(client, ticket)) == 1


def test_transition_checks_history_while_holding_writer_lock(client, ticket, test_settings, monkeypatch):
    import sqlite3
    import api.jyra
    latest = history(client, ticket)[-1]['id']
    original = api.jyra._get_ticket_row
    checks = []
    def get(connection, identifier):
        if not checks:
            with sqlite3.connect(test_settings.db_path, timeout=0) as contender:
                with pytest.raises(sqlite3.OperationalError, match='locked'):
                    contender.execute('BEGIN IMMEDIATE')
            checks.append(True)
        return original(connection, identifier)
    monkeypatch.setattr(api.jyra, '_get_ticket_row', get)
    assert move(client, ticket, expected=latest).status_code == 200
    assert checks == [True]
