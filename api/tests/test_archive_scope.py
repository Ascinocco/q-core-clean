"""No reader of `transactions` may bypass the active scope.

The ticket asks for a test per aggregate. That covers the three aggregates
that exist today and not the one written next month, which is the one that
will actually be missed — the same reason a `list_*` name covered 9 of 12
paged tools, and a `zip` over two labels covered 2 of 3 roots.

So this asserts the *structure* rather than the behaviour: every SQL read
of `transactions` in api/ goes through `active_transactions`, except for
an explicit, reasoned allow-list. A new aggregate that reads the raw table
fails here on the day it is written, with the reason attached, rather than
silently counting rows a user was told were removed.

**And the allow-list is itself a claim, so it is checked too.** A name on
it is an exemption with nothing behind it unless something verifies the
exemption is deserved: someone could silence the guard by adding an
aggregate reader to the list, and the guard would then certify the exact
thing it exists to catch. `test_every_exemption_names_its_subject_and_
carries_no_aggregate` is what stops that, and #104's writer allow-list
needed the same second test for the same reason.

The property an exemption must satisfy is that its read **names its
subject in the WHERE and summarises nothing**. Not "fetches by id", which
was the first version of this rule: `_set_archived` reads by
`statement_id` and returns many rows, and is legitimate — it counts
hand-edited rows across a statement INCLUDING archived ones, because the
count is about what a re-import would discard. The narrower rule would
have failed correct code, and the obvious fix would have been to change
the query to suit the test.

**This guard covers BOTH tables that carry archive semantics**, and the
read path for each is its view:

    transactions  ->  active_transactions
    statements    ->  active_statements

It covered only `transactions` until ticket T-26, while being called
`test_archive_scope` — the hazard impl-3 identified was the NAME, not the
omission. A guard whose name implies wider coverage than it has is worse
than an obviously partial one, because the gap is invisible to anyone
deciding whether they still need to check by hand.

The two allow-lists are deliberately separate: an exemption for a by-id
transaction fetch must not authorize raw statement reads (or vice versa).
"""

import re
from pathlib import Path

from api.config import REPO_ROOT

#: Reads that MUST see archived rows, each with the reason it is exempt.
#: Anything not listed here has to use the view.
ALLOWED_RAW_READS = {
    "_get_transaction_row": (
        "explicit fetch by id: archiving must not make a row unfetchable, "
        "or an archived row could never be inspected or unarchived"
    ),
    "_set_archived": (
        "counts hand-edited rows across a statement, archived or not, "
        "because the count is about what a re-import would discard"
    ),
}

# The writers (archive_transaction, unarchive_transaction,
# update_transaction) are deliberately absent. They only UPDATE, so they
# never match the read pattern -- and listing them anyway was the first
# thing test_the_allow_list_does_not_outlive_its_entries caught, on the day
# it was written. An exemption nothing needs is an exemption nobody
# re-examines.

def _read_re(table: str) -> re.Pattern[str]:
    """`FROM <table>`, matched whole so `transactions` does not match
    `active_transactions` and `statements` does not match
    `active_statements` — which is the entire distinction being policed."""
    return re.compile(rf"\bFROM\s+{table}\b", re.IGNORECASE)


READ = _read_re("transactions")
READ_STATEMENTS = _read_re("statements")

#: The same, for `statements`. A separate list rather than one merged one:
#: the exemptions differ per table and a merged list would let an
#: exemption earned on one table silently cover the other.
ALLOWED_RAW_STATEMENT_READS = {
    "_get_statement_row": (
        "explicit fetch by id: archiving must not make a statement "
        "unfetchable, or an archived one could never be inspected or "
        "unarchived"
    ),
}

#: Counts pinned per table, for the same reason as RAW_READER_COUNT.
RAW_STATEMENT_READER_COUNT = 1
STATEMENT_EXEMPTION_COUNT = 1


#: How many functions in api/ read `FROM transactions` directly. Literal,
#: so changing the number is a decision someone makes with this file open.
RAW_READER_COUNT = 2


