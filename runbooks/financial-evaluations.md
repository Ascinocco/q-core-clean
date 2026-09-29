# Financial evaluations

For the combined workflow, see [end-to-end intake evaluations](intake-flow-evaluations.md).

Run `python -m financial_evals --help` from the repository environment.
This is an offline CLI, not a queue, paid model service, or production
schema change.
The `financial-evals` skill wraps it; the read-only `/ui/evals` page displays
explicitly published summaries. See [eval workflow](eval-workflow.md).

## Three separate measurements

1. **Classification:** compare the real rule matcher with frozen reviewed
   ledger labels. Unknown categories are excluded from accuracy and reported
   separately. Report coverage, automatic precision, accuracy including
   abstentions, top-level accuracy, and transfer/income misclassification.
   Per-account/category slices and transaction IDs identify regressions.
2. **Deduplication:** replay import *sequences* through the real HTTP route in
   a fresh in-memory database. Compare row multisets, not just totals. Missing
   rows are false merges; extra rows are missed duplicates. The reviewed
   ledger replay cannot recover rows previously suppressed by the importer.
3. **Extraction:** score fresh interactive-agent payloads against independently
   source-verified expected payloads. This includes the agent's date/sign/cents,
   description, multi-page and period interpretation, not merely PDF text
   extraction. Neither ledger replay nor a hand-written expected submission
   measures agent extraction quality. Field marginals diagnose errors but can
   hide swapped values; exact full-row precision/recall is authoritative.

## Freeze and run

All artifacts contain private financial data. Keep them under ignored `data/`,
never attach them to PRs or send raw source text to a model. Files are created
with mode 0600 and never overwritten. CLI output prints only metrics/hashes.
`freeze` contacts q-core using GETs; `review-bundle` calls only the read-only
extraction operation. Scoring/replay use disposable databases with no
production lifespan, credentials or writes.

```sh
python -m financial_evals freeze --env-file /path/to/app/.env --output data/evals/v1/reference.json
python -m financial_evals inventory --root /path/to/intake --output data/evals/v1/sources.json
python -m financial_evals classify --reference data/evals/v1/reference.json --output data/evals/v1/classification.json
python -m financial_evals replay --reference data/evals/v1/reference.json --output data/evals/v1/replay.json
python -m financial_evals legacy-synthetic --output data/evals/v1/synthetic.json
```

Freeze while imports/edits are idle. Two matching paginated exports detect
ordinary concurrent edits, but are not a transactional snapshot. Record the
review basis of every label set (who accepted which database state, and when). Entity names,
attributes and addresses are not exported; ledger descriptions remain private.

Baselines live under the app checkout's ignored `data/evals/`. Agreement
between current rules and a baseline built from the same history is
**historical agreement, not a prospective accuracy claim**: the rules were
developed using that history.

The frozen legacy simulator deliberately reports 10/12 passes, one extra and one missing
row. Descriptor changes evade exact dedupe; different identical purchases in
separate partial exports are indistinguishable from reimports with the retired
payload. Those are known limitations, not waived assertions. A CSV export
without a dedicated transaction ID must not be given an invented one. PDF reference fields require verification before claiming unique IDs.

## Compare a candidate

Keep labels fixed. Supply a rules-array file with `classify --rules candidate.json`,
or run the same command/dataset in a different implementation checkout.
Artifacts record commit, dirty state, source hashes, rules hash and dependencies.
Scenarios, labels and code versions are separate identities.

```sh
python -m financial_evals classify --reference data/evals/v1/reference.json --rules data/evals/candidate-rules.json --output data/evals/v1/candidate.json
python -m financial_evals compare --before data/evals/v1/classification.json --after data/evals/v1/candidate.json --output data/evals/v1/comparison.json
```

