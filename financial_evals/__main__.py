"""Run with python -m financial_evals. Artifacts contain private financial data."""

import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from api.config import Settings
from financial_evals.core import (classify, compare, digest, extraction,
                                  load, legacy_replay, replay_reviewed, save, version)
from financial_evals.packets import prepare
from financial_evals.scenarios import synthetic_scenarios
from financial_evals.review import collect_review


def page_all(client, path):
    rows, total = [], None
    while total is None or len(rows) < total:
        response = client.get(path, params={'offset': len(rows), 'limit': 200})
        response.raise_for_status()
        page = response.json()
        if total is not None and page['total'] != total:
            raise ValueError('Live data changed during export; retry while imports are idle')
        total = page['total']
        if not page['items'] and len(rows) < total:
            raise ValueError('Truncated API export')
        rows.extend(page['items'])
    if len({r['id'] for r in rows}) != len(rows) or len(rows) != total:
        raise ValueError('Duplicate or missing rows during export')
    return sorted(rows, key=lambda row: row['id'])


def capture(env_file):
    """GET-only capture; two identical exports detect ordinary concurrent edits.

    This is not a transactional snapshot. Do not run during imports or edits.
    Names, addresses and entity attributes are never exported.
    """
    settings = Settings(_env_file=env_file)
    fields = {'entities': ('id', 'type'), 'categories': ('id', 'name', 'parent_id'),
              'rules': ('id', 'pattern', 'category_id', 'entity_id'),
              'statements': ('id', 'account_id', 'period_start', 'period_end'),
              'transactions': ('id', 'account_id', 'statement_id', 'txn_date', 'description', 'amount_cents', 'category_id', 'entity_id')}
    paths = {'rules': '/merchant_rules'}
    with httpx.Client(base_url=f'http://127.0.0.1:{settings.port}',
                      headers={'Authorization': f'Bearer {settings.api_token}'}, timeout=30) as client:
        def export():
            return {key: [{field: row.get(field) for field in columns}
                          for row in page_all(client, paths.get(key, '/' + key))]
                    for key, columns in fields.items()}
        first, second = export(), export()
    if first != second:
        raise ValueError('Live data changed between exports; baseline was not saved')
    if not first['transactions']:
        raise ValueError('Refusing an empty financial baseline')
    statement_ids = {s['id'] for s in first['statements']}
    if any(t['statement_id'] not in statement_ids for t in first['transactions']):
        raise ValueError('Transactions reference statements outside the captured active ledger')
    return {**first, 'schema_version': 1,
            'metadata': {'captured_at': datetime.now(timezone.utc).isoformat(),
                         'review_basis': 'Operator-accepted current database; record the actual review basis.',
                         'scope': 'Active ledger; previously suppressed source rows are unavailable.',
                         'export_consistency': 'Two equal GET-only exports; not an atomic DB snapshot.',
                         'export_implementation': version()}}


