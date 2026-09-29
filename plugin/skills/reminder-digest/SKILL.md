---
name: reminder-digest
description: Answer "what's due" — renewals, expiries, lease and policy end dates, and reminders — from q-core, grouped by urgency, with the supporting documents attached. Use when asked what's due, what's coming up, what needs renewing, what's expiring or overdue, or for a weekly/monthly digest.
---

# Reminder digest

## Never — read this before anything else

Every failure this skill can produce is **silence**: a confident report
that nothing is due. Nothing errors, nothing looks wrong, and the thing
you would have used to notice is the report you just got wrong. That is
why these come first.

**Never answer "what's due" from `list_reminders`.** It lists reminders
as they were *defined*: one row per repeating series however many times
it fires, and nothing at all for renewals held as entity attributes — a
vehicle's `registration_expiry`, an account's `renewal_date`, a policy's
`end_date`. Today those attribute and relationship sources are where
almost everything lives, so a digest built from `list_reminders` can be
empty while several things are overdue. **`list_due_items` is the only
"what's due" query.** Use `list_reminders` only to find a specific
reminder you are about to change or delete.

**Never treat an empty result as "nothing is due" without saying what
you asked.** An empty page and a wrong query look identical. Before
reporting nothing, state the window you used and the total you got, so
the user can see the question rather than only the answer. If `total`
is 0 for a 30-day window, say "nothing due in the next 30 days" — never
"nothing is due".

**Never widen or narrow the window silently.** The user's question sets
it. "What's due this month" is not the default 30 days from today, and
reporting the default under that question is a wrong answer wearing the
shape of a right one.

**Never edit an attribute to clear an overdue item.** An overdue
`registration_expiry` means the date passed, not that the data is wrong.
Moving it forward asserts that a renewal happened — a claim you cannot
make on the user's behalf, and one that silently removes the only
warning they had. Ask. When they confirm the renewal, update the
attribute to the *new* expiry they give you, never to a guessed
+1 year.

**Never complete or snooze an occurrence the user did not name.**
`complete_reminder` and `snooze_reminder` take a `due_date` that
identifies *which* occurrence of a recurring reminder. The wrong one
succeeds, returns 200, and quietly clears a different week. Pass the
`due_date` exactly as `list_due_items` reported it — never today's date,
never the next one, never a reconstruction.

**Never manually duplicate a reminder for a date an entity already carries.** A
vehicle's `registration_expiry` and an account's `renewal_date` are
already reported by `list_due_items`. A reminder for the same date is a
second copy of one fact, and the two drift the moment either is
updated. If the user wants a nudge earlier than the date itself, say
that the date is already tracked and ask whether they want the
attribute corrected instead.

Birthdays are the supported exception: saving `date_of_birth` on a person or
pet maintains a source-owned annual reminder automatically. Do not create a
second one manually. Defaults are 09:00 America/New_York and alerts one week,
one day and one hour before. February 29 uses February 28 in non-leap years.
Set `birthday_reminder_enabled=false` on the entity to opt out; changing the
source birthday resets occurrence history, while notification/time overrides
survive unrelated entity edits. Provisional birthdays remain provisional.

**Never draw a conclusion from one page.** See "Every list call is
paginated" below.

## What `list_due_items` returns, and the one rule that is not obvious

One call, three sources, each item tagged:

| `source` | Where the date comes from |
|---|---|
| `attribute` | A forward-dated attribute on an entity: `property.insurance_renewal`, `property.tax_due`, `vehicle.registration_expiry`, `vehicle.warranty_end`, `pet.vaccination_due`, `account.renewal_date` |
| `relationship` | A relationship's `end_date` — a lease ending, a policy lapsing. `related[]` names the other entity. |
| `reminder` | A `reminders` row, with its RRULE expanded into individual occurrences |

Each item has `entity_id`, `entity_type`, `entity_name`, `source`,
`field`, `title`, `due_date`, `days_left` and `related[]`. Reminder items also
carry `reminder_id` and `occurrence_date` (the original occurrence identity).

With no explicit `from`, reminders include unfinished occurrences from the
last seven days, using the snoozed date when applicable. On day eight they
age out of the digest; the stored reminder is not deleted. An explicit date
window is honored exactly for reminders.

**Overdue items come back whether or not `from` covers them.** For the
`attribute` and `relationship` sources there is no lower bound: anything
on or before `to` is returned, with a **negative `days_left`**. This is
deliberate. Nothing moves those dates forward on its own, so a lapsed
registration would otherwise vanish from the report exactly when it
matters most.

So: **do not issue a second call with a past `from` to hunt for overdue
items.** You already have them. Doing it anyway is harmless for
attributes and relationships and actively wrong for reminders, which
*are* bounded by `from` — a past `from` expands every occurrence of
every recurring reminder back to that date and buries the report in
last month's bin days.

## Every list call is paginated

`list_due_items`, `list_documents`, `list_entities`, `list_reminders` —
all default to `limit=50` and return `{items, total, ...}`. One call is
**not** the whole set. Drain it:

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

