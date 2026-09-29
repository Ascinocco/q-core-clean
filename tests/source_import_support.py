"""Synthetic fixture imports through the CURRENT production preview/commit API.

Not a legacy endpoint or dedupe implementation. Each literal test batch is a
synthetic source; its listed positions are its physical records. Distinct
batches explicitly attest distinct test purchases. Real-source imports must
never derive identity from extracted rows or infer new/link decisions this way.
The old-shaped result is only a convenience for report/classifier fixtures.
"""
import hashlib
import json as json_module
import httpx


def source_payload(batch):
    source_id = hashlib.sha256(json_module.dumps(batch, sort_keys=True).encode()).hexdigest()
    return {**batch, 'source_id': source_id, 'actor': 'synthetic-test-fixture',
            'transactions': [{**r, 'source_row': f'fixture:record:{i}',
                              'decision': 'new', 'reason': 'Distinct record in authored synthetic fixture'}
                             for i, r in enumerate(batch['transactions'])]}


def fixture_result(result, rows):
    return {**result, 'statement_id': rows[0]['statement_id'] if rows else None,
            'skipped_duplicates': result['recognized'] + result['linked'],
            'unmatched': [r for r, action in zip(rows, result['rows'])
                          if action['action'] == 'new' and r['category_id'] is None]}


def seed_source_batch(client, *, json, headers=None):
    payload = source_payload(json)
    preview = client.post('/source_imports/preview', json=payload, headers=headers)
    if preview.status_code != 200:
        return preview
    result = client.post('/source_imports/commit',
                         json={**payload, 'review_token': preview.json()['review_token']}, headers=headers)
    if result.status_code != 200:
        return result
    body = result.json()
    rows = [client.get(f"/transactions/{r['transaction_id']}", headers=headers).json() for r in body['rows']]
    return httpx.Response(200, json=fixture_result(body, rows))


def seed_source_tool(call_tool, server, batch):
    payload = source_payload(batch)
    preview = call_tool(server, 'preview_source_import', {'payload': payload})
    result = call_tool(server, 'commit_source_import', {'payload': {**payload, 'review_token': preview['review_token']}})
    listing = call_tool(server, 'list_transactions', {'account_id': batch['account_id'], 'all': True})
    by_id = {r['id']: r for r in listing['items']}
    rows = [by_id[r['transaction_id']] for r in result['rows']]
    return fixture_result(result, rows)
