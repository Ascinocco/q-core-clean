"""Explicit-evidence scenarios for the production source-tracked path.

Refusals count as safety outcomes, not successful transaction recovery.
The legacy suite remains separate and its known gaps are not relabeled.
"""
from copy import deepcopy

from financial_evals.core import artifact, sandbox


def evaluate(reference):
    cases = []
    for name in ('same-source-retry', 'same-source-description-change', 'cross-source-description-review',
                 'confirmed-description-alias', 'identical-partial-review', 'confirmed-separate-purchase',
                 'identical-occurrences-in-source', 'different-account', 'different-amount',
                 'near-date-review', 'confirmed-near-date-link', 'date-outside-window'):
        with sandbox(reference) as (db, client):
            payload = {'account_id':'account-a','period_start':'2026-06-01','period_end':'2026-06-30',
                       'source_id':'a'*64,'actor':'synthetic-eval','transactions':[
                           {'source_row':'csv:1','txn_date':'2026-06-15','description':'CAFE','amount_cents':-500}]}
            def submit(body):
                preview = client.post('/source_imports/preview',json=body)
                assert preview.status_code == 200
                return client.post('/source_imports/commit',json={**body,'review_token':preview.json()['review_token']})
            if name == 'identical-occurrences-in-source':
                payload['transactions'].append({**payload['transactions'][0],'source_row':'csv:2'})
            initial = submit(payload)
            assert initial.status_code == 200
            before = [dict(r) for r in db.execute('SELECT * FROM active_transactions ORDER BY id')]
            later = deepcopy(payload)
            expected_count, expected_status = len(before), 200
            if name.startswith('same-source-description'):
                later['transactions'][0]['description']='CAFE #12'
            if name in ('cross-source-description-review','confirmed-description-alias','identical-partial-review','confirmed-separate-purchase'):
                later['source_id']='b'*64
                if 'description' in name:later['transactions'][0]['description']='CAFE #12'
                if name.endswith('review'):expected_status=409
                elif name=='confirmed-description-alias':
                    later['transactions'][0].update(decision='link',transaction_id=before[0]['id'],reason='Synthetic author: same purchase in another export')
                else:
                    later['transactions'][0].update(decision='new',reason='Synthetic author: genuinely distinct identical purchase')
                    expected_count=2
            if name=='different-account':later['account_id']='account-b';expected_count=2
            if name=='different-amount':
                later['source_id']='b'*64;expected_count=2
                later['transactions'][0]['amount_cents']=-501
            # Another export posting the same charge a day earlier (statement vs
            # effective date) is a candidate for review, never silently new.
            if name in ('near-date-review','confirmed-near-date-link'):
                later['source_id']='b'*64;later['transactions'][0]['txn_date']='2026-06-14'
                if name=='near-date-review':expected_status=409
                else:later['transactions'][0].update(decision='link',transaction_id=before[0]['id'],reason='Synthetic author: same charge, other export dates it a day earlier')
            if name=='date-outside-window':
                later['source_id']='b'*64;expected_count=2
                later['transactions'][0]['txn_date']='2026-06-25'
            response=submit(later)
            after=[dict(r) for r in db.execute('SELECT * FROM active_transactions ORDER BY id')]
            preserved=all(row in after for row in before)
            cases.append({'id':name,'expected_status':expected_status,'actual_status':response.status_code,
                          'expected_rows':expected_count,'actual_rows':len(after),'preexisting_rows_preserved':preserved,
                          'review_required':expected_status==409,
                          'passed':response.status_code==expected_status and len(after)==expected_count and preserved})
    return artifact('source-deduplication', {'reference':reference,'scenario_version':2},
                    {'metrics':{'scenarios':len(cases),'passed':sum(c['passed'] for c in cases),
                                'deliberately_blocked_for_review':sum(c['review_required'] for c in cases)},
                     'cases':cases,'interpretation':'Tests the current production importer with supplied source identity and explicit synthetic decisions; safe refusals do not resolve unknown identity. Legacy simulator is offline historical evidence only.'})
