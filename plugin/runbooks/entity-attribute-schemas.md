# Entity attribute schemas

Per-type shape of `entities.attributes` (JSON), and the `entity_relationships`
vocabulary that ties entities together. SQL just stores/queries this; the
shape contract is enforced by Pydantic models at the API layer.

Entity types: `person | property | vehicle | pet | account | project`. (Documents are
not an entity type — see `documents` table, linked via a direct `entity_id`.)

## person

```json
{
  "date_of_birth": "1990-01-15",
  "relationship_to_you": "spouse",   // self | spouse | child | dependent | tenant | other
  "id_last4": "1234"                 // last 4 of DL/passport if useful — never full SSN
}
```

Tenants are `person` entities with `relationship_to_you: "tenant"` — see
`leases` relationship below for lease terms.

## property

```json
{
  "address": {"street": "...", "city": "...", "state": "...", "zip": "..."},
  "property_type": "rental",         // single_family | condo | land | rental | multi_family
  "purchase_date": "2015-05-01",
  "purchase_price": 350000,
  "year_built": 1990,
  "square_footage": 1800,
  "insurance_renewal": "2026-11-01",  // forward-dated -- see "Forward-dated attributes"
  "tax_due": "2026-02-28",            // forward-dated
  "mortgage": {                      // omit entirely if paid off / not applicable
    "lender": "...",
    "origination_date": "2015-05-01",
    "term_years": 30,
    "rate": 4.1
  }
}
```

No loan account numbers here — the mortgage as a payable *account* (for
transaction tracking) is a separate `account` entity linked via `finances`.

**Confirmed for now:** one `property` entity = one rentable unit. Multi-unit
buildings (duplex, etc. with independently-leased units) aren't modeled —
tracked as a `project-management.json` todo.

## vehicle

```json
{
  "vin": "...",
  "make": "Toyota", "model": "RAV4", "year": 2019,
  "license_plate": "...",
  "purchase_date": "2019-06-15",
  "purchase_price": 28500,
  "registration_expiry": "2027-03-31",  // forward-dated
  "warranty_end": "2027-06-15"          // forward-dated
}
```

No mileage/odometer tracking (decided out of scope).

`registration_expiry` reverses this section's earlier position that
registration expiry is "a `reminders` row, not a duplicated fact" (D17).
It is not a duplicate while no `reminders` row exists, and holding it on
the vehicle means the digest derives the date from the thing itself
rather than from a parallel record that can drift out of step with it.
A `reminders` row remains the right home for anything with a real
recurrence rule; these attributes carry a single next occurrence.

## pet

```json
{
  "species": "dog", "breed": "...", "date_of_birth": "2020-01-01",
  "microchip_id": "...",
  "vaccination_due": "2026-05-12"    // forward-dated
}
```

## Forward-dated attributes

Most date-typed attributes are historical — `purchase_date`,
`opened_date`, `date_of_birth`, `origination_date` all record something
that already happened, and a "what's due" query has no use for them.
These are the exceptions, and they are the complete set the `/due`
digest reads from entity attributes:

| Entity type | Attribute |
|---|---|
| `property` | `insurance_renewal`, `tax_due` |
| `vehicle` | `registration_expiry`, `warranty_end` |
| `pet` | `vaccination_due` |
| `account` | `renewal_date` |

Each holds the **next** occurrence, not a series. Once the date passes
the value is stale until something rolls it forward, so `/due` reports a
past one as overdue rather than filtering it out — a stale attribute
should be visible, not silently absent. Anything with a genuine
recurrence rule belongs in `reminders` (RRULE) instead.

Insurance renewals need no new attribute: a policy is an `account`
entity with `account_subtype: "insurance"` and it already carries
`renewal_date`, and where a policy is modelled as a relationship the
relationship's own `end_date` carries the date. Adding a third place to
write the same fact is how the three disagree.

Adding a forward-dated attribute means adding it to this table too — the
digest reads the list, and a field that is not on it is invisible to
"what's due" with no error to say so.

