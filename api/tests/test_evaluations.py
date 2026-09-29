import json
from pathlib import Path

import pytest

from financial_evals.publishing import publish, summarize

from api.tests.reader_routes import assert_reader_only_get

# split part 2: the read-only routes now need a credential like every route.
READER = {"Authorization": "Bearer test-token"}


def report():
    return {'kind': 'end-to-end-intake', 'dataset_hash': 'a'*64,
            'created_at': '2026-09-21T12:00:00+00:00',
            'implementation': {'git_commit': 'b'*40, 'source_hash': 'c'*64, 'dirty': False},
            'metrics': {'passed': 1, 'exact_rows': 250, 'missing_rows': 0, 'private_account_number': 123456},
            'cases': [{'text': 'PRIVATE STATEMENT', 'error': 'secret path'}]}


def test_summary_omits_private_fields_and_is_immutable(tmp_path):
    result = publish(report(), 'development', tmp_path)
    data = (tmp_path / (result['report_hash']+'.json')).read_text()
    assert 'PRIVATE' not in data and 'private_account' not in data and 'cases' not in data
    assert result['status'] == 'passed'
    assert (tmp_path / (result['report_hash']+'.json')).stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        publish(report(), 'development', tmp_path)


def test_status_distinguishes_scenario_count_and_measurement():
    value = report()
    value.update(kind='source-deduplication', metrics={'scenarios': 10, 'passed': 9})
    assert summarize(value, 'synthetic')['status'] == 'failed'
    value['metrics']['passed'] = 10
    assert summarize(value, 'synthetic')['status'] == 'passed'
    with pytest.raises(ValueError):
        summarize(value, 'development')
    value.update(kind='classification', metrics={'automatic_precision': 1, 'coverage': .98})
    assert summarize(value, 'development')['status'] == 'measured'


def test_read_only_summary_api_empty_valid_invalid_and_no_private_download(client, test_settings, tmp_path):
    test_settings.eval_summaries_dir = str(tmp_path / 'published')
    assert client.get('/evals/runs', headers=READER).json() == {'runs': [], 'skipped': 0}
    root = Path(test_settings.eval_summaries_dir)
    summary = publish(report(), 'development', root)
    response = client.get('/evals/runs', headers=READER)
    assert response.json()['runs'] == [summary]
    assert response.headers['cache-control'] == 'no-store'
    (root / ('d'*64+'.json')).write_text(json.dumps({**summary, 'raw_text': 'SECRET'}))
    private = tmp_path / 'raw.json'
    private.write_text('SECRET')
    (root / ('e'*64+'.json')).symlink_to(private)
    (root / ('f'*64+'.json')).write_text('x'*33000)
    response = client.get('/evals/runs', headers=READER)
    assert response.json()['skipped'] == 3
    assert response.json()['runs'] == [summary]
    assert 'SECRET' not in response.text
    assert client.get('/evals/runs/raw.json', headers=READER).status_code == 404
    assert client.post('/evals/runs', json={}, headers=READER).status_code == 405
    assert client.post('/entities', json={}).status_code == 401
    assert client.get('/ui/evals', headers=READER).status_code == 200


@pytest.mark.parametrize('field,value', [('metrics', {'coverage': float('nan')}),
                                       ('metrics', {'coverage': 'SECRET'}),
                                       ('created_at', 'SECRET'), ('corpus', 'SECRET')])
def test_api_rejects_invalid_summary_values(client, test_settings, tmp_path, field, value):
    test_settings.eval_summaries_dir = str(tmp_path)
    summary = summarize(report(), 'development')
    summary[field] = value
    (tmp_path / (summary['report_hash']+'.json')).write_text(json.dumps(summary))
    response = client.get('/evals/runs', headers=READER)
    assert response.json() == {'runs': [], 'skipped': 1}
    assert 'SECRET' not in response.text





def test_eval_routes_need_a_reader_and_are_get_only(client):
    from api.evaluations import router
    assert {r.path for r in router.routes} == {'/ui/evals', '/evals/runs'}
    assert_reader_only_get(router.routes)
    assert client.get('/evals/runs').status_code == 401
