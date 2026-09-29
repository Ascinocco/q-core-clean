import copy
import json
from pathlib import Path

import httpx
import pytest

from financial_evals.__main__ import page_all, inventory
from financial_evals.core import classify, compare, extraction, ledger_score, load, legacy_replay, replay_reviewed, save
from financial_evals.scenarios import synthetic_scenarios
from financial_evals.packets import prepare

FIXTURES = Path(__file__).resolve().parents[1] / 'financial_evals/fixtures'


def test_compare_current_source_scenarios_uses_stable_scenario_ids(reference):
    from financial_evals.source_dedupe import evaluate
    before = evaluate(reference)
    after = copy.deepcopy(before)
    after['metrics']['passed'] -= 1
    after['cases'][0]['passed'] = False
    result = compare(before, after)
    assert result['regressions'] == ['passed']
    assert result['changed_cases'][0]['case_id'] == before['cases'][0]['id']


@pytest.fixture
def reference():
    return load(FIXTURES / 'reference.json')


def test_classifier_matches_reviewed_labels_and_excludes_unknowns(reference):
    result = classify(reference)
    assert result['metrics']['automatic_precision'] == 1
    assert result['metrics']['labeled_cases'] == 3
    assert result['metrics']['unlabeled_cases'] == 1
    assert result['cases'][3]['predicted'] is None  # SHELL must not match ZUMSHELLA


@pytest.mark.parametrize('command', ['source-dedupe', 'legacy-synthetic', 'synthetic', 'legacy-dedupe', 'dedupe', 'replay'])
def test_cli_distinguishes_current_and_retired_importers(command, tmp_path, monkeypatch, capsys):
    from financial_evals.__main__ import main
    output = tmp_path/'result.json'
    args = ['financial_evals', command, '--output', str(output)]
    if command in ('replay', 'dedupe', 'legacy-dedupe'):
        args += ['--reference', str(FIXTURES/'reference.json')]
    if command in ('dedupe', 'legacy-dedupe'):
        scenarios = tmp_path/'scenarios.json'
        save(scenarios, synthetic_scenarios())
        args += ['--scenarios', str(scenarios)]
    monkeypatch.setattr('sys.argv', args)
    main()
    printed = json.loads(capsys.readouterr().out)
    historical = command in ('legacy-synthetic','synthetic','legacy-dedupe','dedupe')
    assert ('retired legacy simulator' in printed['implementation_scope']) is historical
    assert load(output)['metrics']['passed'] == {'replay': 3, 'source-dedupe': 12}.get(command, 10)


def test_severe_regression_is_visible(reference):
    reference['rules'][2]['category_id'] = 'income'
    result = classify(reference)
    assert result['metrics']['severe_errors'] == 1
    assert result['metrics']['automatic_precision'] == pytest.approx(2 / 3)


def test_import_scenarios_preserve_multiplicity_and_expose_known_limits(reference):
    report = legacy_replay(reference, synthetic_scenarios())
    assert 'retired legacy simulator' in report['implementation_scope']
    cases = {c['case_id']: c for c in report['cases']}
    assert all(c['passed'] for k, c in cases.items() if k not in {'known-gap-descriptor-variation', 'ambiguous-partial-exports'})
    assert cases['known-gap-descriptor-variation']['extra_rows'] == 1
    assert cases['ambiguous-partial-exports']['missing_rows'] == 1
    # Counts alone would hide these opposite failure modes.
    assert report['metrics']['missing_rows'] == report['metrics']['extra_rows'] == 1


def test_replay_uses_production_import_path(reference, monkeypatch):
    from api import financial
    original = financial._classify
    calls = []
    def observed(connection, description):
        calls.append(description)
        return original(connection, description)
    monkeypatch.setattr(financial, '_classify', observed)
    report = replay_reviewed(reference)
    assert report['metrics']['passed'] == 3
    assert calls


def test_replay_does_not_persist_reviewed_fixture_ids_as_source_locators(reference):
    reference['transactions'][0]['id'] = '1234567890123456'

    report = replay_reviewed(reference)

    assert report['metrics']['passed'] == 3


@pytest.mark.parametrize('attached', [False, True])
def test_legacy_simulator_refuses_disk_databases(tmp_path, attached):
    import sqlite3
    from api.models import ImportStatementRequest
    from financial_evals.legacy_import import historical_import
    db = sqlite3.connect(':memory:' if attached else str(tmp_path/'disk.db'))
    try:
        if attached:
            db.execute('ATTACH DATABASE ? AS extra', (str(tmp_path/'attached.db'),))
        body = ImportStatementRequest.model_validate(synthetic_scenarios()[0]['imports'][0])
        with pytest.raises(ValueError, match='in-memory'):
            historical_import(body, db)
    finally:
        db.close()


