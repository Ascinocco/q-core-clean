# Statement intake: source formats and normalization

What the intake harness must guarantee before source-tracked preview/commit,
and the per-format quirks that make that non-trivial. The rows below
describe publicly available bank export formats; the ids are format labels
used by the reference adapters, not accounts.

## The split: harness normalizes, API stays strict

The skill is the adapter. Every rough edge in a source document gets
smoothed in the harness; the API accepts one canonical shape and rejects
anything else loudly. The strictness is the point — it is what turns a
sloppy extraction into a visible 422 instead of a plausible wrong number
filed under the wrong month.

Do not "fix" the API to be more accepting of messy input. A date filter
typed as `str` used to accept `05/01/2026` and silently return every row,
because `'2026-05-10' >= '05/01/2026'` holds lexically. Typed as `date`
it is a 422. That is the pattern to preserve.

## Canonical payload

`POST /source_imports/preview` — `account_id` (an entity of type `account`),
ISO `period_start`/`period_end`, `source_id`, `actor`, and `transactions[]` of
`{source_row: stable locator, txn_date: ISO, description: str, amount_cents: signed integer}` where
**negative is a debit**. `$52.10` out is `-5210`.
Commit requires the returned review token and explicit overlap decisions;
see [Source-tracked imports](source-tracked-imports.md). No legacy fallback.

## Per-source quirks

The reference formats are fictional: a deposit-statement PDF, a
card-statement PDF, a debt-account printout, and CSV/spreadsheet exports.
They illustrate the quirks below; they are not one lineup of accounts.

| Format | File type | What bites |
|---|---|---|
| CSV export (`bank-c-csv`) | CSV | `MM/DD/YYYY` dates; columns `Posted, Kind, Payee, Note, Value`; export windows overlap (below) |
| Deposit-statement PDF (`bank-a-pdf`) | PDF | Monthly e-statements; multi-page, so page 2+ completeness is the failure mode |
| Card-statement PDF (`bank-b-card-pdf`) | PDF | **One document can span several statement periods** — split it by the document's own period headers (below). A header may use an EN DASH; matching only `-` silently drops a whole period. |
| Debt-account printout (`bank-b-printout-pdf`) | PDF | **Not a statement** — a rolling-window web printout with no billing period. Use observed posted transaction dates as an activity window, not a calculated billing period; later exports can overlap. Balances are amounts **owed**, so interest increases debt. |
| Spreadsheet export (`bank-a-xlsx`) | XLSX | Returned as CSV text with header `Tag, Posted, Details, More details, Entry type, Value, Balance`; `Tag` (account label and number on the first row) is always `[REDACTED]`. Dates `YYYY-MM-DD`; `Value` is already signed (debits negative) — assert the sign matches `Entry type` (`Debit`/`Credit`). `Balance` can be **blank** on a row; a blank-balance row may be unposted — report it and confirm before importing. **description = `Details` + space + `More details`** (the second omitted when empty), so existing merchant rules keep matching. An activity window, not a statement period. Locator `csv:v1:record:N` (N = data row, header excluded). |
| Headerless CSV export (`bank-b-csv`) | CSV | **No header row**: date, description, debit, credit, balance. Extraction prepends the header `Date, Description, Debit, Credit, Balance`, so record N is still the file's Nth row. Dates `MM/DD/YYYY` or `YYYY-MM-DD`. Balances are amounts **owed**: `Debit` → negative `amount_cents`, `Credit` → positive. Exactly one of debit/credit is filled per row (the scrubber refuses otherwise). Activity window, not a statement period. Locator `csv:v1:record:N`. |

## Normalization rules

1. **Dates → ISO `YYYY-MM-DD`.** Bank C exports `MM/DD/YYYY`. The API
   rejects anything else, so a missed conversion fails loudly.
2. **Dollars → integer cents.** Sources quote dollars (`-52.10`); the API
   takes `amount_cents` and nothing else, so `-52.10` becomes `-5210`.
   Convert exactly — `round(Decimal(value) * 100)`, not
   `int(float(value) * 100)`, which loses a cent on values whose binary
   float sits just below the decimal: `-19.99` becomes `-1998`, `1.15`
   becomes `114`, `-0.29` becomes `-28`. It is right for `-52.10`, which
   is what makes it dangerous — spot-checking one amount does not show
   the bug.

   The `round` is what matters, not the `Decimal` construction: `value`
   may be the source string or a float already parsed from it, since
   `round(Decimal(float("-19.99")) * 100)` is `-1999` too. There is no
   must-pass-a-string precondition to get wrong — `int()` truncating
   toward zero is the whole defect.

   A fractional value is **rejected, not rounded**: `amount_cents` is a
   `StrictInt`, so `-52.10` and even `-5210.0` are a 422. That strictness
   is deliberate — rounding at the boundary would put the representation
   error back exactly where the cents column exists to remove it, and
   silently.

3. **Signs pass through.** In the Bank C CSV format every `DEBIT`
   row is negative and every `CREDIT` row positive, so the
   convention already matches ours — do not invert. Use the redundant
   `Kind` column as a free integrity check: assert the sign matches
   the type, which catches the bank silently changing its export format.
