---
name: statement-intake
description: Process a bank or credit-card statement or new folder in intake/ through the q-core financial audit system — server-side scrub, extract, normalize, preview, review overlaps and import. Use for importing/processing statements or auditing an intake folder; honor preview-only requests. Offline eval runs belong to financial-evals.
---

# Statement intake

## Never — read this before anything else

These are not cautions. They are the reason this skill exists, and every
one of them will feel reasonable to break at the moment it matters.

**Never open a statement yourself.** Not with Read, not with `cat`, not
with a Python script, not "just to see the columns". A tool result is
model context: opening the file puts the account number into the
conversation, which is precisely what the scrubber exists to prevent.
The only way statement text enters context is
`extract_document_text`'s return value.

**This applies to CSVs exactly as much as PDFs.** `intake/` holds CSV
and PDF statements; anything else in there is not a statement and is
ignored unless the user names it. The exposure is identical for both
kinds:
`${CLAUDE_PLUGIN_ROOT}/runbooks/statement-intake.md` records that full card, account and
routing numbers appear in a `Note` column as well as in PDF body text.
"It's only a CSV" is the most likely way this invariant gets broken,
because the file looks harmless and reading it looks trivial. CSV, TSV,
TXT and XLSX all go through the same tool.

**Never process anything from `inbox/`.** That directory belongs to the
document-intake skill, which files receipts and tax documents. A
statement that arrives there should be moved to `intake/` first, or named
explicitly by the user — not swept up because it looked like a statement.
Two skills claiming one file is how the same document gets imported
twice, once as a statement and once as a receipt. Registering a document
from `inbox/` also **moves** it, which is not undoable, so a file bounced
between skills cannot simply be put back.

**When `extract_document_text` refuses or errors, stop.** Report the
tool's error to the user, verbatim except for masking any digits it
echoes, and go no further on that file. Specifically, on
`redaction_incomplete`, on `partial_extraction`, on a scanned or empty
document, on a path outside the permitted roots, or on any other refusal:

- Do **not** open the file to see what went wrong.
- Do **not** ask the user to paste the contents, or a sample, or "just
  the header row".
- Do **not** fall back to reading a CSV, TSV or TXT directly.
- Do **not** try a different path, a copy, or a converted version of the
  same document.

The only remediation you may suggest is filing a ticket against the
scrubber. A refusal means the safety property could not be established;
working around it is working around the safety property.

**`partial_extraction` is a STOP and a ticket. Never pass `allow_partial`
on a bank or card statement.** The refusal means some pages yielded text
and some yielded none, and the tool offers `allow_partial=true` to accept
that — which is exactly why it needs a rule here. The parameter exists
for a blank cover or notice page.

On a statement a missing page means **missing transactions**, and they do
not arrive as an error: they import as though the statement were whole.
Worse, the check that would otherwise catch it cannot. A statement's
balance chain reconciles *within* the pages you have, so a run that
silently dropped page 2 still produces a chain that verifies, totals that
add up, and a closing balance that agrees with itself. The evidence of
completeness and the missing data are on the same page.

Reaching for `allow_partial` will feel reasonable, because the refusal
arrives when you are most of the way through a document that mostly
worked. That is the moment the rule is for.

**Never send an SSN, or a full account, routing or card number, to any
model.** That is the standing invariant of this system, and this skill's
entire shape follows from it. If you ever find yourself holding one, you
have already gone wrong — say so plainly rather than continuing.

## What the scrubbed text looks like

Redaction happens server-side before you see anything, so the text you
get already contains placeholders:

- `****1234` — a long digit run with its last four preserved.
- `[REDACTED]` — an SSN, or a run with nothing safe to keep.

**Never treat either token as merchant text.** They are not part of a
description, they must not become part of a merchant rule pattern, and a
description that is only a token is not a merchant name.

Over-redaction is expected and accepted. A phone number written
`555.555.1234` comes back as `****1234`; a confirmation number is removed
the same way an account number is. Amounts and dates are preserved.
If a row's description is unusable because of redaction, treat it as
unmatched and say so — do not reconstruct it.

