"""Behavioral end-to-end eval contracts using synthetic, not private, data."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from financial_evals.core import compare, load, save
from financial_evals.intake_flow import evaluate, prepare_trial, _canonical_positions, _locator_match
from financial_evals.packets import prepare


@pytest.fixture
def trial():
    ledger = load(Path(__file__).parents[1]/'financial_evals/fixtures/reference.json')
    rows = [{k: row[k] for k in ('txn_date','description','amount_cents')} for row in ledger['transactions'][:2]]
    text = 'Posted,Kind,Payee,Note,Value\n06/03/2026,DEBIT,CAFE #12,,-5.00\n06/04/2026,DEBIT,SHELL #12,,-25.00\n'
    packet = prepare_trial(prepare(text, {'account_id':'account-a','source_format':'.csv'}, hashlib.sha256(b'synthetic').hexdigest(), True))
    statement = {'account_id':'account-a','period_start':'2026-06-01','period_end':'2026-06-30','transactions':rows}
    reference = {k:packet[k] for k in ('source_id','input_hash','text_sha256','privacy_reviewed','prompt_hash','skill_hash')}
    reference.update(source_verified=True, statements=[statement], rows=[{**row,'csv_row':i+1} for i,row in enumerate(rows)])
    reference['account_id']='account-a'
    suite = {'cases':[{'case_id':'case-1','packet':packet,'reference':reference}], 'ledger':ledger,
             'account_mapping':{'account-a':'account-a'}}
    submission = {'source_id':packet['source_id'], 'provenance':{k:packet[k] for k in ('input_hash','prompt_hash','skill_hash')},
                  'statements':[{**statement,'transactions':[{**row,'source_row':f'csv:v1:record:{i+1}'} for i,row in enumerate(rows)]}],
                  'uncertainties':[]}
    submission['provenance'].update(model='synthetic-not-an-agent', trial_id='synthetic', tools=['none'])
    return suite, {'case-1':submission}


def test_actual_production_routes_first_repeat_and_unknown_overlap(trial):
    suite, submissions = trial
    original = copy.deepcopy((suite,submissions))
    report = evaluate(suite,submissions)
    m = report['metrics']
    assert m['passed'] == 1 and m['exact_rows'] == 2
    assert m['automatic_precision'] == m['coverage'] == 1
    assert m['locator_errors'] == m['invalid_submissions'] == 0
    assert m['overlap_batches_refused'] == m['overlap_batches_tested'] == 1
    assert m['overlap_ledger_unchanged'] and m['repeat_unchanged']
    assert all(s['all_repeats_recognized'] for s in report['sequences'])
    assert (suite,submissions) == original


def test_freeze_preserves_reviewed_answers_and_does_not_auto_verify(trial):
    from financial_evals.intake_flow import freeze_suite
    suite,_=trial
    frozen=freeze_suite(suite['cases'],suite['ledger'],suite['account_mapping'])
    assert frozen['cases'][0]['reference']==suite['cases'][0]['reference']
    suite['cases'][0]['reference']['source_verified']=False
    with pytest.raises(ValueError):freeze_suite(suite['cases'],suite['ledger'],suite['account_mapping'])
    assert frozen['cases'][0]['reference']['source_verified'] is True


def test_frozen_suite_tamper_and_empty_gold_are_refused(trial):
    from financial_evals.intake_flow import freeze_suite
    suite,submissions=trial
    frozen=freeze_suite(suite['cases'],suite['ledger'],suite['account_mapping'])
    frozen['ledger']['rules'][0]['pattern']='CHANGED'
    with pytest.raises(ValueError,match='Frozen suite changed'):evaluate(frozen,submissions)
    suite['cases'][0]['reference'].update(rows=[],statements=[])
    with pytest.raises(ValueError,match='posted occurrences'):evaluate(suite,submissions)


def test_sign_inversion_is_not_hidden_by_classification_or_netting(trial):
    suite, submissions = trial
    for row in submissions['case-1']['statements'][0]['transactions']:
        row['amount_cents'] *= -1
    report = evaluate(suite,submissions)
    assert report['metrics']['automatic_precision'] == 1
    assert report['metrics']['missing_rows'] == report['metrics']['extra_rows'] == 2
    assert not report['metrics']['overlap_ledger_unchanged']
    assert report['metrics']['passed'] == 0
    assert report['metrics']['source_fact_mismatches'] == 2


def test_swapped_locators_cannot_hide_behind_perfect_multiset_scores(trial):
    suite,submissions=trial
    rows=submissions['case-1']['statements'][0]['transactions']
    rows[0]['source_row'],rows[1]['source_row']=rows[1]['source_row'],rows[0]['source_row']
    report=evaluate(suite,submissions)
    assert report['metrics']['exact_rows']==2
    assert report['metrics']['source_fact_mismatches']==2
    assert report['metrics']['passed']==0


def test_legitimate_identical_occurrences_survive(trial):
    suite,submissions=trial
    reference=suite['cases'][0]['reference']
    first=reference['statements'][0]['transactions'][0]
    reference['statements'][0]['transactions'][1]=copy.deepcopy(first)
    reference['rows'][1]={**first,'csv_row':2}
    suite['ledger']['transactions'][1].update({**first,'category_id':'food_dining'})
    submissions['case-1']['statements'][0]['transactions'][1]={**first,'source_row':'csv:v1:record:2'}
    report=evaluate(suite,submissions)
    assert report['metrics']['passed']==1
    assert report['sequences'][0]['attempts'][0]['result']['created']==2


def test_route_classification_persistence_is_checked(trial,monkeypatch):
    from contextlib import contextmanager
    from financial_evals import intake_flow
    original=intake_flow.sandbox
    @contextmanager
    def corrupted(reference):
        with original(reference) as (db,client):
            db.execute('CREATE TRIGGER lost_category AFTER INSERT ON transactions BEGIN UPDATE transactions SET category_id=NULL WHERE id=new.id; END;')
            yield db,client
    monkeypatch.setattr(intake_flow,'sandbox',corrupted)
    result=evaluate(*trial)
    assert result['metrics']['automatic_precision']==1
    assert result['metrics']['persisted_classification_mismatches']==4
    assert result['metrics']['passed']==0


@pytest.mark.parametrize('mutation', ['missing','invented','locator','reused','period','source','provenance','decision','fractional'])
def test_bad_submissions_never_score_as_passes(trial, mutation):
    suite, submissions = trial
    output = submissions['case-1']
    rows = output['statements'][0]['transactions']
    if mutation == 'missing': rows.pop()
    elif mutation == 'invented': rows.append({**rows[0], 'source_row':'csv:v1:record:3'})
    elif mutation == 'locator': rows[0]['source_row']='csv:v1:record:100'
    elif mutation == 'reused': rows[1]['source_row']=rows[0]['source_row']
    elif mutation == 'period': output['statements'][0]['period_start']='2026-05-01'
    elif mutation == 'source': output['source_id']='f'*64
    elif mutation == 'provenance': output['provenance']['prompt_hash']='f'*64
    elif mutation == 'decision': rows[0]['decision']='new'
    elif mutation == 'fractional': rows[0]['amount_cents']=-5.5
    assert evaluate(suite,submissions)['metrics']['passed'] == 0


def test_unknown_labels_are_not_counted_correct_and_missing_rows_reduce_coverage(trial):
    suite, submissions = trial
    suite['ledger']['transactions'][0]['category_id']=None
    report = evaluate(suite,submissions)
    assert report['metrics']['classification_labeled']==1
    assert report['metrics']['unlabeled_predictions']==1
    submissions['case-1']['statements'][0]['transactions'].pop()
    report = evaluate(suite,submissions)
    assert report['metrics']['coverage']==0
    assert report['metrics']['classification_unscorable']==1


def test_severe_classification_error_is_visible(trial):
    suite, submissions = trial
    suite['ledger']['rules'][0]['category_id']='transfers'
    report = evaluate(suite,submissions)
    assert report['metrics']['classification_severe_errors']==1
    assert report['metrics']['passed']==0


def test_text_spans_bind_to_exact_source_with_unicode_and_split_decimals():
    text='header\nJun 3 CAFE – 5\n.00 100.00\nJun 4 SHOP 8.00 92.00\n'
    canonical, positions = _canonical_positions(text)
    assert canonical=='header Jun 3 CAFE – 5.00 100.00 Jun 4 SHOP 8.00 92.00'
    assert positions == sorted(positions)
    start,end=text.index('Jun 3'),text.index('\nJun 4')
    packet={'input':{'text':text,'context':{'source_format':'.pdf'}},'text_sha256':hashlib.sha256(text.encode()).hexdigest()}
    expected=[{'span':[start,end],'span_basis':'original scrubbed text','source_excerpt':text[start:end]},
              {'span':[end+1,len(text)-1],'span_basis':'original scrubbed text','source_excerpt':text[end+1:].strip()}]
    assert _locator_match(packet,expected,f"text-v1:{packet['text_sha256']}:{start}:{end}")==[0]
    a,b=canonical.index('Jun 3'),canonical.index(' Jun 4')
    assert _locator_match(packet,expected,f"canon-v1:{packet['text_sha256']}:{a}:{b}")==[0]
    assert _locator_match(packet,expected,f"text-v1:{packet['text_sha256']}:{start}:{len(text)}")==[]
    with pytest.raises(ValueError): _locator_match(packet,expected,f'text-v1:{"f"*64}:{start}:{end}')


def test_quoted_multiline_csv_locators_count_records_not_lines(trial):
    suite, _ = trial
    packet=suite['cases'][0]['packet']
    packet['input']['text']='Name,Amount\n"CAFE\nSHOP",-5.00\nSHELL,-25.00\n'
    expected=[{'csv_row':1},{'csv_row':2}]
    assert _locator_match(packet,expected,'csv:v1:record:2')==[1]
    with pytest.raises(ValueError): _locator_match(packet,expected,'csv:v1:record:3')


def test_no_gold_assisted_filtering_of_valid_wrong_locator(trial):
    suite, submissions=trial
    row=submissions['case-1']['statements'][0]['transactions'][0]
    row['source_row']='csv:v1:record:99'
    result=evaluate(suite,submissions)
    assert result['metrics']['locator_errors']==1
    # API accepts caller attestations; evaluator must show that, not erase row.
    assert result['sequences'][0]['attempts'][0]['result']['created']==2


@pytest.mark.parametrize('what',['packet_text','reference_text','unverified','missing_case'])
def test_refuse_invalid_frozen_inputs(trial,what):
    suite,submissions=trial
    case=suite['cases'][0]
    if what=='packet_text':case['packet']['input']['text']+='tampered'
    elif what=='reference_text':case['reference']['text_sha256']='f'*64
    elif what=='unverified':case['reference']['source_verified']=False
    else:submissions={}
    with pytest.raises(ValueError):evaluate(suite,submissions)


def test_comparison_holds_reference_fixed_and_cli_emits_only_summary(trial,tmp_path,monkeypatch,capsys):
    from financial_evals.__main__ import main
    suite,submissions=trial
    before=evaluate(suite,submissions)
    submissions['case-1']['statements'][0]['transactions'].pop()
    after=evaluate(suite,submissions)
    assert 'missing_rows' in compare(before,after)['regressions']
    for name,value in [('suite',suite),('submissions',submissions)]:save(tmp_path/(name+'.json'),value)
    monkeypatch.setattr('sys.argv',['financial_evals','intake-eval','--suite',str(tmp_path/'suite.json'),
        '--submissions',str(tmp_path/'submissions.json'),'--output',str(tmp_path/'report.json')])
    main()
    printed=capsys.readouterr().out
    assert 'CAFE' not in printed and json.loads(printed)['metrics']['passed']==0
    assert (tmp_path/'report.json').stat().st_mode & 0o777==0o600
