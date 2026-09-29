import copy
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

AUTH = {'Authorization':'Bearer test-token'}


def setup(client):
    account=client.post('/entities',headers=AUTH,json={'type':'account','name':'Synthetic'}).json()['id']
    return {'account_id':account,'period_start':'2026-06-01','period_end':'2026-06-30',
            'source_id':'a'*64,'actor':'test','transactions':[{'txn_date':'2026-06-15','description':'CAFE','amount_cents':-500,'source_row':'csv:1'}]}


def preview(client,body):
    response=client.post('/source_imports/preview',headers=AUTH,json=body)
    assert response.status_code==200,response.text
    return response.json()


def commit(client,body,token=None):
    return client.post('/source_imports/commit',headers=AUTH,json={**body,'review_token':token or preview(client,body)['review_token']})


def rows(client):
    return client.get('/transactions',headers=AUTH).json()['items']


def test_same_source_retry_and_changed_description_preserve_id_and_facts(client,test_settings):
    body=setup(client)
    assert preview(client,body)['rows'][0]['status']=='new'
    assert rows(client)==[]
    first=commit(client,body);assert first.status_code==200
    before=rows(client)
    body['transactions'][0]['description']='CAFE #12'
    assert preview(client,body)['rows'][0]['status']=='known_source'
    result=commit(client,body).json()
    assert (result['created'],result['recognized'])==(0,1)
    assert rows(client)==before
    with sqlite3.connect(test_settings.db_path) as db:
        assert db.execute('SELECT COUNT(*) FROM source_import_rows').fetchone()[0]==1


@pytest.mark.parametrize('description',['CAFE','CAFE #12'])
@pytest.mark.parametrize('decision',['new','link'])
def test_cross_source_identical_or_changed_description_requires_evidence(client,description,decision):
    body=setup(client);assert commit(client,body).status_code==200
    existing=rows(client)[0]
    body['source_id']='b'*64;body['transactions'][0]['description']=description
    report=preview(client,body)
    assert report['needs_review']==1
    assert commit(client,body).status_code==409
    assert len(rows(client))==1
    body['transactions'][0].update(decision=decision,reason='Synthetic author confirms source occurrence identity')
    if decision=='link':body['transactions'][0]['transaction_id']=existing['id']
    response=commit(client,body,report['review_token'])
    assert response.status_code==200,response.text
    assert response.json()['created']==(1 if decision=='new' else 0)
    assert client.get(f"/transactions/{existing['id']}",headers=AUTH).json()==existing


def test_identical_occurrences_survive_and_cannot_link_to_one_target(client):
    body=setup(client);body['transactions'].append({**body['transactions'][0],'source_row':'csv:2'})
    assert commit(client,body).json()['created']==2
    assert commit(client,body).json()['recognized']==2
    body['source_id']='b'*64
    target=rows(client)[0]['id']
    for row in body['transactions']:row.update(decision='link',transaction_id=target,reason='bad duplicate mapping')
    assert commit(client,body).status_code==409
    assert len(rows(client))==2


def test_changed_money_archived_target_and_conflicting_link_are_refused(client):
    body=setup(client);commit(client,body);target=rows(client)[0]
    changed=copy.deepcopy(body);changed['transactions'][0]['amount_cents']=-501
    assert preview(client,changed)['source_conflicts']==1
    assert commit(client,changed).status_code==409
    changed=copy.deepcopy(body);changed['transactions'][0].update(decision='link',transaction_id='missing',reason='test')
    assert commit(client,changed).status_code==409
    client.post(f"/transactions/{target['id']}/archive",headers=AUTH)
    assert commit(client,body).status_code==409


def test_stale_preview_and_concurrent_commits_are_atomic(client,test_settings):
    body=setup(client);token=preview(client,body)['review_token']
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _:commit(client,body,token),range(2)))
    assert sorted(r.status_code for r in responses)==[200,409]
    assert len(rows(client))==1
    with sqlite3.connect(test_settings.db_path) as db:
        assert db.execute('SELECT COUNT(*) FROM source_import_rows').fetchone()[0]==1


def test_whole_batch_refuses_unresolved_row_without_creating_statement(client):
    body=setup(client);commit(client,body)
    body['source_id']='b'*64;body['period_start']='2026-06-02'
    body['transactions'].insert(0,{**body['transactions'][0],'source_row':'csv:2','amount_cents':-999})
    assert commit(client,body).status_code==409
    assert len(rows(client))==1
    assert client.get('/statements',headers=AUTH).json()['total']==1