4. **For Bank C, `description` is the `Payee` column only.** Never `Payee + Note`.
   `merchant_rules` patterns match against `description`, so a description
   that sometimes carries a memo and sometimes doesn't makes rule matching
   unpredictable and quietly degrades the taxonomy. One field, always.
5. **Redact before a model ever sees the document.** SSNs and full card,
   account and routing numbers appear in a `Note` and in PDF body text —
   all four, matching CLAUDE.md's ground rule rather than a subset of it,
   because a scrubber specified from a short list scrubs a short list.

   Scrubbing them in the harness is one layer too late: by then the raw
   statement has already been read into model context, which is the thing
   CLAUDE.md's "never sent to any LLM" forbids. Persisting a clean value
   afterwards does not undo that.

   So extraction and scrubbing happen **outside model context**, in a
   server-side extract-and-scrub step. The intake skill consumes only
   that step's scrubbed output and never opens a raw statement itself.
   `last4` survives — it is what makes an account recognisable — and
   amounts and dates are untouched, since scrubbing anything numeric would
   defeat the import.

   Personal names and addresses now additionally require the locally
   reviewed [document privacy profile](document-privacy.md). Bank C
   `Note` is withheld entirely; `Payee` remains the description source.
   On `personal_redaction_required`, stop for local user review. Do not
   open the original, read the profile, or relax its review flag yourself.

6. **Split multi-period documents into billing periods.** A Bank B card
   statement download can cover several months. Importing it as one multi-month statement
   makes the period meaningless for reconciliation and makes any future
   re-export overlap catastrophically instead of partially.

   Follow source table membership, supported by repeated totals and closing
   balances, not the nearest page header or transaction-date bucketing. A
   transaction can fall outside its table's billing dates: keep both the
   source date and table membership. Exclude pending activity from posted
   rows and report it separately. A header without visible rows does not
   justify importing an empty statement. For activity exports (CSV exports,
   web printouts, or posted activity since the last card statement), use the minimum
   and maximum observed posted dates; do not claim complete billing coverage.

   **Descriptions remain free text.** Preserve merchant spelling, case,
   punctuation and source detail. Join wrapped text with spaces, collapse
   whitespace, and remove standalone `[REDACTED]` tokens (bounded by whitespace
   or string ends). Do not remove embedded lookalikes or infer hidden text.
   Keep masked suffixes such as `****1234` as source detail, never merchant
   names or rule patterns. If no meaningful description survives, flag the
   row for review instead of inventing one. Preserve every source occurrence,
   including identical purchases; extraction does not deduplicate or classify.


7. **Rejoin money tokens split across lines, and verify with the balance
   chain.** `pypdf` sometimes emits one amount as two tokens — for example
   `1,234` then `.56`. When the split token is a **balance**, that row
   parses with one number instead of two and every later row's delta is
   computed against a stale balance.

   The damage is not a missing row. In an invented example, `TRANSFER
   FROM ****1234` is a **+100.00 deposit** and a broken chain computes
   **-1,234.56** for it — a plausible-looking number, wrong sign, no error
   anywhere.

   Rejoin `N,NNN` + `.NN` before parsing. Then check the balance chain:
   for each row, `|balance - previous_balance|` must equal the stated
   amount. That check is what caught this, and it is not optional on a
   bank PDF. For a deposit-account balance (money held), signed cents equal
   chronological `after - before`. For a credit-card/LOC balance (money owed),
   signed cents equal `before - after`: purchases and interest increase debt
   and are negative; payments, refunds and cashback reduce debt and are positive.
   A newest-first display is not chronological order. Validate both the
   absolute amount and direction against the source columns and balance meaning;
   a balanced chain alone can pass while every sign is reversed.

   The redundant-column check from rule 3 does not exist here: that was
   the CSV's `Kind` column. The balance chain is its replacement.
## Gotcha: export windows overlap

Bank C's exports are ragged and share boundary days. For example
(invented dates):

```
01/05 .. 02/04      02/04 .. 03/05      03/06 .. 04/05      04/06 .. 04/20
             ^^^^^^^^^^^ same day in both files
```

Both files carry rows dated 02/04. The source-tracked API flags overlapping
same-account, same-cents candidates within 3 days (exact dates first) for
review, because two exports can date one charge a day or two apart. Even identical descriptions cannot
prove that different exports describe the same purchase. Preserve each
source occurrence; confirm links or distinct purchases with evidence before
commit. Unresolved overlaps refuse the entire batch. The retired importer's
description/multiplicity heuristic is no longer a production fallback.

## Assumption: everything is CAD

### Comparison-only compatibility

The API's review-only description comparison removes only standalone `[REDACTED]` tokens
and normalizes whitespace, on both existing and incoming rows. Case,
punctuation, masked suffixes and merchant detail still distinguish rows.
An entirely uninformative result retains its whitespace-normalized original
instead of becoming an empty key. Stored descriptions and classification
inputs are not rewritten. Matching descriptions never authorize links by themselves, including
mixed placeholder/clean descriptions within one batch.

All accounts are assumed CAD, so there is no currency column and aggregates
sum raw amounts. This is load-bearing: a single USD transaction would be
summed into a CAD total with no signal, producing a wrong number rather
than an error. If a non-CAD account is ever added, a `currency` column
has to land **before** its first import — cheap now, a migration later.
