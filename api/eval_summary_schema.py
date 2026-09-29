"""Data-only public summary contract; no scorers or historical importer imports."""
import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

METRICS = frozenset('''cases labeled_cases unlabeled_cases automatic_decisions_on_labeled
coverage accuracy automatic_precision top_level_accuracy severe_errors scenarios passed
deliberately_blocked_for_review expected_rows actual_rows exact_rows row_precision row_recall
missing_rows extra_rows invalid_submissions locator_errors source_fact_mismatches
classification_correct classification_automated classification_labeled classification_unknown
classification_abstentions classification_unscorable classification_severe_errors
overlap_batches_tested overlap_batches_refused overlap_ledger_unchanged repeat_unchanged
committed_missing_rows committed_extra_rows persisted_classification_mismatches'''.split())


class Summary(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    schema_version: Literal[1] = 1
    report_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    dataset_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    kind: Literal['classification', 'source-reviewed-ledger-replay', 'source-deduplication',
                  'end-to-end-intake', 'extraction', 'deduplication']
    corpus: Literal['development', 'synthetic', 'holdout']
    created_at: str
    git_commit: str = Field(pattern=r'^[a-f0-9]{40}$')
    source_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    dirty: bool
    status: Literal['passed', 'failed', 'measured']
    metrics: dict[str, int | float | bool | None]


def validate_summary(value):
    summary = Summary.model_validate(value).model_dump()
    datetime.fromisoformat(summary['created_at'])
    if len(summary['created_at']) > 40:
        raise ValueError('Invalid timestamp')
    for key, number in summary['metrics'].items():
        if key not in METRICS or (number is not None and not math.isfinite(number)):
            raise ValueError('Invalid metric')
    return summary
