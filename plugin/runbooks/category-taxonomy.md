# Category taxonomy

Fixed, two-level taxonomy stored in the `categories` table (`db/schema.sql`),
seeded from `db/seed_categories.sql`. FK'd from `transactions.category_id`
and `merchant_rules.category_id` — no freeform category strings, so typos
and casing variants ("Groceries" vs "groceries") can't happen.

## Design rule: category = kind of spending, not which asset

`transactions.entity_id` already says which property/vehicle/account a
charge belongs to. Category never forks on asset ("Rental Utilities" vs
"Home Utilities") — it's always just `Housing > Utilities`, disambiguated
by the `entity_id` join. This keeps the tree an order of magnitude smaller
than a per-asset taxonomy would be.

Two exceptions worth naming explicitly since they could look like
oversights otherwise:
- `Housing > Property Tax` covers rental property tax too, via `entity_id`.
  The top-level `Taxes` category is *only* for personal income tax.
- `Insurance` (top-level) is for policies not tied to one asset (health,
  life, umbrella). Home and auto insurance premiums live under `Housing`
  and `Auto` respectively, since those charges do belong to a specific
  asset.

## Current tree

See `db/seed_categories.sql` for the authoritative list — top-level
categories: Housing, Auto, Food, Insurance, Healthcare & Medical, Personal
& Shopping, Entertainment & Subscriptions, Travel, Education, Childcare,
Pets, Gifts & Charity, Income, Transfers, Fees & Interest, Taxes,
Business, Hobbies, Uncategorized.

A category added at runtime through `POST /categories` gets the id
`_unique_category_slug` generates (`{parent_id}_{slug}`); when such a
category is later added to the seed, the seed uses the same id, so a fresh
install and the live database agree.

## `Transfers` is a non-spending branch

Money moving between the owner's own accounts is not spending. Paying a credit
card from chequing should land under `transfers` on **both** sides, and
`spending_summary` and `trend` **exclude the whole branch by default**
(`exclude_transfers`, default true, added for ticket T-40). Exclusion is
by `parent_id`, so renaming a category in the taxonomy cannot switch it
off, and it is the branch root plus its children — the tree is fixed at
two levels, and anything deeper would need the query made recursive.

Signed summing alone was the earlier answer and it is not enough. It
nets the two legs only when both fall inside the same query, which
fails in three ordinary situations:

- **A period boundary.** Sent on the 31st, posted on the 1st: one leg in
  each month. March is short by the whole transfer and April is long by
  it, and each month looks reasonable on its own.
- **One side not imported.** Until every account is in, one leg exists
  and the other does not.
- **One side not categorized.** For example, a card's payment inflows
  are labelled and their outflow twins are not.

Both aggregates report `transfers_excluded_cents` **and**
`transfers_excluded_count`. The count is not redundant: once both legs
are labelled they cancel, so the cents read `0` in exactly the healthy
case, and `0` alone cannot be told apart from "there were no transfers".

One exception, applied automatically: `trend` asked for a transfers
category **by id** returns that series regardless of the flag. An empty
series is indistinguishable from "you made no payments", which is a
thing people act on.

**What is a transfer, and how we know.** Whether an outgoing transfer to
a scrubbed counterparty is spending or an own-account movement cannot be
proven from the description: the scrubber removes the counterparty. That is
a policy decision for the operator; if it is made, such descriptions are
filed under `transfers_account_transfer` by rule. Own-account movement
between two accounts at the same bank is identified by the masked last four
matching an account entity's `last4` (`TRANSFER TO ****1234`), or by an
unambiguous literal (`ONLINE PAYMENT TO CARD`).

The safeguard that replaces the warning is a net check, not a rule: over
any period every account covers, transfers out should roughly equal
transfers in. A net that does not close means a statement is missing or
a leg is not really a transfer, and the Spend Cadence report shows that
net per month for exactly this reason. If a person is ever paid by
transfer under such a rule, that is where it will show.

`housing_down_payment` is for a capital outflow tied to one property, with
`entity_id` naming the property. It is housing
spending, not a transfer: the money left rather than moved.

`Uncategorized` is a leaf with no children — the catch-all for anything
`merchant_rules` hasn't matched yet. Healthy state is this trending toward
zero as rules accumulate, not a permanent bucket.

## The `null` key in `spending_summary`

`spending_summary` groups rows that have no value for whatever it is
grouping by under a `key` of **`null`**. That is the contract, not a
defect (D48), and it stays in `items`.

**It means "not grouped", and what that means depends on `group_by`** —
no category, or no entity. The companion figure in the same response
tracks it: `uncategorized_cents` with `group_by=category`,
`unattributed_cents` with `group_by=entity`. Exactly one of them
appears, and it always equals the `null` bucket's total under the same
filters. Before D48 the field was always `uncategorized_cents`, so an
entity summary carried a null bucket meaning "no entity" beside a field
meaning "no category" — two numbers, one response, nothing saying which
was which.

**The bucket cannot be dropped from `items`.** Removing it turns "total
spending" into "total *categorized* spending" with no sign that it has:
a caller summing `items` would report a smaller figure with nobody to catch it.

**The key must not be renamed to a sentinel, and this is the part worth
remembering, because the proposal sounds obviously right.**
`uncategorized` is a **real category id** in this taxonomy — top-level,
seeded. A sentinel of that name would merge "no category" with
"explicitly filed as Uncategorized" and sum them silently. And no other
string is safe either: the taxonomy is edited by plain SQL (see below),
so any sentinel is a category somebody can create later, and the
collision would be invisible on the day it happened.

`trend` keeps `uncategorized_cents` in both of its forms. Its items are
months, so it has no null bucket to agree with, and a series filtered to
one entity cannot contain an unattributed row — `unattributed_cents`
would name something the response cannot hold.

## Changing the taxonomy

Editing the list is a SQL insert/update against `categories`, not a schema
migration — "fixed" means "no freeform typing," not "hard to change."
Business/Childcare/Education were included as a reasonable default set;
prune them (or add new top-levels) if they don't reflect your actual
situation.

## Not built (yet)

Tax-prep-aligned subcategories (e.g. matching Schedule E line items for
rental property — advertising, cleaning/maintenance, commissions, legal,
management fees, mortgage interest, repairs, supplies, utilities, etc.)
would make year-end filing easier but is more granularity than v1 needs.
Revisit if/when tax-prep export becomes a real workflow.