## The flow

### 1. Choose the file

**On a machine that is not the server, push the folder first.** When the user
names a folder they dropped in their local intake folder (for example "process
the intake folder `example_batch`"), copy it to the server before anything else:

```
"${CLAUDE_PLUGIN_ROOT}/scripts/q-core-server.sh" --ssh "${user_config.server_ssh}" --checkout "${user_config.server_checkout}" push --intake-dir "${user_config.intake_dir}" NAME
```

It streams the folder to the server's `intake/NAME` over OpenSSH and prints one
JSON line: the file names, the count and the total size. That line is all you
see; the helper never reads or prints file contents, and you must not either.
On the server itself it reports `skipped` and nothing moves.
- **Any non-zero exit is a stop.** Report it and do not work around it:
  - exit 3 (`refused`): a symlink, an unreadable or non-file entry, too large,
    the folder already exists on the server, or it arrived incomplete;
  - exit 4: no such local folder;
  - any other code, e.g. 255: SSH failed.

  Nothing lands on the server unless the whole folder did.

After a successful push, the files are on the server as `NAME/<file>`, and the
rest of this procedure uses those intake-relative paths.

If the user names a new folder under `intake/`, discover filenames recursively
only within that subtree; never open file contents or follow links outside the
permitted roots. Process supported statements one at a time and preserve the
stop-on-refusal rule. Files already in intake need no upload. Do not sweep other
months or `inbox/` into the request. An audit/preview-only request authorizes no
commit, account creation, merchant rule change or other live mutation; report
missing account context instead of creating it.

On the server, list `intake/` by name only (`ls`). From another machine, use
the file names the push printed; you cannot list the server's folder. Filenames
are not statement content. Confirm with the user which document to import if it
is not obvious.

Count what you find rather than repeating a number you were told: the
directory also holds files that are not statements, so "how many
documents" and "how many files" are different questions and the second
is the one `ls` answers.

### 2. Extract

Call `extract_document_text(path)`. The path is relative to the intake
directory, or absolute inside `intake/` or `data/documents/`.

Check the result before parsing: it reports the page count, characters
extracted per page, and how many redactions were made.

A document whose pages **all** extract zero characters is
`no_text_extracted` — usually a scanned image, which this system cannot
OCR. A document where **some** pages extract and others do not is
`partial_extraction`. Both are a stop.

**A clean return is not a guarantee of completeness.** The refusal
catches a page that extracted *nothing*; it does not catch a page that
extracted too little. A continuation stub page can legitimately come back
with only a few dozen characters and pass.
So read `extracted_chars` yourself and satisfy yourself every page
plausibly carries what it should, rather than treating a clean return as
an assurance somebody else already checked.

### 2b. Find or create the account entity

`preview_source_import` needs an `account_id`, and that account must exist as
an `entity` of type `account`. On a fresh system none of them do.

1. Derive the bank, the account subtype and the **last four digits** from
   the scrubbed text. The scrubber keeps the last four of a long digit
   run, so a card appears as `****1234`. The CSV filename also carries a
   bank-masked card number — filenames are not statement content, so
   reading the name is fine.
2. `list_entities(entity_type="account")` — **drained, see "Every list
   call is paginated"** — and look for one whose `last4` attribute
   matches. **A missing `last4` never matches anything,
   including another missing one.** Two statements that both failed to
   yield digits are not evidence of the same account — they are two
   separate failures to identify one. Treat a missing `last4` on either
   side as a miss, go to step 3, and flag it.
3. If none matches, create it:
   `create_entity(entity_type="account", name="Example Bank Chequing ****1234",
   attributes={"institution": ..., "account_subtype": ..., "last4": "1234"})`.

`account_subtype` is one of `checking`, `savings`, `credit_card`, `loan`,
`investment`, `insurance` (`${CLAUDE_PLUGIN_ROOT}/runbooks/entity-attribute-schemas.md`).

**If the subtype is not clear, leave `account_subtype` null.** Set
`institution` and `last4`, omit the subtype, and flag it in the import log
as awaiting the owner. The attribute is optional, so omitting it is a valid
create and nothing downstream breaks; guessing it writes a wrong fact that
reads exactly like a checked one. Never infer the subtype from a filename
beyond what the filename plainly states — `synthetic 5555 xxxx xxxx
4321.csv` states a masked number and nothing about the product.

**The value is `checking`, not `chequing`.** The Canadian spelling is what
the bank prints and what the display name should keep, but it is not a
legal attribute value and the API rejects it with a 422. Spell the
attribute `checking` even when the statement, and your own entity name,
say Chequing.

**`last4` is the four digits alone.** It is length-limited to four, so
passing the scrubber's `****1234` token is rejected — strip the mask and
store `1234`. The display name may keep the mask; the attribute may not.

**Never write a full account number anywhere**, including into the entity
name. If the scrubbed text yields no last four, take it from the filename
if it is masked there, and otherwise **leave `last4` unset** and flag it
in the import log rather than digging for the real number.

Unset means omitted or `null`. Do **not** write the string `"unknown"`:
`last4` is `max_length=4`, so `"unknown"` is seven characters and the
create fails with a 422. A sentinel that cannot be stored is not a
fallback — it is an import that stops on an error at the point where you
had already decided to proceed without the digits.

When someone is present, ask before creating an account. **Unattended,
create it without confirming** — the alternative is no import at all, and
a missed import has to be re-run. Record every creation in the import log
so it is reviewable.

**The account entity is load-bearing for correctness, not a label.**
Source receipts and overlap candidates are scoped to `account_id`. A wrongly
split account can double-count the same source; a wrongly merged pair can
produce misleading overlap candidates. Never infer account identity from
matching transaction descriptions or amounts, and never resolve those
collisions by guessing.

So identity must come from a **derived** last four — the scrubbed text or
the masked filename — never from a guess, a position in a list, or the
assumption that the only existing account must be the right one. Anything
less than a derived match is flagged in the import log and left for the owner,
not resolved on your own judgement. Creating an extra entity that the owner later
merges is the safer error: it is visible in the data as a duplicated row
and repairable from what is already stored. A wrong merge is repairable
only by splitting the entities and re-importing the source file — which
requires noticing it first, and its symptom is a row that is quietly
absent.

## Every list call is paginated

`list_merchant_rules`, `list_entities`, `list_categories` — all of them
default to `limit=50` and return `{items, total, limit, offset}`. One
call is **not** the whole set. Drain it:

```python
def fetch_all(list_tool, **kwargs):
    items, offset = [], 0
    while True:
        page = list_tool(limit=200, offset=offset, **kwargs)
        if not page["items"]:
            return items          # empty page: stop, never spin
        items += page["items"]
        if len(items) >= page["total"]:
            return items
        offset += len(page["items"])
```

Always compare against `total`. Raising `limit` is not a fix — it moves
the cliff rather than removing it, and `MAX_LIMIT` is 200.

The empty-page guard is load-bearing, not defensive habit: `total` is
counted in a separate query from the page, so a row deleted between the
two leaves `total` permanently above what any offset can return. Without
the guard the loop advances by zero and spins forever — an unattended run
hanging silently rather than failing.

This is not hypothetical and not only about rules. A modest installation
easily has, say, **90 categories** and **70 merchant rules**, both past the default. A
short first page looks exactly like a small dataset, so nothing about the
result says it was truncated — every consequence below is a confident
wrong answer, never an error:

- **Categories (90, so 40 unseen).** Step 7 says never to guess a
  category and to ask when none fits. With a truncated list the right
  category can be one of the 40 you never saw, and "no fitting category
  exists" is itself a documented legitimate outcome — so the mistake
  arrives wearing the shape of a correct answer.
- **Merchant rules (70, so 20 unseen).** The preview disagrees with the
  import, which the rule below says to treat as a stop.
- **Accounts.** A matching account past the first page reads as absent,
  so you create a duplicate — and an account entity is load-bearing for
  de-duplication, as above.

### 3. Parse rows from the scrubbed text

Per `${CLAUDE_PLUGIN_ROOT}/runbooks/statement-intake.md`, which carries the per-format
quirks. The parts that bite:

Read that runbook's description and period rules before parsing. Preserve
merchant text, case and punctuation; normalize whitespace and remove only
standalone `[REDACTED]` tokens. Masked suffixes can remain as source detail,
never merchant identities or rule patterns. Do not invent missing text.
Keep every occurrence, including identical purchases, with its stable source
locator. Description similarity is review evidence, never duplicate proof.

Use source-supported table membership, not date bucketing or nearest headers.
For exports without a billing period, use observed posted-date extent and
identify it as an activity window, not complete coverage. Report pending rows
separately; do not import empty statements from headers alone.

- **Dates → ISO `YYYY-MM-DD`.** Bank C exports `MM/DD/YYYY`.
- **For Bank C, `description` is the `Payee` column only.** Never `Payee + Note`.
  Merchant rules match against `description`, so a description that
  sometimes carries a memo makes rule matching unpredictable and quietly
  degrades the taxonomy. One field, always.
- **Split multi-period documents into billing periods.** A card
  statement download can cover several months; importing it as one statement makes the
  period meaningless for reconciliation.
- **Signs pass through — in the CSV exports.** Debits are already
  negative in the Bank C source. Do not invert. Use the redundant
  `Kind` column as a free check: assert the sign matches
  `DEBIT`/`CREDIT`. This holds for the CSV export formats **only**; see
  below before assuming it of a PDF.

#### PDF signs depend on what the balance represents

PDF statements can show unsigned amounts and a running balance. First
identify whether the balance is **money held** or **money owed**. q-core
records purchases/charges as negative and money received as positive:

- Bank deposit account (money held): `amount_cents = after - before`.
- Credit card or line of credit (money owed): `amount_cents = before - after`.
  Interest and purchases increase debt and are negative; payments, refunds
  and cashback decrease debt and are positive. Never apply the deposit-account
  formula to a debt balance.

`before` and `after` mean chronological balances, not adjacent printed rows:
newest-first tables run backward. Resolve the source's debit/credit columns
and balance meaning before deriving signs. If either is unclear, stop.

Then verify: `|after - before|` must equal the amount the row
states. A row that fails is a **stop**, not a guess. A parser without this
check is guessing the sign of every transaction on the statement.

Two things this catches:

- **`pypdf` splits money tokens across lines** — for example `1,234` then
  `.56`. Rejoin `N,NNN` + `.NN` before parsing. When the split token is a
  *balance*, one row parses with a single number and every later delta is
  computed from a stale balance. A small
  deposit came out as a large negative: plausible number, wrong sign, no error.
- **Page headers and summary lines bleed into descriptions.** A
  continuation page repeats the account holder's name and the statement
  period mid-row; a `Total` line's amounts get picked up with the previous
  row's date. Anchor the pattern so a description can contain **no `$`**
  and no second date, and cut it at the page-break marker.

  This is the dangerous shape, not a cosmetic one: **the amounts are
  right, so the row reconciles, and nothing downstream objects.** A
  description is matched by merchant rules and used as
  overlap review evidence, so header text makes a row unmatchable and makes
  review depend on where a page happened to break. Such a row can put an
  interest rate or credit limit inside a transaction description, or shift
  a statement period's apparent closing balance by exactly the amount it
  swallowed.

**Check the opening balance against the previous statement's closing
balance.** Consecutive statements for one account chain, so a mismatch
means a missing statement, a misread balance or the wrong account — and
it is the cheapest cross-file check available.

**"Debit" is not a direction.** On a chequing statement the withdrawn
column *decreases* the balance and is money leaving. On a line of credit
the "Debits" total *increases* the balance, because the balance is what
is owed, and the debits are interest. Key off the balance delta, never
off the word. For money owed, negate the chronological balance increase.

### 4. Convert amounts to integer cents

`amount_cents` is an integer. A fractional value is rejected, not
rounded.

```python
from decimal import Decimal
amount_cents = int(round(Decimal(text) * 100))   # "-52.10" -> -5210
```

**Never `int(float(text) * 100)`.** It truncates toward zero and is wrong
on about one in twenty two-decimal values — `-19.99` becomes `-1998`,
`1.15` becomes `114`. It is *correct* for `-52.10`, which is what makes
it dangerous: spot-checking one amount does not show the bug.

### 5. Preview the categorization

Fetch **every** rule with the draining loop above — not one call. Then,
for each row, find the matching rules locally and apply **longest pattern
wins, ties to the lowest `id`** (`${CLAUDE_PLUGIN_ROOT}/runbooks/merchant-rules-conventions.md`).

At, say, 70 rules a single default call returns 50, and the 20 it omits
are invisible in exactly the way that matters: the preview reports those
merchants as unmatched, the import categorizes them correctly, and the
two disagree for a reason that has nothing to do with matching.

Match on **token boundaries, not bare substring**. A pattern matches only
where it is preceded by the start of the description or a non-alphanumeric
character, and followed by the end or a non-alphanumeric character.
Case-insensitive.

```python
import re

def matches(pattern, description):
    return re.search(
        rf"(?<![A-Za-z0-9]){re.escape(pattern)}(?![A-Za-z0-9])",
        description, re.IGNORECASE,
    ) is not None

# Filter FIRST, then rank what actually matched.
hits = [r for r in rules if matches(r["pattern"], description)]
winner = min(hits, key=lambda r: (-len(r["pattern"]), r["id"])) if hits else None
```

**Filter then rank — never rank then check.** A longer pattern that fails
the boundary test must not beat a shorter one that passed. Against
`BLOFESSORS PUB`, the pattern `BLOFESSORS PU` is longer but is not
token-bounded there, so `PUB` wins. Ranking first and testing only the
winner silently returns the wrong category on exactly these.

**Ties go to the lowest `id`.** This mirrors the API's
`ORDER BY length(pattern) DESC, id ASC` (q-core's `api/financial.py`), which ranks
first and then takes the first row that survives the boundary test — the
same two steps in the same order.

`min` with a negated length rather than `max`, because the two keys sort
in opposite directions and an `id` is a string that cannot be negated. So
the numeric half carries the direction: `-len` makes longest smallest,
and `id` then ascends naturally.

**This is a robustness change, not a behaviour change, and the explicit
key is the point.** `max` returns the *first* maximal element of the list
it is handed, and `list_merchant_rules` returns `ORDER BY id`, so the
older `max(hits, key=len)` already resolved ties to the lowest id — but
only as a coincidence of someone else's `ORDER BY`, in a different file,
which nothing recorded and nothing tested. Re-sort the rules for any
reason, or page them out of order, and the old form silently picks a
different category. The explicit key stops the preview depending on how
its input happened to arrive. (Found by impl-3, cross-checking the
mirror: a probe that feeds the fence an id-ordered list cannot observe
this change at all, which is why the test for it hands the fence a
deliberately reversed list.)

Lowest id is **arbitrary with respect to age** — ids are uuid4 and
`merchant_rules` has no `created_at`, so for example `SHELL` can beat
`MARTZ` despite being written later. That is deliberate. What the
tie-break must be is deterministic and stable across re-imports, because
a category is written at import time and a tie resolved differently on a
later run would silently recategorize. `rowid` would mean age, but the
table has no `INTEGER PRIMARY KEY`, so `VACUUM` renumbers it — and this
repo's backup path is `VACUUM INTO`, which would change categories on
restore. `rowid` is also absent from the API response, so the preview
could not mirror it even if we wanted to.

`re.escape` is load-bearing, not habit: patterns can contain dots and
stars (`Shopco.ca`, `SHPC Mktp CA`), and unescaped `Shopco.ca` matches
`ShopcoXca`.

Digits are alphanumeric, so `ESSO` does **not** match `ESSO1234` — but it
does match `ESSO 1234 ANYTOWN` and `FUELCO/ESSO`, because the
separator is the boundary.

Bare substring is wrong in a way that is invisible until you look: `ESSO`
matches `ZESSOR ACADEMY`, `SHELL` matches `ZUMSHELLA SALON`. A category
assigned at import time **persists on the transaction row**, so a false
match is not a display quirk — it is a wrong number in every report built
on it, sitting under a plausible-looking category nobody re-examines.

Boundaries are about the characters *around* the match, so patterns with
internal punctuation still work: `MEGA-MART` matches `MEGA-MART #4321
ANYTOWN XY`, `AMAZON` matches `AMAZON.CA*AB12`, and `UBER` matches
`UBER CANADA/UBEREATS` at the leading token while correctly not matching
inside `UBEREATS` itself.

**This must mirror the API's matcher exactly** — the canonical rule and
implementation live in `${CLAUDE_PLUGIN_ROOT}/runbooks/merchant-rules-conventions.md` under
"What counts as a match". The preview's value is that it predicts the
import; if the two rules differ, the preview is confidently wrong, which
is worse than showing nothing.

**If the preview and the import disagree on a real description, stop and
report it.** Do not adjust the preview to match, and do not carry on
because the import "looks right" — the two implementing the same rule is
the property that makes the preview worth showing at all, so a
disagreement means one of them is wrong about money and you do not yet
know which.

Show the user what will happen before importing: how many rows, the
period, how many will categorize, and the list of merchants that will
not. This preview is local and advisory — the API does the real
categorization on import.

### 6. Import

**Source-tracked preview/commit is the only statement import workflow.** Read
`${CLAUDE_PLUGIN_ROOT}/runbooks/source-tracked-imports.md`. Preserve `source_id` and `text_sha256`
from server extraction. Give every row a stable locator from the approved
scrubbed source, never extraction-output order. Preview before writing; resolve
`needs_review` with source evidence or operator confirmation. Stop on
`source_conflict`, unreliable locators, or unresolved overlaps. The legacy
`import_statement` tool and endpoint are retired; there is no fallback.

**A `near_date` match is a probable duplicate, not a new purchase.** Preview
lists same-account, same-cents candidates up to 3 days from the row's date,
because two exports can date one charge differently: interest on the 31st in
one, the 1st in the other. Link it
when the evidence shows one charge (a balance that only fits one, the other
source's posting convention) and say which in the reason. Decide `new` only
with evidence of a second, distinct charge. Never commit a `needs_review` row
without deciding it, and never widen or narrow the window yourself.

**Own-account transfers appear on both sides.** Once more than one account
is imported, a transfer between them is two rows: negative in the source,
positive in the destination. That is correct and the taxonomy expects it
(`transfers_account_transfer`, `transfers_credit_card_payment`).

`spending_summary` excludes own-account transfer categories by default. Keep
both source and destination transactions; do not delete or net them during
extraction. Preserve the operator's approved transfer classification policy.

Call `commit_source_import` with the exact previewed facts and review token,
plus justified new/link decisions where required. Report `created`, `linked`,
and `recognized` separately. On a timeout, preview again before retrying.
Read back returned transaction IDs with `list_transactions`, draining all
pages for this account; compare new rows' categories against the advisory
preview. Links and recognitions preserve existing classifications. Rows with
`category_id=null` are unmatched and need step 7; never report an import as
fully categorized just because it committed.

### 7. Propose rules for the unmatched

For each unmatched merchant, propose a **generalized** pattern and ask
for confirmation before creating anything.

Generalized means the merchant, not the transaction: `MAPLE DONUTS`, not
`MAPLE DONUTS #4821 ANYTOWN XY`. Bank descriptions carry store numbers,
locations and transaction ids that differ every time, so an exact pattern
matches that one charge and never the next.

Broad patterns are safe because precedence is longest-pattern-wins: a
more specific rule added later takes over for that merchant without
editing or breaking the broad one.

- On confirmation → `create_merchant_rule(pattern, category_id)`.
- Only for an explicit "always categorize *exactly this* one" →
  `apply_transaction_as_rule(transaction_id)`, which learns the full
  description.

Before creating a rule, check whether its pattern nests with an existing
one — either contains it or is contained by it, case-insensitively, since
matching is.

Nesting is **not** itself a problem: it is the mechanism. A broad
`AMAZON` alongside a specific `AMAZON WEB SERVICES` is the intended
shape, and longest-pattern-wins resolves it. The hazard is nesting you
did not notice **where the two carry different categories**, because the
broader rule then silently captures transactions meant for the narrower
one. Treat a hit as a question — *is this overlap intended, and is each
category right?* — not as a failure to avoid.

Never guess a category. If the right one is not obvious from
`list_categories` (drained — the category count exceeds the default page), ask.

**If nobody is available to confirm** — an unattended or autonomous run —
do not create rules on your own judgement, and do not leave the rows in a
worse state to force the question. Import them, leave `category_id` null,
and record the unmatched merchants in the import log as *awaiting
confirmation*. An uncategorized transaction is an honest gap that a later
rule fixes retroactively; a guessed rule is silent misattribution that
compounds on every future import and nobody re-examines.

Some merchants cannot be resolved by any rule, and saying so is the right
answer rather than a failure:

- **No fitting category exists.** The taxonomy is deliberately fixed
  (`${CLAUDE_PLUGIN_ROOT}/runbooks/category-taxonomy.md`), so a merchant with no home needs a
  human decision about the taxonomy, not a stretched mapping into the
  nearest category.
- **One merchant string covers two kinds of spending.** A combined
  rides-and-delivery descriptor cannot be split by any substring pattern.
  Leave it uncategorized and say why; the fix is transaction-level, not a
  cleverer pattern.

### 8. Check coverage, and stop on a gap

`statement_coverage(account_id=<this account>)` after every import.

**A gap is a STOP.** It means a statement period nobody has imported:
money moved in that window and q-core has no record of it, so every
total from here on silently understates. Report the gap with its dates
and ask — do not carry on, and do not treat the import you just ran as
finished because it succeeded on its own terms. `created` reports what
arrived, never what should have.

**Coverage overlap is not duplicate proof.** Overlapping export windows are
normal, but unknown source occurrences require explicit review before commit.
Report resolved links separately from genuinely new purchases.

**This cannot tell you a page is missing, and nothing here can.** A
statement imported without its second page looks whole from every angle
available after the fact: its rows are present, its period is intact,
and it sits flush against its neighbours. The balance chain does not
close that gap either — it reconciles *within* the pages you have, so
the run that lost the page also read the closing balance off a page it
did have.

That is why the `partial_extraction` refusal in step 2 is a stop rather
than something to work around with `allow_partial`. **It is the only
point in this procedure where a missing page is still visible.** By the
time you are here it is not, and no check you can add later will make it
so.

## Before a real import

- Take a WAL-safe backup first, on the server, from any machine:
  `"${CLAUDE_PLUGIN_ROOT}/scripts/q-core-server.sh" --ssh "${user_config.server_ssh}" --checkout "${user_config.server_checkout}" backup`
  (it runs `python -m api.backup` in the server's checkout, over OpenSSH
  unless this is the server). It prints one JSON line with the backup's
  path; a non-zero exit is a stop. Never `cp` the database: in WAL mode
  recent commits live in the `-wal` sidecar and a plain copy can silently
  miss them.
- Log the result with `create_note`, titled `Import log: <statement>`:
  source file, backup path, rows offered / created / linked / recognized,
  unmatched merchants, and any tool refusal verbatim with digits masked.
  (Before the split this went into a repository runbook; a note is
  reachable from every machine.)
- Stop at the first refusal or anomaly and report it rather than
  continuing to the next file.

## Related

- `${CLAUDE_PLUGIN_ROOT}/skills/financial-evals/SKILL.md` — offline regression measurements,
  not a prerequisite or substitute for a real import; run only when requested.

- `${CLAUDE_PLUGIN_ROOT}/runbooks/statement-intake.md` — per-source quirks and the
  normalization contract.
- `${CLAUDE_PLUGIN_ROOT}/runbooks/merchant-rules-conventions.md` — precedence, and why a
  correction adds a rule rather than editing one.
- `${CLAUDE_PLUGIN_ROOT}/runbooks/category-taxonomy.md` — the fixed two-level tree;
  `Uncategorized` should trend toward zero, which is what step 7 is for.