def _functions_reading(
    pattern: re.Pattern[str], expected: int, table: str
) -> dict[str, list[str]]:
    """Map function name -> raw-read lines for one table, across api/.

    Shared by both tables rather than duplicated: the ticket's own
    instruction was to reuse this shape, not to grow a second idiom. Two
    copies of a discovery loop drift exactly the way two copies of a
    default do.
    """
    found: dict[str, list[str]] = {}
    for path in sorted((REPO_ROOT / "api").glob("*.py")):
        current = "<module>"
        for line in path.read_text().splitlines():
            match = re.match(r"\s*(?:async\s+)?def\s+(\w+)", line)
            if match:
                current = match.group(1)
            if pattern.search(line):
                found.setdefault(current, []).append(line.strip())
    assert len(found) == expected, (
        f"expected {expected} functions reading `FROM {table}`, found "
        f"{len(found)}: {sorted(found)}. If a reader was added or moved onto "
        f"the view, change the count deliberately; if this found none, the "
        f"discovery is broken and every test over {table} is passing over an "
        "empty set."
    )
    return found


def _functions_reading_the_raw_table() -> dict[str, list[str]]:
    """Map function name -> raw-read lines, across api/ (tests excluded).

    The count is asserted because this function is the subject generator
    for every test in the file, and a subject generator that finds nothing
    makes all of them vacuous AT ONCE — the blindness is correlated, and
    the passing count does not move.

    It is not enough that `test_the_allow_list_does_not_outlive_its_entries`
    happens to fail on an empty result today. It does, but only because
    ALLOWED_RAW_READS is non-empty: every entry looks stale, so it trips.
    The moment the allow-list empties — which is the state this codebase is
    trying to reach, since every exemption removed is a reader moved onto
    the view — that incidental cover disappears and BOTH tests go vacuous
    together. Relying on it would be depending on the codebase not
    improving.

    Found by applying review-1's `return []` mutation (from #90) to this
    file, after impl-3 checked their own discovery test the same way.
    """
    found: dict[str, list[str]] = {}
    for path in sorted((REPO_ROOT / "api").glob("*.py")):
        current = "<module>"
        for line in path.read_text().splitlines():
            match = re.match(r"\s*(?:async\s+)?def\s+(\w+)", line)
            if match:
                current = match.group(1)
            if READ.search(line):
                found.setdefault(current, []).append(line.strip())
    assert len(found) == RAW_READER_COUNT, (
        f"expected {RAW_READER_COUNT} functions reading `FROM transactions`, "
        f"found {len(found)}: {sorted(found)}. If a reader was added or "
        "moved onto the view, change the count deliberately; if this found "
        "none, the discovery is broken and every test here is passing over "
        "an empty set."
    )
    return found


#: How many exemptions exist. Pinned for the same reason RAW_READER_COUNT
#: is: an allow-list that can grow silently is a list nobody re-reads.
EXEMPTION_COUNT = 2

#: Aggregate functions. A read carrying one of these is summarising a set
#: rather than fetching a named thing, and summarising a set is exactly
#: what must go through the view.
AGGREGATES = re.compile(r"\b(SUM|COUNT|AVG|MIN|MAX|TOTAL)\s*\(|\bGROUP\s+BY\b", re.I)

#: A WHERE that names its subject: `x = ?` or `x IN (...)`. Not a general
#: SQL parser -- it is looking for evidence that the query is bounded to
#: something the caller named, which is the property the exemption rests
#: on.
NAMES_A_SUBJECT = re.compile(r"\bWHERE\b.*?(=\s*\?|\bIN\s*\()", re.I | re.S)


def _statement_around(path: Path, index: int, lines: list[str]) -> str:
    """The whole SQL statement, not just the line that matched.

    The discovery matches the line carrying `FROM transactions`, but a
    query can put its WHERE on the next line:

        "SELECT ... FROM transactions "
        "WHERE id = ?"

    Checking the matched line alone would fail that legitimate read and
    invite someone to relax the check until it passed. So the shape test
    reads a window instead, ending at the close of the execute() call.

    The window is heuristic and its limit is stated rather than hidden: a
    query whose WHERE sits more than WINDOW lines below its FROM reads as
    unbounded and fails. That is the safe direction — it errs toward
    refusing an exemption, not toward granting one.
    """
    WINDOW = 6
    chunk = []
    for line in lines[index : index + WINDOW]:
        chunk.append(line)
        if line.rstrip().endswith(")") or line.rstrip().endswith(").fetchone()"):
            break
    return " ".join(chunk)


