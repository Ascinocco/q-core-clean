"""Frozen references, production-code replay, and multiset scoring.

Only synthetic/previously exported data enter the in-memory SQLite connection.
No connection to the production database is opened by the evaluator.
"""

import hashlib
import copy
import importlib.metadata
import json
import os
import sqlite3
import subprocess
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth import require_token
from api.config import REPO_ROOT
from api.db import get_connection
from api.financial import _classify, _match_merchant_rule, router
from api.models import ImportStatementRequest
from api.source_imports import router as source_router

ROW_FIELDS = ('account_id', 'txn_date', 'description', 'amount_cents')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    """New artifacts only: never replace a baseline or run in place."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write('\n')


def version():
    def git(*args):
        return subprocess.check_output(['git', '-C', str(REPO_ROOT), *args], text=True).strip()
    files = {}
    for directory in ('api', 'financial_evals', 'db', 'plugin/skills/statement-intake'):
        for path in sorted((REPO_ROOT / directory).rglob('*')):
            if path.is_file() and path.suffix in {'.py', '.sql', '.md', '.json'}:
                files[str(path.relative_to(REPO_ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ('runbooks/statement-intake.md', 'runbooks/merchant-rules-conventions.md'):
        files[name] = hashlib.sha256((REPO_ROOT / name).read_bytes()).hexdigest()
    return {'git_commit': git('rev-parse', 'HEAD'),
            'dirty': bool(git('status', '--porcelain', '--untracked-files=normal')),
            'source_hash': digest(files), 'source_files': files,
            'dependencies': {name: importlib.metadata.version(name) for name in ('pydantic', 'pypdf', 'python-dateutil')}}


def artifact(kind, dataset, result):
    return {'schema_version': 1, 'kind': kind, 'created_at': datetime.now(timezone.utc).isoformat(),
            'dataset_hash': digest(dataset), 'implementation': version(), **result}


@contextmanager
def sandbox(reference):
    """Use production HTTP routes, with only a disposable connection available."""
    db = sqlite3.connect(':memory:', check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys = ON')
    db.executescript((REPO_ROOT / 'db/schema.sql').read_text())
    try:
        for entity in reference['entities']:
            db.execute('INSERT INTO entities (id,type,name) VALUES (?,?,?)',
                       (entity['id'], entity['type'], entity['id']))
        categories = reference['categories']
        for category in sorted(categories, key=lambda c: bool(c['parent_id'])):
            db.execute('INSERT INTO categories (id,name,parent_id) VALUES (?,?,?)',
                       (category['id'], category['name'], category['parent_id']))
        for rule in reference['rules']:
            db.execute('INSERT INTO merchant_rules (id,pattern,category_id,entity_id) VALUES (?,?,?,?)',
                       tuple(rule.get(k) for k in ('id', 'pattern', 'category_id', 'entity_id')))
        db.commit()
        app = FastAPI()
        app.include_router(router)
        app.include_router(source_router)
        app.dependency_overrides[require_token] = lambda: None
        app.dependency_overrides[get_connection] = lambda: db
        with TestClient(app) as client:
            yield db, client
    finally:
        db.close()


def row_key(row, fields=ROW_FIELDS):
    return tuple(row[field] for field in fields)


def ledger_score(expected, actual, fields=ROW_FIELDS):
    """Multisets preserve legitimate identical purchases and expose missing rows."""
    gold, predicted = Counter(row_key(r, fields) for r in expected), Counter(row_key(r, fields) for r in actual)
    missing, extra = gold - predicted, predicted - gold
    correct = sum((gold & predicted).values())
    return {'expected_rows': len(expected), 'actual_rows': len(actual), 'exact_rows': correct,
            'row_precision': correct / len(actual) if actual else None,
            'row_recall': correct / len(expected) if expected else None,
            'missing_rows': sum(missing.values()), 'extra_rows': sum(extra.values()),
            'net_cents_delta': sum(r['amount_cents'] for r in actual) - sum(r['amount_cents'] for r in expected),
            'missing': [{'row': dict(zip(fields, key)), 'count': count} for key, count in sorted(missing.items())],
            'extra': [{'row': dict(zip(fields, key)), 'count': count} for key, count in sorted(extra.items())]}


def classify(reference, candidate_rules=None):
    candidate = copy.deepcopy(reference)
    if candidate_rules is not None:
        candidate['rules'] = candidate_rules
    parents = {c['id']: c['parent_id'] or c['id'] for c in reference['categories']}
    results = []
    with sandbox(candidate) as (db, _):
        for row in reference['transactions']:
            predicted, entity = _classify(db, row['description'])
            matched = _match_merchant_rule(db, row['description'])
            expected = row['category_id']
            expected_top, predicted_top = parents.get(expected), parents.get(predicted)
            severe = expected is not None and predicted is not None and expected_top != predicted_top and bool(
                {expected_top, predicted_top} & {'transfers', 'income'})
            results.append({'case_id': row['id'], 'account_id': row['account_id'],
                            'expected': expected, 'predicted': predicted, 'predicted_entity': entity,
                            'rule_id': matched['id'] if matched else None,
                            'label_known': expected is not None,
                            'correct': expected == predicted if expected is not None else None,
                            'top_level_correct': expected_top == predicted_top if expected is not None else None,
                            'severe_error': severe})
    known = [r for r in results if r['label_known']]
    automated = [r for r in known if r['predicted'] is not None]
    metrics = {'cases': len(results), 'labeled_cases': len(known), 'unlabeled_cases': len(results) - len(known),
               'automatic_decisions_on_labeled': len(automated),
               'coverage': len(automated) / len(known) if known else None,
               'accuracy': sum(r['correct'] for r in known) / len(known) if known else None,
               'automatic_precision': sum(r['correct'] for r in automated) / len(automated) if automated else None,
               'top_level_accuracy': sum(r['top_level_correct'] for r in known) / len(known) if known else None,
               'severe_errors': sum(r['severe_error'] for r in results)}
    slices = {}
    for field in ('account_id', 'expected'):
        slices[field] = {}
        for key in sorted({str(r[field]) for r in results}):
            group = [r for r in known if str(r[field]) == key]
            slices[field][key] = {'labeled_cases': len(group), 'correct': sum(r['correct'] for r in group),
                                 'abstentions': sum(r['predicted'] is None for r in group)}
    return artifact('classification', reference, {'metrics': metrics, 'cases': results, 'slices': slices,
                    'candidate_rules_hash': digest(candidate['rules']),
                    'interpretation': 'Agreement with reviewed historical ledger, not a prospective holdout.'})


def legacy_replay(reference, scenarios):
    """Frozen LEGACY simulator, historical evidence only, never production HTTP."""
    from financial_evals.legacy_import import historical_import
    results = []
    ids = [s['id'] for s in scenarios]
    if len(set(ids)) != len(ids):
        raise ValueError('Scenario IDs must be unique')
    for scenario in scenarios:
        with sandbox(reference) as (db, client):
            steps = []
            for index, payload in enumerate(scenario['imports']):
                validated = ImportStatementRequest.model_validate(payload)
                body = historical_import(validated, db)
                steps.append({'step': index, 'input_hash': digest(payload), 'created': body['created'],
                              'skipped_duplicates': body['skipped_duplicates']})
            actual = [dict(row) for row in db.execute('SELECT * FROM active_transactions')]
            score = ledger_score(scenario['expected'], actual)
            results.append({'case_id': scenario['id'], 'steps': steps, **score,
                            'passed': score['missing_rows'] == score['extra_rows'] == 0})
    return artifact('deduplication', {'reference': reference, 'scenarios': scenarios},
                    {'implementation_scope': 'retired legacy simulator (in-memory only), not the production importer',
                     'metrics': {'scenarios': len(results), 'passed': sum(r['passed'] for r in results),
                                 'missing_rows': sum(r['missing_rows'] for r in results),
                                 'extra_rows': sum(r['extra_rows'] for r in results)}, 'cases': results})


def replay_reviewed(reference):
    """Current production replay; reviewed ledger IDs are OFFLINE fixture identity.

    Each reviewed row is an attested separate occurrence, not an inferred link.
    This is not source extraction or evidence that cross-export identity is known.
    """
    statements = {s['id']: s for s in reference['statements']}
    batches = []
    for statement in sorted(statements.values(), key=lambda s: (s['period_start'], s['id'])):
        reviewed_rows = [r for r in reference['transactions'] if r['statement_id'] == statement['id']]
        rows = [{**{k: r[k] for k in ('txn_date', 'description', 'amount_cents')},
                 # Source-row locators are transport identity, not reviewed
                 # ledger IDs. Keeping them short also prevents an opaque
                 # numeric fixture ID from looking like an account number to
                 # the production persistence guard exercised by this replay.
                 'source_row': f'reviewed:{position}', 'decision': 'new',
                 'reason': 'Reviewed ledger reference attests a separate occurrence'}
                for position, r in enumerate(reviewed_rows, start=1)]
        if rows:
            batches.append({k: statement[k] for k in ('account_id', 'period_start', 'period_end')} |
                           {'transactions': rows, 'source_id': digest(statement), 'actor': 'reviewed-ledger-eval'})
    results = []
    for name, inputs in [('forward', batches), ('reversed', list(reversed(batches))), ('reimport', batches + batches)]:
        with sandbox(reference) as (db, client):
            steps = []
            for body in inputs:
                preview = client.post('/source_imports/preview', json=body)
                preview.raise_for_status()
                response = client.post('/source_imports/commit', json={**body, 'review_token': preview.json()['review_token']})
                response.raise_for_status()
                steps.append(response.json())
            score = ledger_score(reference['transactions'], [dict(r) for r in db.execute('SELECT * FROM active_transactions')])
            results.append({'case_id': 'reviewed-ledger-' + name, 'steps': steps, **score,
                            'passed': score['missing_rows'] == score['extra_rows'] == 0})
    return artifact('source-reviewed-ledger-replay', reference,
                    {'metrics': {'scenarios': len(results), 'passed': sum(r['passed'] for r in results),
                                 'missing_rows': sum(r['missing_rows'] for r in results),
                                 'extra_rows': sum(r['extra_rows'] for r in results)}, 'cases': results,
                     'interpretation': 'Current production preview/commit; reviewed ledger fixture identities are not bank/source IDs.'})


def extraction(reference, submission):
    """Score a fresh agent submission; never infer source truth from deduped rows."""
    required = ('model', 'prompt_hash', 'skill_hash', 'tools', 'trial_id', 'input_hash')
    for key in required:
        if not submission.get('provenance', {}).get(key):
            raise ValueError(f'Missing extraction provenance: {key}')
    if submission['provenance']['input_hash'] != reference['input_hash']:
        raise ValueError('Extraction input differs from the reference input')
    for key in ('prompt_hash', 'skill_hash'):
        if reference.get(key) and reference[key] != submission['provenance'][key]:
            raise ValueError('Extraction trial contract differs from reference')
    from financial_evals.packets import authorized
    privacy_ok = reference.get('privacy_reviewed') or authorized(reference.get('privacy_authorization'), reference.get('text_sha256'))
    if not privacy_ok or not reference.get('source_verified'):
        raise ValueError('Extraction requires privacy-reviewed input and source-verified expected rows')
    predicted, expected = [], []
    for target, batches in ((predicted, submission['statements']), (expected, reference['statements'])):
        for payload in batches:
            valid = ImportStatementRequest.model_validate(payload).model_dump(mode='json')
            for row in valid['transactions']:
                target.append({**row, **{k: valid[k] for k in ('account_id', 'period_start', 'period_end')}})
    rows = ledger_score(expected, predicted, ('account_id', 'period_start', 'period_end', 'txn_date', 'description', 'amount_cents'))
    periods = lambda batches: Counter((b['account_id'], b['period_start'], b['period_end']) for b in batches)
    fields = ('account_id', 'period_start', 'period_end', 'txn_date', 'description', 'amount_cents')
    # Marginal agreement diagnoses fields, not full rows: swapped amounts
    # can score perfectly here but still fail exact-row scoring.
    field_scores = {field: ledger_score(expected, predicted, (field,)) for field in fields}
    return artifact('extraction', reference, {'provenance': submission['provenance'],
                    'submission_hash': digest(submission), 'metrics': {k: v for k, v in rows.items() if k not in ('missing', 'extra')},
                    'periods_match': periods(reference['statements']) == periods(submission['statements']),
                    'field_agreement': {k: {m: v[m] for m in ('row_precision', 'row_recall')} for k, v in field_scores.items()},
                    'missing': rows['missing'], 'extra': rows['extra'],
                    'interpretation': 'Scored agent output. This command does not invoke a model.'})


def compare(before, after):
    if before['kind'] != after['kind'] or before['dataset_hash'] != after['dataset_hash']:
        raise ValueError('Comparison requires the same suite and frozen dataset')
    # Synthetic source scenarios use id; extraction/ledger cases use case_id.
    old = {c['case_id'] if 'case_id' in c else c['id']: c for c in before.get('cases', [])}
    new = {c['case_id'] if 'case_id' in c else c['id']: c for c in after.get('cases', [])}
    higher = {'accuracy', 'automatic_precision', 'coverage', 'top_level_accuracy', 'passed', 'row_precision', 'row_recall'}
    lower = {'severe_errors', 'missing_rows', 'extra_rows', 'invalid_submissions', 'locator_errors',
             'classification_severe_errors', 'classification_unscorable', 'committed_missing_rows', 'committed_extra_rows',
             'source_fact_mismatches', 'persisted_classification_mismatches'}
    regressions = [key for key in higher | lower
                   if isinstance(before['metrics'].get(key), (int, float))
                   and isinstance(after['metrics'].get(key), (int, float))
                   and ((after['metrics'][key] < before['metrics'][key]) if key in higher else
                        (after['metrics'][key] > before['metrics'][key]))]
    if before.get('periods_match') and not after.get('periods_match', True):
        regressions.append('periods_match')
    return {'kind': before['kind'], 'dataset_hash': before['dataset_hash'],
            'regressions': sorted(regressions),
            'before': before['metrics'], 'after': after['metrics'],
            'extraction_errors': {'before': {k: before[k] for k in ('missing', 'extra') if k in before},
                                  'after': {k: after[k] for k in ('missing', 'extra') if k in after}},
            'changed_cases': [{'case_id': k, 'before': old.get(k), 'after': new.get(k)}
                              for k in sorted(old.keys() | new.keys()) if old.get(k) != new.get(k)]}
