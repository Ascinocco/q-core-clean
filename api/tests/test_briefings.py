"""Invented data only. Exercise records, not mocks of their persistence."""
from uuid import uuid4
import pytest

# split part 2: the read-only routes now need a credential like every route.
READER = {"Authorization": "Bearer test-token"}

HEADERS = {'Authorization': 'Bearer test-token'}


def document(title='Morning brief', html='<h1>Today</h1><p>Review the roof quote.</p>'):
    return {'title': title, 'html': html, 'period_start': '2026-09-21T00:00:00-04:00',
            'as_of': '2026-09-23T09:00:00-04:00', 'coverage': [
                {'source': 'gmail', 'status': 'unavailable', 'detail': 'Not connected in this fixture.'}]}


def create(client, **changes):
    body = dict(request_id=str(uuid4()), kind='daily', document=document(), actor='Test', note='Requested brief')
    body.update(changes)
    result = client.post('/artifacts', headers=HEADERS, json=body)
    assert result.status_code == 200, result.text
    return result.json(), body


def test_artifact_versions_retries_and_order(client):
    a, original = create(client)
    b, _ = create(client, document=document('Newer'))
    edit = dict(expected_revision=1, document=document('Revised'), actor='Test', note='Operator revision')
    response = client.put('/artifacts/'+a['id'], headers=HEADERS, json=edit)
    assert response.status_code == 200, response.text
    assert response.json()['revision'] == 2
    assert response.json()['created_at'] == a['created_at']
    assert client.put('/artifacts/'+a['id'], headers=HEADERS, json=edit).json()['revision'] == 2
    edit['document'] = document('Conflicting edit')
    assert client.put('/artifacts/'+a['id'], headers=HEADERS, json=edit).status_code == 409
    assert client.get('/artifacts/'+a['id']+'?revision=1', headers=HEADERS).json()['document']['title'] == 'Morning brief'
    assert client.post('/artifacts', headers=HEADERS, json=original).json()['revision'] == 1
    original['document'] = document('Changed request')
    assert client.post('/artifacts', headers=HEADERS, json=original).status_code == 409
    rows = client.get('/artifacts', headers=HEADERS).json()['items']
    assert [r['id'] for r in rows] == [b['id'], a['id']]
    assert client.get('/artifacts?limit=1&offset=1', headers=HEADERS).json()['items'][0]['id'] == a['id']


@pytest.mark.parametrize('html', ['<script>alert(1)</script>', '<p onclick="x()">test</p>',
    '<img src="https://example.com">', '<svg><script>x</script></svg>', '<iframe>text</iframe>',
    '<p style="background:url(https://example.com)">test</p>', '<a href="javascript:x">link</a>',
    '<p>1234<strong>56789012</strong></p>', '<p>&#49;&#50;&#51;&#52;&#53;&#54;&#55;&#56;&#57;</p>',
    '<p>Unbalanced', '<!-- secret -->', '<p>1234\u200b567890</p>'])
def test_artifact_refuses_active_or_sensitive_content(client, html):
    body = dict(request_id=str(uuid4()), kind='daily', document=document(html=html), actor='Test', note='Test')
    response = client.post('/artifacts', headers=HEADERS, json=body)
    assert response.status_code == 422, response.text
    assert html not in response.text
    assert client.get('/artifacts', headers=HEADERS).json()['total'] == 0


def test_private_profile_and_rich_content(client, test_settings):
    a, _ = create(client, document=document(html='<h1>Brief</h1><table><tr><th>Task</th></tr><tr><td>Review</td></tr></table><pre>Inbox → Decision</pre><p>Alex Example</p>'))
    assert 'Alex Example' not in a['document']['html']
    assert '[REDACTED]' in a['document']['html']
    assert client.get('/artifacts').status_code == 401
    assert client.get('/artifacts/'+a['id'], headers=HEADERS).json()['document'] == a['document']
    from pathlib import Path
    Path(test_settings.privacy_profile_path).unlink()
    response = client.post('/artifacts', headers=HEADERS, json=dict(request_id=str(uuid4()), kind='daily', document=document(), actor='Test', note='Test'))
    assert response.status_code >= 400


def test_review_resolution_replay_and_rediscovery(client):
    source_key = str(uuid4())
    body = {'source_key':source_key, 'content':{'summary':'Two renewal dates disagree.'}, 'actor':'Test', 'note':'Found discrepancy'}
    first = client.post('/review-items', headers=HEADERS, json=body).json()['item']
    change = {'expected_revision':1, 'content':first['content'], 'state':'resolved', 'actor':'Test', 'note':'Operator resolved without changing source; recorded date is correct.'}
    response = client.put('/review-items/'+first['id'], headers=HEADERS, json=change)
    assert response.status_code == 200, response.text
    assert response.json()['item']['state'] == 'resolved'
    assert client.put('/review-items/'+first['id'], headers=HEADERS, json=change).json()['replayed']
    repeated = client.post('/review-items', headers=HEADERS, json=body).json()
    assert repeated['created'] is False
    assert repeated['item']['state'] == 'resolved'
    assert client.get('/review-items?actionable=true', headers=HEADERS).json()['total'] == 0
    history = client.get('/review-history?item_id='+first['id'], headers=HEADERS).json()
    assert [r['state'] for r in history['items']] == ['open', 'resolved']
    change['note'] = 'Conflicting stale decision'
    assert client.put('/review-items/'+first['id'], headers=HEADERS, json=change).status_code == 409
    body['content']['summary'] = 'New evidence needs attention.'
    result = client.post('/review-items', headers=HEADERS, json=body).json()
    assert result['source_changed'] and result['item']['state'] == 'resolved'


