from pathlib import Path
from financial_evals.core import load
from financial_evals.source_dedupe import evaluate


def test_source_evidence_suite_distinguishes_safe_refusals_from_resolved_imports():
    result=evaluate(load(Path(__file__).parents[1]/'financial_evals/fixtures/reference.json'))
    assert result['metrics']=={'scenarios':12,'passed':12,'deliberately_blocked_for_review':3}
    assert all(c['preexisting_rows_preserved'] for c in result['cases'])
