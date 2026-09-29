# Merchant rules conventions

`merchant_rules(pattern, category_id, entity_id)` auto-classifies
transactions during `statement-intake` before asking you to confirm
whatever's left unmatched.

## What counts as a match: token-bounded, not bare substring

A `pattern` matches a description only where it appears as a **whole
token** — preceded by the start of the string or a non-alphanumeric
character, and followed by the end of the string or a non-alphanumeric
character. Case-insensitive, as before.

It was bare substring until 2026-09-19, and that was a live defect rather
than a simplification: `ESSO` matched "ZESSOR ACADEMY", "ESPRESSOVILLE CAFE"
and "BLOFESSORS PUB"; `SHELL` matched "ZUMSHELLA SALON" and
"SHELLWORTZ MARKET". A tutoring charge was categorised as auto fuel.

**Longest-pattern-wins cannot fix this**, which is the part worth
remembering before reaching for it: that rule resolves a short form losing
to a longer form of the *same* merchant. It does nothing about a pattern
buried inside an unrelated word, because the unrelated word is not a rule.

Descriptions keep working because banks separate fields with
non-alphanumerics, for example (invented): `ESSO 1234 ANYTOWN`,
`FUELCO/ESSO`, `Shopco.ca*482KX7RT2`, `SHPC Mktp CA*1A2B3C`.

Digits are alphanumeric, so `ESSO1234` does **not** match. Deliberate: the
costs are asymmetric. An unmatched transaction lands in `unmatched` and
asks a human; a miscategorised one answers wrongly and never asks again,
because the category is written onto the row at import time and a later
fix does not retroactively re-categorise anything. Whoever writes a rule
for a merchant whose descriptions run straight into digits should write
the pattern to include them.

Anything consuming these rules outside the API — the intake skill's local
preview, for one — must mirror this rule exactly, or its preview count
will disagree with what the import actually does.

## Match precedence: decided

When a transaction description matches more than one rule's `pattern`
(e.g. a broad rule `"AMAZON"` and a narrower one `"AMAZON WEB SERVICES"`
both match `"AMAZON WEB SERVICES AWS.AMAZON.COM"`), **the longest matching
pattern wins.** Computed at match time from `LENGTH(pattern)` — no
priority column, no manually maintained rule ordering. Most-specific-wins
is the intuitive default and needs zero upkeep as the rule list grows over
years.

### Equal lengths: the lowest id wins

Two patterns of the same length can both match one description
(`MARTZ` and `SHELL` are both five characters; a description containing
both is all it takes). **The lowest `id` wins.** The matcher's query is
`ORDER BY length(pattern) DESC, id ASC` and takes the first row that
survives the token-boundary test.

Lowest id is **arbitrary with respect to age**, and the wording matters
because the obvious reading is wrong: `merchant_rules.id` is a uuid4 and
the table has no `created_at`, so id order has nothing to do with the
order the rules were written. Anyone who reads "lowest id" as "the
one I wrote first" will predict the wrong winner.

What it buys is the part that matters: the answer is deterministic, it
is stable across re-imports, and it is **identical to what the intake
skill's preview computes**, so the preview cannot promise one category
and the import write another. Before this, the API resolved ties by
whatever order SQLite scanned the table in — insertion order in
practice, guaranteed by nothing — while the preview resolved them by id.

**Not `rowid`, although `rowid` really is insertion order.** It is the
obvious way to get literal oldest-wins for free, and it is a trap:
`merchant_rules` has no `INTEGER PRIMARY KEY`, so its rowids are not
stable — `VACUUM` renumbers them, and this repo's documented backup path
is `VACUUM INTO` (see `runbooks/decisions-log.md`). A rowid-based
tie-break would silently change which category a re-import writes the
first time anyone restores from a backup: a wrong number about money,
arriving from an operation nobody would connect to merchant matching.
`rowid` is also absent from the API response, so the preview could not
reproduce it. Found by impl-1.

Mirror the full key, not just the length, in anything that matches these
rules outside the API:

```python
# min with a negated length, not max: max() breaks a tie toward the
# first element of whatever order it was handed, which is the bug.
winner = min(hits, key=lambda r: (-len(r["pattern"]), r["id"]))
```

If a tie ever needs a *deliberate* winner rather than a stable one, add
an explicit `priority` column then — don't pre-build it. Age semantics
would need a `created_at` column too; note that backfilling one would
give all existing rules the same timestamp, so it would change
nothing until new rules are written.

## When rules take effect: decided

Rules run **at import time** and on an **explicit reapply**. Nothing else
re-runs them. So adding or correcting a rule changes nothing already
stored — "we added the rules" and "the totals reflect them" are different
claims until `reapply_merchant_rules` runs: rows imported before a rule
existed stay uncategorized until then.

**A reapply never re-categorizes a row that already has a category.** It
touches only rows with no category and no entity. Two consequences worth
stating, because both are the opposite of the natural assumption:

- Correcting a **wrong** rule with `update_merchant_rule` does **not**
  retroactively fix the rows it already miscategorized. Those keep the
  wrong category. Fixing them is a separate, explicit decision — per-row
  correction, or a deliberate re-import.
- A reapply is therefore safe to run repeatedly and is idempotent: a
  second run reports zero.

**Archived imports are excluded.** Reapply reads `active_transactions`,
so an archived statement's rows are not reclassified — archiving says
"this import should not count", and silently re-categorizing its rows
would make it count again in the one column a reader trusts.

Dry run is the default. The response lists every id it changed, and
since there is no undo, that list is the only record of what moved.

## Correcting a transaction: decided

Correcting a transaction's category **only ever updates that transaction**
by default — it never silently rewrites an existing rule. Mutating a
broad rule (e.g. `"AMAZON"`) because one purchase was miscategorized would
misclassify every other transaction that rule already matches, past and
future.

If the correction should apply going forward, ask once: "always
categorize [merchant] this way?" On yes, **create a new, more specific
rule** (e.g. the full/longer description as `pattern`) rather than editing
the existing broad one — precedence (longest pattern wins, above) means
the new rule takes over for that merchant without touching the old rule's
behavior for anything else.

Directly editing an existing rule's classification ("the STARBUCKS rule
is just wrong") is a separate, explicit action — never an implicit side
effect of correcting one transaction.