def test_review_deferral_and_history_boundaries(client, test_settings):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    import sqlite3
    today = datetime.now(ZoneInfo('America/New_York')).date()
    body = {'source_key':str(uuid4()), 'content':{'summary':'Awaiting quote.'}, 'actor':'Test', 'note':'Captured'}
    item = client.post('/review-items', headers=HEADERS, json=body).json()['item']
    update = dict(expected_revision=1, content=item['content'], state='deferred', revisit_date=(today+timedelta(days=2)).isoformat(), actor='Test', note='Discuss later')
    assert client.put('/review-items/'+item['id'], headers=HEADERS, json=update).status_code == 200
    assert client.get('/review-items?actionable=true', headers=HEADERS).json()['total'] == 0
    # Synthetic time passage: persistent deferral is surfaced without mutating it.
    with sqlite3.connect(test_settings.db_path) as db:
        db.execute('UPDATE review_items SET revisit_date = ?', ((today-timedelta(days=1)).isoformat(),))
    rows = client.get('/review-items?actionable=true', headers=HEADERS).json()['items']
    assert rows[0]['state'] == 'deferred'
    bad = client.get('/review-history?date_from=2026-09-01', headers=HEADERS)
    assert bad.status_code == 422


def test_local_zone_dst_bounds():
    from datetime import date, datetime
    from api.briefing_data import utc_bounds
    lower, upper = utc_bounds(date(2026,3,8), date(2026,3,8))
    assert (datetime.fromisoformat(upper)-datetime.fromisoformat(lower)).total_seconds() == 23*3600
    lower, upper = utc_bounds(date(2026,11,1), date(2026,11,1))
    assert (datetime.fromisoformat(upper)-datetime.fromisoformat(lower)).total_seconds() == 25*3600


def test_completion_uses_completion_timestamp_not_due_date(client, test_settings):
    import sqlite3
    reminder = client.post('/reminders', headers=HEADERS, json={'title':'Inspect roof','start_date':'2026-09-01'}).json()
    with sqlite3.connect(test_settings.db_path) as db:
        db.execute("INSERT INTO reminder_instances (id,reminder_id,due_date,status,completed_at) VALUES (?,?,?,?,?)", (str(uuid4()),reminder['id'],'2026-09-01','done','2026-09-21 04:00:00'))
    response = client.get('/reminder-completions?date_from=2026-09-21&date_to=2026-09-23&as_of=2026-09-21T05:00:00Z', headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()['items'][0]['due_date'] == '2026-09-01'
    assert client.get('/reminder-completions?date_from=2026-09-20&date_to=2026-09-20', headers=HEADERS).json()['total'] == 0
    assert client.get('/reminder-completions?date_from=2026-09-21&date_to=2026-09-23&as_of=2026-09-21T03:00:00Z', headers=HEADERS).json()['total'] == 0


def test_expected_payments_include_past_and_unknown(client):
    plan = {'accounts':[{'key':'cash','label':'Cash','kind':'cash','balance_cents':10000,'as_of':'2026-09-01','evidence':'Invented snapshot'}],
            'runway_account':'cash','rules':[{'key':'bill','label':'Roof inspection','kind':'expense','account':'cash',
            'amount_cents':None,'cadence':'once','start':'2026-09-21','confidence':'unknown','evidence':'Awaiting quote'}]}
    response = client.put('/forecast/plan', headers=HEADERS, json={'plan':plan,'actor':'Test','note':'Synthetic assumptions'})
    assert response.status_code == 200, response.text
    response = client.get('/expected-payments?date_from=2026-09-21&date_to=2026-09-23', headers=HEADERS)
    assert response.status_code == 200, response.text
    event = response.json()['items'][0]
    assert event['amount_cents'] is None and event['confidence'] == 'unknown'
    assert event['basis'] == 'current_plan_expectation'


def test_viewer_collection_revision_and_isolation(client):
    assert 'No briefs here yet' in client.get('/ui/briefs', headers=READER).text
    a, _ = create(client)
    response = client.get('/ui/briefs/'+a['id'], headers=READER)
    assert response.status_code == 200
    assert 'sandbox=""' in response.text and 'srcdoc=' in response.text
    assert "default-src 'none'" in response.headers['content-security-policy']
    assert response.headers['cache-control'] == 'no-store'
    assert 'test-token' not in response.text
    assert 'Source coverage' in response.text and 'unavailable' in response.text
    assert '/ui/briefs/'+a['id'] in client.get('/ui/briefs', headers=READER).text
    assert 'Morning brief' not in client.get('/ui/briefs?kind=weekly', headers=READER).text
    assert client.get('/ui/briefs/'+a['id']+'?revision=2', headers=READER).status_code == 404
    assert client.post('/ui/briefs', json={}, headers=READER).status_code == 405


def test_private_alias_split_by_html_tags_is_not_stored(client):
    payload = dict(request_id=str(uuid4()), kind='daily', document=document(html='<p>Alex <strong>Example</strong></p>'), actor='Test', note='Test')
    assert client.post('/artifacts', headers=HEADERS, json=payload).status_code == 422


def test_concurrent_create_and_stale_revision_do_not_lose_work(client):
    from concurrent.futures import ThreadPoolExecutor
    # Bootstrap before exercising writer races.
    client.get('/artifacts', headers=HEADERS)
    payload = dict(request_id=str(uuid4()),kind='daily',document=document(),actor='Test',note='Same request')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:client.post('/artifacts',headers=HEADERS,json=payload),range(2)))
    assert [r.status_code for r in results]==[200,200]
    assert results[0].json()['id']==results[1].json()['id']
    aid=results[0].json()['id']
    def edit(title):
        return client.put('/artifacts/'+aid,headers=HEADERS,json={'expected_revision':1,'document':document(title),'actor':'Test','note':title})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(edit,['First edit','Second edit']))
    assert sorted(r.status_code for r in results)==[200,409]
    assert client.get('/artifacts/'+aid,headers=HEADERS).json()['current_revision']==2


