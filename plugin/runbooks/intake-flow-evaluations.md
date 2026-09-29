# End-to-end intake evaluations

This offline runner connects fresh source-aware extraction submissions to the
production preview/commit API in disposable in-memory databases. No live
imports, credentials, raw statement reads, model API calls or standing queue.
It is a development-corpus regression test, not unseen accuracy certification.

## Freeze, trial, score

1. Use previously approved scrubbed packets and separately source-reviewed
   references. Freeze current accepted labels/rules using the existing `freeze`
   command (GET-only). Keep all artifacts private under ignored `data/`.
2. Create a cases JSON array containing `case_id`, `packet_path`, and
   `reference_path`. Paths resolve relative to that JSON file. A separate
   account-mapping JSON maps opaque packet account IDs to frozen ledger IDs.
   Freeze the suite; expected rows and review attestations are preserved, not
   regenerated or blessed from arithmetic checks.

   ```sh
   python -m financial_evals freeze-intake-suite --cases data/evals/cases.json --ledger data/evals/ledger.json --account-mapping data/evals/accounts.json --output data/evals/suite.json
   ```

3. Give fresh agents **only each case's `packet` object**, not the suite. The
   suite contains gold answers and labels. Packets include approved text,
   opaque account context, source/text/input/prompt/skill hashes and current
   instructions, with live skill actions explicitly disabled. A standalone
   `prepare-intake --packet approved-packet.json --output new-packet.json`
   prepares the same current contract. Never reuse a result against a different
   prompt or silently update a frozen packet. Save the actual submissions
   unedited. Agents may calculate offsets from approved text, but may not read
   reference parsers, other trial outputs, raw documents, or live tools.
4. Assemble a submissions JSON object keyed by case ID. Each value contains
   `source_id`, `provenance`, `statements`, and `uncertainties`. Provenance names
   model, actual tools, trial ID and packet input/prompt/skill hashes. Record
   `unknown` if the runtime model cannot be verified. Each transaction has
   `txn_date`, `description`, integer `amount_cents`, and `source_row`. No agent
   classification, target transaction, review token or new/link decision.
5. Score and compare immutable outputs:

   ```sh
   python -m financial_evals intake-eval --suite data/evals/suite.json --submissions data/evals/submissions.json --output data/evals/report.json
   python -m financial_evals compare --before data/evals/before.json --after data/evals/report.json --output data/evals/comparison.json
   ```

CLI output contains aggregate metrics, not transaction text. Artifact files are
0600 and existing paths are refused. Exit 0 means measurement completed; inspect
`metrics.passed`, not just the exit code. Compare rejects different frozen
references, label/rule baselines, text inputs, account maps or scorer versions.
Prompt/skill changes are recorded separately so unchanged evidence can measure
an instruction change. Do not tune gold answers to a failed trial.

## Separate measurements

- **Extraction:** full-row multisets include account, period, date, description
  and signed cents; field marginals are diagnostic only. Counts/net totals do
  not establish correctness.
- **Source evidence:** CSV data-record indices (header excluded, quoted
  multiline records count once), or exact raw/canonical text character spans
  bound to the approved text hash. Spans must cover a complete reviewed
  occurrence and no neighboring occurrence. Reused occurrence targets and
  locator-to-payload disagreements are failures, even if row multisets match.
  This evidence uses reviewed scrubbed text, not a new visual PDF audit.
- **Classification:** map labels independently from reviewed source occurrences,
  not by matching an agent's possibly wrong description. Ambiguous source/label
  associations require review. Unknown labels are excluded from precision;
  missing/unaligned labeled rows reduce coverage. Report wrong decisions,
  abstentions and transfer/income errors separately. Check persisted categories
  against the rule prediction too.
- **Initial/repeat imports:** replay actual valid-schema submissions in forward
  and reverse batch order. Compare committed multisets and all stored row fields
  before/after a fresh-preview retry. Gold never supplies import decisions or
  filters out badly extracted rows. API failures and unsafe writes remain in
  the trace; malformed trial schemas cannot masquerade as successful runs.
- **Unknown overlaps:** seed the frozen reviewed ledger without source receipts,
  then submit unchanged extraction facts with no new/link decisions. Every
  matching batch should refuse atomically. This tests review-required behavior,
  not successful cross-export adjudication. Incorrect extracted dates/amounts
  can evade candidate matching; the evaluator must expose that, not call it a
  dedupe success. Separate `source-dedupe` scenarios test explicit known links
  and distinct identical purchases.

Artifacts connect case/source, packet/reference/submission hashes, provenance,
row locator, expected occurrence, API attempt, committed transaction ID and
classification. A pending row excluded with a note is not an error. Reported
uncertainties are retained; they do not authorize guessed facts.
