"""Explicit, data-minimized publication. Never serve arbitrary eval artifacts."""
from api.eval_summary_schema import METRICS, validate_summary
from financial_evals.core import digest, save


def summarize(report, corpus):
    """Only explicit safe fields; private case details/paths/provenance never copied."""
    metrics = {k: v for k, v in report['metrics'].items() if k in METRICS}
    kind = report['kind']
    if kind in ('source-deduplication', 'deduplication') and corpus != 'synthetic':
        raise ValueError('Scenario suites must be labeled synthetic')
    status = 'measured'
    if kind == 'end-to-end-intake':
        status = 'passed' if metrics.get('passed') == 1 else 'failed'
    elif 'scenarios' in metrics:
        status = 'passed' if metrics['scenarios'] > 0 and metrics.get('passed') == metrics['scenarios'] else 'failed'
    implementation = report['implementation']
    return validate_summary(dict(schema_version=1, report_hash=digest(report),
        dataset_hash=report['dataset_hash'], kind=kind, corpus=corpus,
        created_at=report['created_at'], git_commit=implementation['git_commit'],
        source_hash=implementation['source_hash'], dirty=implementation['dirty'],
        status=status, metrics=metrics))


def publish(report, corpus, directory):
    summary = summarize(report, corpus)
    save(directory / (summary['report_hash'] + '.json'), summary)
    return summary