def test_artifact_creation_rolls_back_if_revision_fails(client,test_settings):
    import sqlite3
    client.get('/artifacts',headers=HEADERS)
    with sqlite3.connect(test_settings.db_path) as db:
        db.execute("CREATE TRIGGER fail_revision BEFORE INSERT ON artifact_revisions BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    result=client.post('/artifacts',headers=HEADERS,json={'request_id':str(uuid4()),'kind':'daily','document':document(),'actor':'Test','note':'Requested'})
    assert result.status_code==500
    with sqlite3.connect(test_settings.db_path) as db:
        assert db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0]==0


def test_backup_contains_artifact_versions_and_review_history(client,test_settings,tmp_path):
    import sqlite3
    from api.artifacts import artifact
    saved,_=create(client)
    edit=dict(expected_revision=1,document=document('Updated'),actor='Test',note='Revision')
    assert client.put('/artifacts/'+saved['id'],headers=HEADERS,json=edit).status_code==200
    body={'source_key':str(uuid4()),'content':{'summary':'Awaiting a date.'},'actor':'Test','note':'Captured'}
    assert client.post('/review-items',headers=HEADERS,json=body).status_code==200
    with sqlite3.connect(test_settings.db_path) as source, sqlite3.connect(tmp_path/'restored.db') as target:
        source.backup(target)
    with sqlite3.connect(tmp_path/'restored.db') as restored:
        restored.row_factory=sqlite3.Row
        assert artifact(restored,saved['id'],1)['document']['title']=='Morning brief'
        assert artifact(restored,saved['id'])['document']['title']=='Updated'
        assert restored.execute('SELECT count(*) FROM review_item_history').fetchone()[0]==1


def test_deferral_rejects_past_date(client):
    body={'source_key':str(uuid4()),'content':{'summary':'Review quote.'},'actor':'Test','note':'Captured'}
    item=client.post('/review-items',headers=HEADERS,json=body).json()['item']
    update=dict(expected_revision=1,content=item['content'],state='deferred',revisit_date='2020-01-01',actor='Test',note='Defer')
    assert client.put('/review-items/'+item['id'],headers=HEADERS,json=update).status_code==422
    assert client.get('/review-items/'+item['id'],headers=HEADERS).json()['revision']==1


@pytest.mark.parametrize('kind,stamp,start,outlook', [
    ('daily', '2026-09-23T03:30:00+00:00', '2026-09-22T00:00:00-04:00', '2026-09-29'),
    ('weekly', '2026-09-21T04:01:00+00:00', '2026-09-21T00:00:00-04:00', '2026-09-27'),
    ('weekly', '2026-09-23T16:00:00+00:00', '2026-09-21T00:00:00-04:00', '2026-09-27'),
    ('weekly', '2026-09-28T03:59:00+00:00', '2026-09-21T00:00:00-04:00', '2026-09-27'),
])
def test_briefing_window_uses_local_week_without_saved_artifacts(client, monkeypatch, kind, stamp, start, outlook):
    from datetime import datetime
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(stamp).astimezone(tz)
    monkeypatch.setattr('api.briefing_data.datetime', Clock)
    assert client.get('/artifacts', headers=HEADERS).json()['total'] == 0
    response = client.get('/briefing-window', headers=HEADERS, params={'kind': kind})
    assert response.status_code == 200
    window = response.json()
    assert window['period_start'] == start
    assert window['outlook_to'] == outlook
    assert datetime.fromisoformat(window['as_of']) == datetime.fromisoformat(stamp)