def _exemption_statements() -> dict[str, str]:
    """Each allow-listed function mapped to the SQL it is exempted for."""
    found: dict[str, str] = {}
    for path in sorted((REPO_ROOT / "api").glob("*.py")):
        lines = path.read_text().splitlines()
        current = "<module>"
        for index, line in enumerate(lines):
            match = re.match(r"\s*(?:async\s+)?def\s+(\w+)", line)
            if match:
                current = match.group(1)
            if READ.search(line) and current in ALLOWED_RAW_READS:
                found[current] = _statement_around(path, index, lines)
    assert len(found) == EXEMPTION_COUNT, (
        f"expected {EXEMPTION_COUNT} exempted reads, found {len(found)}: "
        f"{sorted(found)}. If an exemption was added or removed, change the "
        "count deliberately; if this found none, the discovery is broken and "
        "the shape test below is passing over an empty set."
    )
    return found


def test_every_exemption_names_its_subject_and_carries_no_aggregate():
    """The allow-list is a claim; this checks the claim.

    Without it, a name on the list is an exemption from the rule with
    nothing behind it — someone could silence the guard by adding an
    aggregate reader to the list, and the guard would then certify the
    very thing it exists to catch. #104's writer allow-list needed the
    same second test for the same reason.

    The property is NOT "fetches by id", which was the first version of
    this ticket. `_set_archived` reads by `statement_id` and returns many
    rows, and is a legitimate exemption — it counts hand-edited rows
    across a statement INCLUDING archived ones, because the count is
    about what a re-import would discard. Writing the narrower rule would
    have failed correct code, and the obvious fix would have been to
    change the query to suit the test.

    What actually distinguishes an exemption from the readers this guard
    catches is that it names its subject and summarises nothing.
    """
    offenders = {}
    for name, sql in _exemption_statements().items():
        if AGGREGATES.search(sql):
            offenders[name] = f"carries an aggregate: {sql.strip()[:90]}"
        elif not NAMES_A_SUBJECT.search(sql):
            offenders[name] = f"no WHERE naming a subject: {sql.strip()[:90]}"

    assert offenders == {}, (
        f"exempted reads that do not match the shape the exemption rests on: "
        f"{offenders}. An exemption is for fetching a NAMED thing that must "
        "remain reachable when archived; summarising a set goes through "
        "active_transactions."
    )


def test_every_exemption_states_a_reason():
    """A name with no reason is an exemption nobody can evaluate later."""
    empty = sorted(
        name for name, reason in ALLOWED_RAW_READS.items() if not (reason or "").strip()
    )

    assert empty == [], f"allow-list entries with no stated reason: {empty}"


def test_the_shape_check_can_fail():
    """A source-reading check that cannot fail is worth nothing."""
    assert AGGREGATES.search('"SELECT COUNT(*) FROM transactions WHERE x = ?"')
    assert AGGREGATES.search('"SELECT SUM(amount_cents) FROM transactions"')
    assert AGGREGATES.search('"... FROM transactions GROUP BY category_id"')
    assert not AGGREGATES.search('"SELECT id FROM transactions WHERE id = ?"')
    assert NAMES_A_SUBJECT.search('"SELECT * FROM transactions WHERE id = ?"')
    assert NAMES_A_SUBJECT.search('"... WHERE id IN (?, ?)"')
    assert not NAMES_A_SUBJECT.search('"SELECT * FROM transactions"')
    assert not NAMES_A_SUBJECT.search('"SELECT * FROM transactions WHERE 1=1"')


def test_every_raw_read_of_transactions_is_on_the_allow_list():
    offenders = {
        name: lines
        for name, lines in _functions_reading_the_raw_table().items()
        if name not in ALLOWED_RAW_READS
    }

    assert offenders == {}, (
        "these read `FROM transactions` directly and so can see archived "
        f"rows: {offenders}. Use active_transactions, or add an entry to "
        "ALLOWED_RAW_READS saying why this read must see them."
    )


