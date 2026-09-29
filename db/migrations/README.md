# db/migrations/

Numbered plain-SQL upgrades for databases that predate a schema change.
The convention `runbooks/decisions-log.md` settled on before Phase 2
("Migration tooling: deferred, with a convention in mind"), built when
the first real migration was needed.

## How the two files relate

- **`db/schema.sql` is the full current schema.** A brand-new database is
  built from it directly and never runs a migration — the bootstrap
  stamps every known version as applied, because schema.sql already
  describes the state those migrations build toward.
- **A migration is only ever applied to an existing database** that
  predates it.

Both paths must converge on the same schema. A change added to one and
not the other is the mistake this directory makes possible:
`test_repo_migrations_directory_is_consistent_with_schema_sql` guards it.

## Adding one

1. Add the change to `db/schema.sql` (fresh installs).
2. Add `NNNN_short_description.sql` here with the same change written as a
   delta (existing installs). Next unused number, zero-padded to four.
3. Add any new table to `EXPECTED_TABLES` in `api/db.py` —
   `test_expected_tables_matches_schema_sql` asserts it matches schema.sql
   exactly.
4. Run `pytest api/tests/test_migrations.py`.

## What a migration may change

Anything: a new table, a new column, a new index. Migrations are applied
once at startup by `apply_startup_migrations`, which does not care what a
file creates.

This used to be narrower, and the history is worth knowing because the
old rule looked like a convention and was really a limitation. Migrations
were reachable only through `init_db`'s "incomplete" branch, which
triggers on a missing *table*, so a column- or index-only file was skipped
silently — it existed, `pending_migrations` listed it, and nothing ran or
said so. `test_repo_migrations_are_table_additive` enforced "every
migration creates a table" to stop anyone tripping over that. It is gone,
replaced by `test_every_repo_migration_is_reachable_by_the_runner`, which
asserts the property that rule was protecting: no file here is
unreachable.

Use `IF NOT EXISTS` when a database may already have the object by some
other route — a hand-applied index, for instance. The migration then
records that database as up to date without touching it.

## Constraints

- **No semicolons inside string literals or trigger bodies.** The runner
  splits on `;` rather than using `executescript`, which would issue its
  own COMMIT and break the transaction that makes a failed migration leave
  nothing behind. Semicolons in `--` comments are fine: comments are
  stripped before the split. They were not always — a semicolon in this
  directory's own first migration header is how that was found.
  **This is the usual reason the no-transaction-control rule below
  fires.** A trigger body is `BEGIN ... END`, so splitting it leaves a
  bare `END` that trips that guard, and the failure names transaction
  control even though you wrote none. If you are staring at that error
  and did not write `BEGIN`/`COMMIT` yourself, the semicolons in your
  trigger body are what to look at.
- **No `BEGIN`, `COMMIT`, `END`, `ROLLBACK` or `SAVEPOINT`.** Write only
  the DDL. The runner already wraps the whole file in one `BEGIN
  IMMEDIATE ... COMMIT`, so a file that manages its own transaction
  fights that wrapper: a bare `COMMIT;` half way down ends the span
  early, committing the statements above it on their own and leaving the
  `schema_migrations` row in a different transaction. `init_db`'s
  stale-state re-read (see the decisions-log) is only correct because
  that half-applied state cannot happen. This is easy to get wrong
  precisely because explicit transaction control is good practice in
  ordinary SQL, and nothing in the file you are writing says your
  statements are already inside someone else's transaction.
  `test_repo_migrations_contain_no_transaction_control` enforces it.
  **If that test fires on a file you believe has no transaction control,
  read the semicolon bullet above first** — a trigger body's internal
  semicolons split the file and leave its closing `END` standing alone,
  which looks identical to authored transaction control from here. The
  guard names which of the two it thinks it found.
- **Never edit a migration that has shipped.** Databases that already
  applied it will not re-run it. Add a new one.
- Each migration runs in its own transaction and is recorded only if it
  fully succeeds.

## Two rules the guards enforce

### No `CREATE TABLE IF NOT EXISTS`

A versioned migration runs **exactly once**, guarded by
`schema_migrations`. It does not need conditional DDL, and conditional
DDL is actively harmful here: `CREATE TABLE IF NOT EXISTS x` is a no-op
where `x` already exists, so a column declared inside it **never arrives
on that database** — while the column-baseline guard reads the statement
and counts it as supplied. Green, wrong, and nothing to say so.

Two files are exempt, frozen in `CONDITIONAL_CREATE_EXEMPTIONS`. `0001`
is structurally required — the runner creates `schema_migrations` before
applying anything, so an unconditional CREATE there would always fail.
`0006` is historical and stays exempt because it has already shipped,
and editing a shipped migration is worse than exempting it.

`test_no_migration_uses_a_conditional_create_table` enforces this.

### Rebuild a table by renaming the OLD one aside

When a column's type or name changes, SQLite needs a table rebuild.
Write it this way:

```sql
ALTER TABLE thing RENAME TO thing_pre_change;
CREATE TABLE thing (
    ...the new shape...
);
INSERT INTO thing (...) SELECT ... FROM thing_pre_change;
DROP TABLE thing_pre_change;
```

**Not** the other order — creating `thing_new`, copying into it, then
`ALTER TABLE thing_new RENAME TO thing`. Both are correct SQL and both
work. The difference is that the first ends with a `CREATE TABLE thing`
carrying the table's real name, which the column-baseline guard reads
directly; in the second the columns only ever appear under the interim
name, so the guard cannot attribute them and **goes red**.

That failure is safe rather than silent, and the fix is to use the order
above rather than to exempt the migration. Measured both ways:
`test_a_rebuild_that_renames_the_old_table_aside_is_readable` and
`test_a_rebuild_that_renames_into_place_is_not_attributed` pin each.

## What this deliberately does not do

No downgrades, no branching, no checksums. Single-user, single-machine,
forward-only — `runbooks/decisions-log.md` rejected Alembic and friends
because they are built around an ORM this API deliberately doesn't have.