def test_duplicate_mapping_across_batches_hits_unique_constraint_and_rolls_back(client,test_settings):
    body=setup(client);commit(client,body);target=rows(client)[0]['id']
    body['transactions'][0].update(source_row='csv:2',decision='link',transaction_id=target,reason='same target')
    assert commit(client,body).status_code==409
    with sqlite3.connect(test_settings.db_path) as db:
        assert db.execute('SELECT COUNT(*) FROM source_import_rows').fetchone()[0]==1


@pytest.mark.parametrize('change',[{'source_id':'filename.csv'}, {'actor':' '}, {'transactions':[]}])
def test_bad_requests_refused(client,change):
    body=setup(client)
    assert client.post('/source_imports/preview',headers=AUTH,json={**body,**change}).status_code==422


def test_unknown_link_and_auth_refused(client):
    body=setup(client);body['transactions'][0].update(decision='link',transaction_id='missing',reason='test')
    assert commit(client,body).status_code==409
    assert client.post('/source_imports/commit',json=body).status_code==401
    assert client.post('/source_imports/preview',json=body).status_code==401


def test_receipt_failure_rolls_back_new_statement_and_transaction(client,test_settings):
    body=setup(client)
    with sqlite3.connect(test_settings.db_path) as db:
        db.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON source_import_rows BEGIN SELECT RAISE(ABORT,'test'); END")
    assert commit(client,body).status_code==409
    assert rows(client)==[]
    assert client.get('/statements',headers=AUTH).json()['total']==0


def test_archived_parent_is_not_a_valid_link_even_with_active_child(client,test_settings):
    body=setup(client);commit(client,body);target=rows(client)[0]
    with sqlite3.connect(test_settings.db_path) as db:
        db.execute("UPDATE statements SET archived_at=CURRENT_TIMESTAMP WHERE id=?",(target['statement_id'],))
    assert commit(client,body).status_code==409
    body['source_id']='b'*64
    body['transactions'][0].update(decision='link',transaction_id=target['id'],reason='test')
    assert commit(client,body).status_code==409


def test_rule_change_invalidates_preview(client):
    body=setup(client);token=preview(client,body)['review_token']
    response=client.post('/merchant_rules',headers=AUTH,json={'pattern':'CAFE','category_id':'food_dining','actor':'test'})
    assert response.status_code==200
    assert commit(client,body,token).status_code==409


@pytest.mark.parametrize('decision',['new','link'])
def test_date_shifted_duplicate_from_another_source_needs_review_not_new(client,decision):
    # An existing row dated D; another export posts the same charge on D-1.
    body=setup(client);assert commit(client,body).status_code==200
    existing=rows(client)[0]
    body['source_id']='b'*64;body['transactions'][0]['txn_date']='2026-06-14'
    report=preview(client,body)
    row=report['rows'][0]
    assert (row['status'],row['match'])==('needs_review','near_date')
    assert [(c['id'],c['days_apart']) for c in row['candidates']]==[(existing['id'],1)]
    assert commit(client,body,report['review_token']).status_code==409
    assert len(rows(client))==1
    body['transactions'][0].update(decision=decision,reason='Synthetic author: other export dates the same charge a day earlier')
    if decision=='link':body['transactions'][0]['transaction_id']=existing['id']
    response=commit(client,body,report['review_token'])
    assert response.status_code==200,response.text
    assert response.json()['created']==(1 if decision=='new' else 0)
    assert client.get(f"/transactions/{existing['id']}",headers=AUTH).json()==existing
    if decision=='link':
        # Re-importing the same source recognises the near-date link, not a conflict.
        body['transactions'][0].pop('decision');body['transactions'][0].pop('transaction_id');body['transactions'][0].pop('reason')
        again=preview(client,body)['rows'][0]
        assert again['status']=='known_source'
        assert commit(client,body).json()['recognized']==1
        assert len(rows(client))==1


@pytest.mark.parametrize('day,expected',[('2026-06-12','needs_review'),('2026-06-18','needs_review'),
                                         ('2026-06-11','new'),('2026-06-19','new')])
def test_near_date_window_is_three_days_either_side(client,day,expected):
    body=setup(client);commit(client,body)
    body['source_id']='b'*64;body['transactions'][0]['txn_date']=day
    assert preview(client,body)['rows'][0]['status']==expected


def test_exact_date_candidates_come_first_and_set_match(client):
    body=setup(client);commit(client,body)
    shifted=copy.deepcopy(body);shifted['source_id']='c'*64;shifted['transactions'][0].update(txn_date='2026-06-16',decision='new',reason='Synthetic distinct purchase next day')
    assert commit(client,shifted).status_code==200
    body['source_id']='b'*64
    row=preview(client,body)['rows'][0]
    assert row['match']=='exact'
    assert [c['days_apart'] for c in row['candidates']]==[0,1]