This is the same helper as `statement-intake`, character for character,
and q-core's `api/tests/test_skill_pagination_parity.py` fails if the two copies
diverge. It is duplicated rather than referenced because a skill has to
be followable without reading another skill; the test is what keeps the
duplication honest.

Always compare against `total`. Raising `limit` is not a fix — it moves
the cliff rather than removing it, and `MAX_LIMIT` is 200.

The empty-page guard is load-bearing, not defensive habit: `total` is
counted in a separate query from the page, so a row deleted between the
two leaves `total` permanently above what any offset can return. Without
the guard the loop advances by zero and spins forever — an unattended
run hanging silently rather than failing.

A truncated list here fails the same way everything else in this skill
fails: a short page looks exactly like a small dataset, and the report
comes out confident and incomplete.

## The procedure

### 1. Fix the window before you query

Take it from the question, not from the default.

| The user asked | `from` / `to` |
|---|---|
| "what's due", "what's coming up", no period | omit both — the API defaults to today .. +30d plus seven overdue days for reminders |
| "this month" | first and last day of the current month |
| "next 90 days" / "this quarter" | today .. today+90 |
| "anything overdue?" | omit both — overdue items are already included; do **not** set a past `from` |

Say the window in the report. A digest without its window is not
checkable.

### 2. Fetch

`fetch_all(list_due_items, **window)`. One call per window, all sources.
Do not filter by `source` to "check each one" — that is three calls
returning the same union, and it invites reporting one of them.

### 3. Group by urgency, not by source

The source is an implementation detail of where the date was stored. The
user cares when.

- **Overdue** — `days_left < 0`. Always first, always called overdue,
  with how long: "registration expired 45 days ago".
- **This week** — `0 <= days_left <= 7`.
- **This month** — `8 <= days_left <= 30`.
- **Later** — beyond that, only if the window asked for it.

Within a group, keep the order `list_due_items` returned: it is sorted
by `due_date` on a decided key, so the first item is the most urgent and
the ordering is stable between calls.

If a group is empty, omit the heading rather than printing "none".
If **every** group is empty, see the second Never above.

### 4. Cross-reference documents

For each item, `fetch_all(list_documents, entity_id=<the item's
entity_id>)` and name any document that plausibly supports it — a
policy, a registration, a warranty. Attach the document's title and id
so the user can ask for it.

Two things to keep straight. A document is **evidence about** an item,
never the source of its date: if a policy PDF says one expiry and the
attribute says another, report the disagreement, do not silently prefer
either. And an item with no document is normal — say nothing rather than
flagging it, or every line grows a warning that means nothing.

Skip this step entirely if the user asked a quick question ("anything
overdue?"). It is a per-entity call and it is only worth it for a real
digest.

### 5. Report

Group headings, one line per item, each line carrying: what, which
thing, when, and how long. For example:

```
Overdue
  Registration expired 45 days ago — Toyota RAV4 (2026-07-12)
  Home policy lapsed 3 days ago — 12 Oak St (2026-09-16)
    doc: "Homeowners policy 2025-26" (d4f2…)

Due this week
  Vaccination due in 4 days — Rex (2026-09-23)
```

End with the window and the total: "12 items due between 2026-09-19 and
2026-10-19." That line is what makes the report checkable, and it is the
one thing that distinguishes "nothing is due" from "I asked the wrong
question".

## Acting on what you find

Only when the user asks. A digest is a read.

- **Done with one occurrence** — `complete_reminder(reminder_id,
  due_date)`, with the `due_date` exactly as reported. It stops
  appearing; the rest of the series is untouched.
- **Not now** — `snooze_reminder(reminder_id, due_date, snoozed_to)`.
  `snoozed_to` must be after `due_date`. The item reappears then.
- **A renewal actually happened** — update the *attribute* on the
  entity with `update_entity`, to the new date the user gives you. Never
  a guessed +1 year, and never without being told the renewal happened.
- **Something new to track** — `create_reminder`. Check the Never list
  first: if the thing is already an entity attribute, it is already
  reported.
- **Edit a series** — `update_reminder` keeps its ID. Schedule edits with
  history require explicit `reset_occurrences=true`; this clears that series’
  completion and snooze history. Edit source-owned birthday dates/names on
  the entity instead.
- **Stop a series** — `delete_reminder` removes the whole thing and
  every record of its occurrences, and cannot be undone. Confirm first,
  and prefer `complete_reminder` if the user meant one occurrence.

## When something looks wrong in the data

`list_due_items` skips rows it cannot read — an unparseable attribute
date, a malformed recurrence rule — and logs each at WARNING rather than
failing, so one bad row cannot take down the digest. The consequence for
you: **an item can be missing without anything in the response saying
so.**

If a user says something should be due and it is not in the report, that
is the first thing to check, not the last. Fetch the entity with
`get_entity` and read the attribute value, or `get_reminder` and read
its `recurrence_rule`. A date the API could not parse is a plain string
in the JSON and obvious once you look. Report it as a data problem with
the exact value, and offer to correct it — do not conclude the item is
not due.