## account

Bank, credit card, loan, investment, and insurance are all `account`,
discriminated by `account_subtype`:

```json
{
  "institution": "Chase",
  "account_subtype": "credit_card",  // checking | savings | credit_card | loan | investment | insurance
  "last4": "4242",
  "opened_date": "2018-01-01",

  "interest_rate": 5.75,             // loan/mortgage only

  "policy_type": "homeowners",       // insurance only: homeowners | auto | umbrella | life | health
  "premium": 1800,
  "renewal_date": "2026-03-01"
}
```

Filtering on `account_subtype` etc. uses SQLite `json_extract()`. Add an
expression index (`CREATE INDEX ... ON entities(json_extract(attributes,
'$.account_subtype'))`) only if a query pattern turns out to be hot —
don't pre-optimize.

## project

```json
{
  "description": "free text, optional",
  "repository_path": "projects/bakery-app"
}
```

Both fields are optional; existing and non-software projects need no changes.
`repository_path` is the canonical repository-relative submodule path:
`projects/<slug>`, where a slug contains lowercase ASCII letters/digits,
optionally separated by single hyphens. Absolute paths, traversal, nested paths,
spaces, uppercase and trailing slashes are rejected, rather than normalized.

One path belongs to one project, including archived projects. Create and update
return HTTP 409 on a duplicate; the check and write share a SQLite writer lock
so concurrent requests cannot both assign it. To transfer a path, first remove
it from the old project's attributes. Attributes updates replace the whole
object: read first and preserve the other fields. Null or omission within the
replacement object removes the association; omitting `attributes` leaves it alone.

This is a logical association, not proof that a checkout exists. Factory tooling
must verify the registered submodule, initialization, repository identity and
filesystem containment before execution. Reuse `boards.entity_id` for boards;
multiple boards require explicit selection. Git remains authoritative for remote
URLs and pinned commits, and each project owns its technical instructions.

The title is `entities.name`, not an attribute,
and everything else about a project lives in the rows that point at it —
boards, notes, documents, reminders, transactions. Resist adding status
or dates here: `entities.status` already carries `active`/`archived`, and
a project's real progress is its board.

## entity_relationships vocabulary

| type | direction | meaning |
|---|---|---|
| `owns` | Person → Property/Vehicle/Account | ownership |
| `resides_at` | Person → Property | lives there |
| `leases` | Person(tenant) → Property | rental agreement — carries `start_date`/`end_date` + `attributes: {rent_amount, security_deposit}`; a renewal is a new row, not an update |
| `insures` | Account(insurance) → Property/Vehicle | policy covers this |
| `finances` | Account(loan/mortgage) → Property/Vehicle | debt financing it — how `get_entity_cost_of_ownership` walks from a property/vehicle to its loan's transactions |
| `maintains` | Person → Property/Vehicle | responsible for upkeep |
| `spouse_of`, `parent_of` | Person → Person | family structure — symmetric types (`spouse_of`) are inserted once; query both `from`/`to` sides rather than double-writing |

No DB-level `UNIQUE` constraint on this table — see schema.sql comment.
Point-in-time types (`owns`, `insures`, `finances`, `maintains`,
`spouse_of`, `parent_of`) should be deduped at the API layer before insert;
`leases` (and other dated types) are expected to accumulate multiple rows
over time.

## Notes

Freeform, attachable to anything via `note_links(target_type, target_id)`
where `target_type` is `entity | transaction | document | reminder |
statement`. No SQL FK on `target_id` (polymorphic) — the API layer checks
the target exists before creating a link.

### Birthday reminders

`person` and `pet` accept optional `birthday_reminder_enabled` (boolean; absent
or null means enabled). Saving an active entity with `date_of_birth` maintains
one linked annual reminder; false opts out. Removal/deactivation removes the
generated reminder. See [Calendar lifecycle](google-calendar.md#reminder-lifecycle)
for timing, leap days, overrides and the explicit existing-record backfill path.