Comparison rejects different frozen datasets/suites and returns exit status 1
if monitored metrics regress; its private output identifies changed cases.
Suite commands produce measurements even when cases fail; exit 0 means the
measurement ran, **not** that every case passed. Never bless false merges just
because missed duplicates improved, or improve coverage at the expense of
precision. Investigate every new severe error. Before claiming generalization,
reserve newly reviewed, unseen documents/merchants as a holdout, and freeze
rules before examining it. Use development cases for tuning, not the holdout.

## Interactive extraction trials

To assemble local review material without exposing document text to a model:

```sh
python -m financial_evals review-bundle --env-file /path/to/app/.env --root /path/to/inventoried/sources --inventory data/evals/v1/sources.json --output data/evals/v1/local-review.json
```

This checks source hashes, calls only the server-side extraction endpoint,
and saves its scrubbed responses privately. Original bytes are read only for
hashing, never decoded locally. It refuses changed sources, path escapes,
server errors and partial extractions. The bundle includes local source paths
for the human reviewer, so it must not be sent to an agent. Both privacy and
source-verification attestations remain false. No gold rows are fabricated.

1. Extract/scrub locally, then have a human review the model-facing text outside
   model context. Existing number scrubbing alone is **not** a names/address
   guarantee. Do not open raw
   statements in an agent. `inventory` only stores hashes/counts, never text.
2. Create a private context JSON with opaque `account_id`, `source_format` and,
   only when the source lacks them, known `period_start`/`period_end`.
3. Prepare a packet; `--privacy-reviewed` is a human attestation, not a bypass
   to apply automatically. Alternatively `--privacy-authorization FILE` records
   an explicit operator decision to use server-scrubbed text. The JSON must
   contain `basis: "operator-authorized-server-scrubbed"`, the exact
   `text_sha256`, and a nonempty `decision`. This leaves `privacy_reviewed`
   false; it does not manufacture a human review. Never infer authorization
   from extraction success or a profile flag.

   ```sh
   python -m financial_evals prepare --text data/evals/reviewed.txt --context data/evals/context.json --source-id SOURCE_SHA256 --privacy-reviewed --output data/evals/packet.json
   ```

4. In a fresh interactive agent session, supply only the packet. It contains
   normalized input, source/input/prompt/skill hashes and an offline prompt.
   Do not grant live write tools or include gold answers. This tests the
   extraction adapter with the intake normalization contract, not the entire
   production skill's live workflow. Save the agent's actual returned statements
   plus a `provenance` object with `model` (exact version), `prompt_hash`,
   `skill_hash`, `tools` (list, use `["none"]` if none), `trial_id` and
   `input_hash` from the packet. Preserve the actual prompt if changed and
   recompute its hash. Run multiple distinct trials for stochastic comparisons.
5. Separately, review every expected row against the source, preserving
   duplicates *before* dedupe. Save gold JSON with `input_hash`, `source_id`,
   `privacy_reviewed: true`, `source_verified: true`, reviewer/review notes,
   and `statements` in the canonical import shape. Approved ledger labels
   alone do not verify source-level row recall or periods.
6. Score without importing:

   ```sh
   python -m financial_evals extract-score --reference data/evals/gold.json --submission data/evals/trial-1.json --output data/evals/score-1.json
   ```

## Source-tracked dedupe evaluation

`python -m financial_evals source-dedupe --output data/evals/source-dedupe.json`
exercises the only production import routes (preview/commit). Ten synthetic cases distinguish
same-source recognition, explicit cross-source linking, distinct identical
purchases and two intentional unresolved refusals. This suite does not replace
`legacy-synthetic`: the retired importer's known two identity gaps remain measured.

The current `replay` command uses production preview/commit and reviewed ledger
IDs as offline fixture identities. It does not infer real source identities.
`legacy-synthetic` and `legacy-dedupe` run the frozen in-memory simulator only.
Old command aliases (`synthetic`, `dedupe`) are retained but explicitly label
their artifacts and CLI output as retired historical behavior. No legacy HTTP
route or MCP tool exists, and the simulator refuses disk-backed databases.