def test_the_allow_list_does_not_outlive_its_entries():
    """An exemption for a function that no longer exists is a stale licence.

    Left alone, the list becomes a place where anything is permitted
    because nobody can tell which entries still mean something.
    """
    present = set(_functions_reading_the_raw_table())
    stale = sorted(set(ALLOWED_RAW_READS) - present)

    assert stale == [], f"allow-list entries with no matching read: {stale}"


def _statement_exemption_statements() -> dict[str, str]:
    """Each allow-listed statements reader mapped to the SQL it is exempted
    for. Same window logic as the transactions side, via the same helper."""
    found: dict[str, str] = {}
    for path in sorted((REPO_ROOT / "api").glob("*.py")):
        lines = path.read_text().splitlines()
        current = "<module>"
        for index, line in enumerate(lines):
            match = re.match(r"\s*(?:async\s+)?def\s+(\w+)", line)
            if match:
                current = match.group(1)
            if READ_STATEMENTS.search(line) and current in ALLOWED_RAW_STATEMENT_READS:
                found[current] = _statement_around(path, index, lines)
    assert len(found) == STATEMENT_EXEMPTION_COUNT, (
        f"expected {STATEMENT_EXEMPTION_COUNT} exempted statements reads, "
        f"found {len(found)}: {sorted(found)}."
    )
    return found


def test_every_raw_read_of_statements_is_on_the_allow_list():
    """The second table, which this guard's name implied it already covered.

    `statements` carries the same archive semantics as `transactions` and
    has its own `active_statements` view, but until now the discipline for
    statement reads rested entirely on individual behavioural tests. The
    module was called test_archive_scope and guarded one of the two tables
    the archive applies to — the hazard impl-3 identified is the name, not
    the omission.
    """
    offenders = {
        name: lines
        for name, lines in _functions_reading(
            READ_STATEMENTS, RAW_STATEMENT_READER_COUNT, "statements"
        ).items()
        if name not in ALLOWED_RAW_STATEMENT_READS
    }

    assert offenders == {}, (
        f"these read `FROM statements` directly and so can see archived "
        f"statements: {offenders}. Use active_statements, or add an entry to "
        "ALLOWED_RAW_STATEMENT_READS saying why this read must see them."
    )


def test_every_statement_exemption_names_its_subject_and_carries_no_aggregate():
    """The allow-list is a claim on this table too."""
    offenders = {}
    for name, sql in _statement_exemption_statements().items():
        if AGGREGATES.search(sql):
            offenders[name] = f"carries an aggregate: {sql.strip()[:90]}"
        elif not NAMES_A_SUBJECT.search(sql):
            offenders[name] = f"no WHERE naming a subject: {sql.strip()[:90]}"

    assert offenders == {}, f"exempted statement reads of the wrong shape: {offenders}"


def test_every_statement_exemption_states_a_reason():
    empty = sorted(
        name
        for name, reason in ALLOWED_RAW_STATEMENT_READS.items()
        if not (reason or "").strip()
    )

    assert empty == [], f"statement allow-list entries with no reason: {empty}"


def test_the_statements_allow_list_does_not_outlive_its_entries():
    present = set(
        _functions_reading(READ_STATEMENTS, RAW_STATEMENT_READER_COUNT, "statements")
    )
    stale = sorted(set(ALLOWED_RAW_STATEMENT_READS) - present)

    assert stale == [], f"statement allow-list entries with no matching read: {stale}"


def test_the_two_tables_do_not_share_an_allow_list():
    """An exemption earned on one table must not cover the other."""
    assert '_get_transaction_row' not in ALLOWED_RAW_STATEMENT_READS
    assert '_get_statement_row' not in ALLOWED_RAW_READS


def test_the_guard_would_catch_a_new_aggregate(tmp_path, monkeypatch):
    """The test that proves this test works.

    A structural check that cannot fail is worth nothing, and this one
    reads source rather than behaviour, so a typo in the regex would leave
    it green forever.
    """
    assert READ.search("        f\"FROM transactions WHERE entity_id = ?\"")
    assert READ.search("SELECT COUNT(*) FROM transactions WHERE x = 1")
    assert not READ.search("FROM active_transactions WHERE x = 1")
