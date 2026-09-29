"""No writer of `merchant_rules` may skip the audit trail.

A test per writer covers the four that exist today and not the fifth,
written next month by someone who reads the three endpoints the ticket
named and misses `apply_as_rule` — which is not hypothetical: that fourth
writer is a second creation path, invisible to anyone enumerating
`/merchant_rules` routes, and it was found by counting rather than by
reading.

So this asserts the structure: every mutating statement against
`merchant_rules` in api/ lives in a function on a literal allow-list of
recording writers. A new writer fails here on the day it is written, with
the reason attached, rather than the day someone asks who changed a rule
and the trail has a hole in it.
"""

import re

from api.config import REPO_ROOT

#: Functions permitted to mutate `merchant_rules`. Every one of them calls
#: `_record_rule_change` before its commit. Adding a name here is a claim
#: that the new writer records too, and is meant to be hard to make idly.
RECORDING_WRITERS = {
    "create_merchant_rule": "POST — writes a creation row",
    "update_merchant_rule": "PATCH — writes one row per changed field",
    "delete_merchant_rule": "DELETE — writes a terminal row that outlives the rule",
    "apply_transaction_as_rule": (
        "the second creation path, from a transaction; the writer an "
        "endpoint-by-endpoint reading misses"
    ),
}

#: How many functions mutate the table. Literal, so a fifth writer makes
#: someone open this file rather than quietly joining the set.
WRITER_COUNT = 4

MUTATES = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+merchant_rules\b", re.IGNORECASE
)


def _functions_mutating_rules() -> dict[str, list[str]]:
    """Map function name -> mutating lines, across api/ (tests excluded).

    The count is asserted here because this function is the subject
    generator for every test in the file. A subject generator that finds
    nothing makes all of them vacuous at once — the blindness is
    correlated and the passing count does not move, which is exactly how
    it survives review.
    """
    found: dict[str, list[str]] = {}
    for path in sorted((REPO_ROOT / "api").glob("*.py")):
        current = "<module>"
        for line in path.read_text().splitlines():
            match = re.match(r"\s*(?:async\s+)?def\s+(\w+)", line)
            if match:
                current = match.group(1)
            if MUTATES.search(line):
                found.setdefault(current, []).append(line.strip())
    assert len(found) == WRITER_COUNT, (
        f"expected {WRITER_COUNT} functions mutating merchant_rules, found "
        f"{len(found)}: {sorted(found)}. If a writer was added, make it record "
        "and list it; if this found none, the discovery is broken and every "
        "test here is passing over an empty set."
    )
    return found


def test_every_writer_of_merchant_rules_records_the_change():
    offenders = {
        name: lines
        for name, lines in _functions_mutating_rules().items()
        if name not in RECORDING_WRITERS
    }

    assert offenders == {}, (
        f"these mutate merchant_rules without being known to record it: "
        f"{offenders}. Call _record_rule_change before the commit, then add "
        "the function to RECORDING_WRITERS with what it records."
    )


def test_every_listed_writer_actually_calls_the_recorder():
    """The allow-list is a claim; this checks the claim.

    Without it the list degrades into a list of names someone typed. A
    writer could be added here to silence the test above while recording
    nothing at all — which would be worse than no guard, because the
    guard would then assert the property it had been used to bypass.
    """
    source = (REPO_ROOT / "api" / "financial.py").read_text()
    bodies = re.split(r"\ndef ", source)
    seen = {}
    for body in bodies:
        name = body.split("(", 1)[0].strip()
        if name in RECORDING_WRITERS:
            seen[name] = "_record_rule_change(" in body

    missing = sorted(n for n in RECORDING_WRITERS if not seen.get(n))
    assert missing == [], f"listed as recording but never calls the recorder: {missing}"


def test_the_allow_list_does_not_outlive_its_entries():
    present = set(_functions_mutating_rules())
    stale = sorted(set(RECORDING_WRITERS) - present)

    assert stale == [], f"allow-list entries with no matching writer: {stale}"


def test_the_guard_would_catch_a_new_writer():
    """A source-reading check that cannot fail is worth nothing, and a typo
    in the regex would leave this one green for ever."""
    assert MUTATES.search('        "INSERT INTO merchant_rules (id, pattern) "')
    assert MUTATES.search('connection.execute("DELETE FROM merchant_rules WHERE id = ?")')
    assert MUTATES.search('"UPDATE merchant_rules SET pattern = ?"')
    assert not MUTATES.search('"SELECT * FROM merchant_rules ORDER BY id"')
    assert not MUTATES.search('"INSERT INTO merchant_rule_changes (id, rule_id) "')