def test_same_net_total_can_still_be_completely_wrong():
    row = {'account_id': 'a', 'txn_date': '2026-06-01', 'description': 'CAFE', 'amount_cents': -100}
    wrong = {**row, 'description': 'FUEL'}
    score = ledger_score([row, row], [wrong, wrong])
    assert score['net_cents_delta'] == 0
    assert score['missing_rows'] == score['extra_rows'] == 2


def test_capture_drains_pagination():
    def handler(request):
        offset = int(request.url.params['offset'])
        return httpx.Response(200, json={'total': 201, 'items': [{'id': str(i)} for i in range(offset, min(offset + 200, 201))]})
    with httpx.Client(base_url='http://localhost', transport=httpx.MockTransport(handler)) as client:
        assert len(page_all(client, '/transactions')) == 201


def test_capture_refuses_changed_or_truncated_pages():
    with httpx.Client(base_url='http://localhost', transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={'total': 5, 'items': []}))) as client:
        with pytest.raises(ValueError, match='Truncated'):
            page_all(client, '/transactions')


def test_artifacts_are_private_and_cannot_be_overwritten(tmp_path):
    target = tmp_path / 'reference.json'
    save(target, {'ok': True})
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        save(target, {'ok': False})
    assert json.loads(target.read_text()) == {'ok': True}


def test_comparison_rejects_changed_reference(reference):
    before = classify(reference)
    changed = copy.deepcopy(reference)
    changed['transactions'][0]['category_id'] = 'auto'
    with pytest.raises(ValueError, match='same suite and frozen dataset'):
        compare(before, classify(changed))


def test_candidate_rules_compare_without_mutating_labels(reference):
    original = copy.deepcopy(reference)
    rules = copy.deepcopy(reference['rules'])
    rules[2]['category_id'] = 'income'
    diff = compare(classify(reference), classify(reference, rules))
    assert 'severe_errors' in diff['regressions']
    assert 'automatic_precision' in diff['regressions']
    assert len(diff['changed_cases']) == 1
    assert reference == original


def test_packet_requires_review_and_keeps_gold_separate():
    context = {'account_id': 'account-a', 'source_format': '.csv'}
    with pytest.raises(ValueError, match='privacy review'):
        prepare('CAFE -5.00', context, 'a' * 64)
    packet = prepare('CAFE -5.00', context, 'a' * 64, True)
    assert 'statements' not in packet
    assert len(packet['input_hash']) == 64
    assert len(packet['prompt_hash']) == 64
    assert packet == prepare('CAFE -5.00', context, 'a' * 64, True)
    with pytest.raises(ValueError, match='Context only permits'):
        prepare('CAFE -5.00', {**context, 'name': 'Private'}, 'a' * 64, True)


def test_capture_rejects_total_changes():
    def handler(request):
        if request.url.params['offset'] == '0':
            return httpx.Response(200, json={'total': 201, 'items': [{'id': str(i)} for i in range(200)]})
        return httpx.Response(200, json={'total': 202, 'items': [{'id': '201'}]})
    with httpx.Client(base_url='http://localhost', transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match='changed'):
            page_all(client, '/transactions')


def test_extraction_checks_source_provenance_and_duplicates():
    batch = synthetic_scenarios()[0]['imports'][0]
    reference = {'privacy_reviewed': True, 'source_verified': True, 'input_hash': 'input1', 'statements': [batch]}
    submission = {'provenance': {k: 'test' for k in ('model', 'prompt_hash', 'skill_hash', 'tools', 'trial_id')}, 'statements': [batch, batch]}
    submission['provenance']['input_hash'] = 'input1'
    score = extraction(reference, submission)
    assert score['metrics']['extra_rows'] == 1
    assert not score['periods_match']
    submission['provenance']['input_hash'] = 'another-source'
    with pytest.raises(ValueError, match='differs'):
        extraction(reference, submission)
    submission['provenance']['input_hash'] = 'input1'
    reference['privacy_reviewed'] = False
    with pytest.raises(ValueError, match='privacy-reviewed'):
        extraction(reference, submission)


def test_source_inventory_never_includes_cell_values(tmp_path):
    (tmp_path / 'example.csv').write_text('Posted,Kind,Payee,Note,Value\n06/01/2026,DEBIT,CAFE,PRIVATE ADDRESS,-5.00\n')
    result = inventory(tmp_path)
    assert result['sources'][0]['known_csv_layout']
    assert result['sources'][0]['rows'] == 1
    assert 'PRIVATE ADDRESS' not in json.dumps(result)
