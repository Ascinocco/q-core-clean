# Source-tracked statement imports

This is the only production statement importer. The legacy `import_statement`
MCP tool and HTTP route are removed; old HTTP callers receive 404 and must
migrate to preview/commit. Reconnect clients that cache MCP tool lists.
No ledger migration or historical source-receipt backfill is performed.

## Identity and privacy

`extract_document_text` returns `source_id`, SHA-256 of the original file's
bytes, calculated server-side. It does not expose original text. Do not use a
filename, account number, hash of a cleaned description, or bank reference
whose uniqueness has not been verified. Do not register/move documents merely
to obtain a hash. Previous approved extraction packets also carry source IDs.

Each posted row needs a stable `source_row` locator derived from the approved
scrubbed source, e.g. `csv:v1:record:17` (original data-record position, with an
explicit header convention). A PDF locator must identify a stable physical
page/table/row, not the order the model happened to output transactions. The
current flattened PDF text does not always establish such positions: stop and
request review when it cannot. Never reopen raw statements or privacy profiles.
Version a changed locator convention; do not reuse keys for different rows.
Hashes/locators supplied by callers are attestations, not verified bank IDs.

An alternative for flattened PDF text is an exact character-span locator
namespaced by the server-returned `text_sha256` and the text-normalization
version, e.g. `text-v1:<64-hex-text-hash>:123:189`. Compute spans from approved
scrubbed text, not output-row order. A changed redaction profile or extractor
then changes the namespace and requires review instead of silently reusing
shifted offsets. The reviewed reference adapters' canonical-text spans require
their own `canon-v1` namespace; they are not offsets in unnormalized text.

Identity is `(account_id, source_id, source_row)`. A source occurrence can link
to only one ledger transaction, and distinct occurrences within the same file
cannot link to the same transaction. Re-extraction may change the description
of a recognized occurrence without changing stored facts or classification;
date/amount disagreement or an archived target is a stop. A re-export is a
different file and requires cross-source review where it overlaps.

## Preview and commit

1. Call `preview_source_import(payload)` with account, period, source ID, actor,
   and transactions (`source_row`, ISO `txn_date`, description, integer cents).
   Preserve occurrences; do not deduplicate in the extraction step. Maximum
   2,000 rows per request; larger sources can use disjoint locator batches.
2. Preview returns `new`, `known_source`, `needs_review`, or `source_conflict`
   for each row, candidate ledger rows, original receipts and a `review_token`.
   Candidates share account and signed cents and fall within **3 days** of
   the row's date. Each row's `match` is `exact` (a same-date candidate
   exists) or `near_date` (only other dates within the window); each
   candidate carries `days_apart`, exact dates first. Two exports of one
   charge often disagree on its date by a day or two (statement vs effective
   date, month-end vs the 1st), so a near-date candidate is a possible
   duplicate that needs review, never a silent `new`. Exact descriptions are
   additional evidence, **not** proof that two exports describe one purchase.
3. For `needs_review`, obtain source evidence or operator confirmation. Supply
   `decision: "link"`, the candidate `transaction_id`, and a review `reason`
   for the same purchase; or `decision: "new"` plus a reason for a distinct
   purchase. No guesses based on fuzzy merchant similarity. A differing
   amount, or a date more than 3 days away, is never a link candidate. A
   `near_date` candidate may be linked when the evidence shows one charge,
   e.g. a balance that only fits one charge, or the other source's known
   posting-date convention; say which in the reason. The existing row keeps
   its date, category and entity. No broad "merge everything" switch.
4. Submit the same facts and `review_token` through `commit_source_import`.
   The token binds input, candidate state, receipts, statement state and rules.
   Missing/stale token, unresolved row or conflicting decision refuses the
   entire batch. New ledger rows and source receipts commit atomically.
5. After an uncertain response, preview again. Already committed occurrences
   become `known_source`; no new rows are inserted. Do not blindly retry an
   old token. Read back returned transaction IDs to check categories; original
   rows are never reclassified or reparented by a link or recognition.

Only batches creating new transactions create a statement. Linking evidence
does not claim that another statement period is fully covered. Archived
statement-period collisions refuse rather than silently reactivate old data.
Receipts are append-only through these tools and are visible in later previews.
Existing ledger rows are not backfilled or reimported automatically.

## Evaluations

`python -m financial_evals source-dedupe --output data/evals/source-dedupe.json`
tests the real routes in a disposable database. Scenario results distinguish
confirmed links, confirmed distinct purchases and unresolved safe refusals.
The `legacy-synthetic` command preserves the frozen pre-retirement simulator
and its two known failures, in memory only; it is not the running importer.
The `replay` command now exercises production preview/commit with reviewed
ledger identities as offline test fixtures, not as inferred source identities. Passing the new suite is not proof of automatic
cross-export identity or model extraction accuracy. No standing queue or model
API is introduced.
