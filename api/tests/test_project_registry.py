"""Managed repository assignments through the real API and isolated SQLite."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Barrier

import pytest


@pytest.fixture
def headers(test_settings):
    return {"Authorization": f"Bearer {test_settings.api_token}"}


def create(client, headers, attributes=None, **fields):
    return client.post('/entities', headers=headers, json={
        'type': 'project', 'name': 'Example', 'attributes': attributes or {}, **fields,
    })


@pytest.mark.parametrize('path', [
    '/projects/app', '../app', 'projects/../app', 'projects/app/nested',
    'projects/app/', 'projects/App', 'projects/a_b', 'projects/a--b',
    'projects/-app', 'projects/app-', 'projects/', 'projects/a b',
    'projects/app\n', 'projects/é', '', 123,
])
def test_invalid_path_refused_on_create_and_patch(client, headers, path):
    project = create(client, headers).json()
    attributes = {'repository_path': path}
    assert create(client, headers, attributes).status_code == 422
    assert client.patch(f"/entities/{project['id']}", headers=headers,
                        json={'attributes': attributes}).status_code == 422
    assert client.get(f"/entities/{project['id']}", headers=headers).json()['attributes'] == {}


@pytest.mark.parametrize('attributes', [{}, {'description': 'Old project'}, {'repository_path': None}])
def test_unmanaged_projects_remain_valid(client, headers, attributes):
    for _ in range(2):
        response = create(client, headers, attributes)
        assert response.status_code == 200
        assert 'repository_path' not in response.json()['attributes']


def test_roundtrip_collision_and_release(client, headers):
    attributes = {'description': 'Game', 'repository_path': 'projects/bakery-app2'}
    first = create(client, headers, attributes).json()
    second = create(client, headers).json()
    url = f"/entities/{first['id']}"
    assert client.get(url, headers=headers).json()['attributes'] == attributes
    assert client.get('/entities?type=project', headers=headers).json()['total'] == 2
    # Archiving must not silently make an occupied path available.
    assert client.patch(url, headers=headers, json={'status': 'archived'}).status_code == 200
    duplicate = create(client, headers, attributes)
    assert duplicate.status_code == 409
    assert first['id'] in duplicate.text
    conflict = client.patch(f"/entities/{second['id']}", headers=headers,
                            json={'name': 'Must not persist', 'attributes': attributes})
    assert conflict.status_code == 409
    assert client.get(f"/entities/{second['id']}", headers=headers).json()['name'] == 'Example'
    assert client.patch(url, headers=headers, json={'attributes': attributes}).status_code == 200
    assert client.patch(url, headers=headers, json={'attributes': {'description': 'Keep'}}).status_code == 200
    assert create(client, headers, attributes).status_code == 200


def test_null_clear_releases_path_and_nonprojects_refuse_it(client, headers):
    attributes = {'repository_path': 'projects/app'}
    project = create(client, headers, attributes).json()
    assert client.patch(f"/entities/{project['id']}", headers=headers,
                        json={'attributes': None}).status_code == 200
    assert create(client, headers, attributes).status_code == 200
    assert create(client, headers, attributes, type='person').status_code == 422


@pytest.mark.parametrize('operation', ['create', 'update'])
def test_concurrent_assignments_have_one_winner(client, headers, operation):
    ids = [create(client, headers).json()['id'] for _ in range(4)]
    ready = Barrier(4)

    def assign(entity_id):
        ready.wait(timeout=10)
        attributes = {'repository_path': 'projects/concurrent'}
        if operation == 'create':
            return create(client, headers, attributes).status_code
        return client.patch(f'/entities/{entity_id}', headers=headers,
                            json={'attributes': attributes}).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sorted(pool.map(assign, ids)) == [200, 409, 409, 409]
    projects = client.get('/entities?type=project', headers=headers).json()['items']
    assert sum(p['attributes'].get('repository_path') == 'projects/concurrent'
               for p in projects) == 1


@pytest.mark.parametrize('operation', ['create', 'update'])
def test_assignment_check_holds_writer_lock(client, headers, test_settings, monkeypatch, operation):
    """A removed BEGIN fails deterministically, independent of race scheduling."""
    import api.entities

    project = create(client, headers).json()
    original = api.entities._check_repository_assignment
    checks = []

    def check(connection, *args):
        with sqlite3.connect(test_settings.db_path, timeout=0) as contender:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                contender.execute('BEGIN IMMEDIATE')
        checks.append(True)
        return original(connection, *args)

    monkeypatch.setattr(api.entities, '_check_repository_assignment', check)
    attributes = {'repository_path': 'projects/locked'}
    if operation == 'create':
        response = create(client, headers, attributes)
    else:
        response = client.patch(f"/entities/{project['id']}", headers=headers,
                                json={'attributes': attributes})
    assert response.status_code == 200
    assert checks == [True]