def inventory(root):
    """Local-only inspection: retain hashes and structural facts, never PDF text.

    Source filenames/paths remain private; printed CLI output is counts only.
    A reference label is evidence of a field, NOT proof of stable unique IDs.
    """
    import re
    from pypdf import PdfReader

    result = []
    for path in sorted(Path(root).rglob('*')):
        if not path.is_file() or path.suffix.lower() not in {'.pdf', '.csv'}:
            continue
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        item = {'source_id': sha, 'relative_path': str(path.relative_to(root)), 'format': path.suffix.lower(),
                'source_verified': False, 'privacy_reviewed': False}
        if path.suffix.lower() == '.csv':
            with path.open(encoding='utf-8-sig', newline='') as stream:
                reader = csv.DictReader(stream)
                headers = reader.fieldnames or []
                expected = ['Posted', 'Kind', 'Payee', 'Note', 'Value']
                item['known_csv_layout'] = set(headers) == set(expected)
                item['columns'] = [h if h in expected else 'unrecognized_column' for h in headers]
                item['rows'] = sum(1 for _ in reader)
        else:
            reader = PdfReader(path)
            pages = [p.extract_text() or '' for p in reader.pages]
            item['pages'] = len(pages)
            item['empty_pages'] = sum(not p.strip() for p in pages)
            item['reference_label_count'] = sum(len(re.findall(r'\b(?:transaction\s*(?:id|number)|reference\s*(?:number|no\.?|#)|confirmation\s*(?:number|no\.?))\b', p, flags=re.I)) for p in pages)
            item['identity_evidence'] = 'Unverified; reference-label counts do not establish uniqueness or persistence across exports.'
        result.append(item)
    return {'schema_version': 1, 'sources': result,
            'privacy_note': 'No raw PDF text or CSV cell values retained. Model-facing extraction packets need separate privacy review.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    publication = commands.add_parser('publish', help='Publish a minimized summary for the read-only eval page')
    publication.add_argument('--report', required=True)
    publication.add_argument('--corpus', choices=('development', 'synthetic', 'holdout'), required=True)
    publication.add_argument('--summary-dir', required=True)
    intake_packet = commands.add_parser('prepare-intake', help='Current source-aware offline trial packet from approved input')
    intake_packet.add_argument('--packet', required=True)
    intake_eval = commands.add_parser('intake-eval', help='Score actual source-aware trials through disposable production routes')
    intake_eval.add_argument('--suite', required=True)
    intake_eval.add_argument('--submissions', required=True)
    intake_freeze = commands.add_parser('freeze-intake-suite', help='Freeze reviewed packets, references and labels without live calls')
    intake_freeze.add_argument('--cases', required=True, help='JSON array of case_id, packet_path, reference_path; paths relative to this file')
    intake_freeze.add_argument('--ledger', required=True)
    intake_freeze.add_argument('--account-mapping', required=True)
    freeze = commands.add_parser('freeze')
    freeze.add_argument('--env-file', required=True)
    inv = commands.add_parser('inventory')
    inv.add_argument('--root', required=True)
    review = commands.add_parser('review-bundle')
    review.add_argument('--env-file', required=True)
    review.add_argument('--root', required=True)
    review.add_argument('--inventory', required=True)
    packet = commands.add_parser('prepare')
    packet.add_argument('--text', required=True, help='Already scrubbed and locally reviewed text, never a raw statement')
    packet.add_argument('--context', required=True)
    packet.add_argument('--source-id', required=True)
    packet.add_argument('--privacy-reviewed', action='store_true')
    packet.add_argument('--privacy-authorization', help='Explicit operator authorization bound to scrubbed text SHA-256')
    commands.add_parser('legacy-synthetic', aliases=['synthetic'], help='Historical retired importer only (in-memory simulator)')
    commands.add_parser('source-dedupe')
    for name in ('classify', 'replay', 'legacy-dedupe', 'extract-score'):
        p = commands.add_parser(name, aliases=['dedupe'] if name == 'legacy-dedupe' else [])
        p.add_argument('--reference', required=True)
        if name == 'classify':
            p.add_argument('--rules', help='Candidate rules JSON array; never changes frozen labels')
        if name == 'legacy-dedupe':
            p.add_argument('--scenarios', required=True)
        if name == 'extract-score':
            p.add_argument('--submission', required=True)
    comparison = commands.add_parser('compare')
    comparison.add_argument('--before', required=True)
    comparison.add_argument('--after', required=True)
    for command in dict.fromkeys(commands.choices.values()):
        if command is publication:
            continue
        command.add_argument('--output', required=True, help='New private JSON artifact; existing files are refused')
    args = parser.parse_args()
    if args.command == 'publish':
        from financial_evals.publishing import publish
        result = publish(load(args.report), args.corpus, Path(args.summary_dir))
        print(json.dumps({'report_hash': result['report_hash'], 'status': result['status']}))
        return
    if args.command == 'freeze-intake-suite':
        from financial_evals.intake_flow import freeze_suite
        base = Path(args.cases).resolve().parent
        cases = [{'case_id': c['case_id'], 'packet':load(base/c['packet_path']), 'reference':load(base/c['reference_path'])}
                 for c in load(args.cases)]
        result = freeze_suite(cases,load(args.ledger),load(args.account_mapping))
    elif args.command == 'prepare-intake':
        from financial_evals.intake_flow import prepare_trial
        result = prepare_trial(load(args.packet))
    elif args.command == 'intake-eval':
        from financial_evals.intake_flow import evaluate
        result = evaluate(load(args.suite), load(args.submissions))
    elif args.command == 'freeze':
        result = capture(args.env_file)
    elif args.command == 'inventory':
        result = inventory(args.root)
    elif args.command == 'review-bundle':
        settings = Settings(_env_file=args.env_file)
        with httpx.Client(base_url=f'http://127.0.0.1:{settings.port}',
                          headers={'Authorization': f'Bearer {settings.api_token}'}, timeout=30) as client:
            result = collect_review(client, args.root, load(args.inventory))
    elif args.command == 'compare':
        result = compare(load(args.before), load(args.after))
    elif args.command == 'prepare':
        result = prepare(Path(args.text).read_text(), load(args.context), args.source_id, args.privacy_reviewed,
                         load(args.privacy_authorization) if args.privacy_authorization else None)
    elif args.command == 'source-dedupe':
        from financial_evals.source_dedupe import evaluate
        result = evaluate(load(Path(__file__).parent / 'fixtures/reference.json'))
    elif args.command in ('legacy-synthetic', 'synthetic'):
        result = legacy_replay(load(Path(__file__).parent / 'fixtures/reference.json'), synthetic_scenarios())
    else:
        reference = load(args.reference)
        if args.command == 'classify':
            result = classify(reference, load(args.rules) if args.rules else None)
        elif args.command == 'replay':
            result = replay_reviewed(reference)
        elif args.command in ('legacy-dedupe', 'dedupe'):
            result = legacy_replay(reference, load(args.scenarios))
        else:
            result = extraction(reference, load(args.submission))
    save(args.output, result)
    print(json.dumps({'artifact': str(Path(args.output).resolve()), 'sha256': digest(result),
                      'metrics': result.get('metrics'), 'implementation_scope': result.get('implementation_scope', 'current'),
                      'source_count': len(result.get('sources', []))}, indent=2))
    if args.command == 'compare' and result['regressions']:
        raise SystemExit(1)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Validation/JSON/HTTP exceptions can quote financial values, source
        # filenames or credentials. Never emit their repr or a traceback to
        # a model-facing terminal. Inspect private inputs locally instead.
        raise SystemExit('Evaluation refused: check inputs, review attestations, API availability and unused output path locally.') from None
