"""Merchant-rule matching must be token-bounded, not bare substring.

Found by review-1 auditing the seeded rules: `ESSO` matched "ZESSOR
ACADEMY" and `SHELL` matched "ZUMSHELLA SALON", so a tutoring charge was
categorised as auto fuel. Longest-pattern-wins cannot help — that rule only
resolves collisions between a short and a long form of the *same* merchant,
not a pattern buried inside an unrelated word.

Why it had to land before any import rather than after: the
category is written onto the transaction row at import time. A later fix
does not retroactively re-categorise anything, so every wrong category
survives the fix that prevents new ones.

These call `_match_merchant_rule` directly rather than driving
source preview/commit for each case. The unit is the matcher, and one
statement import per case would be forty round trips to assert a predicate.
The end-to-end path is covered by the existing import tests.
"""

import sqlite3

import pytest

from api.financial import _match_merchant_rule


class _Rules:
    """An in-memory merchant_rules table, so these tests need no API.

    A wrapper rather than attributes hung off the connection:
    sqlite3.Connection does not accept attribute assignment.
    """

    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            "CREATE TABLE merchant_rules (id TEXT PRIMARY KEY, pattern TEXT, "
            "category_id TEXT, entity_id TEXT)"
        )

    def add(self, *patterns: str) -> None:
        self.connection.executemany(
            "INSERT INTO merchant_rules (id, pattern, category_id, entity_id) "
            "VALUES (?, ?, ?, NULL)",
            [(pattern, pattern, "auto_fuel") for pattern in patterns],
        )

    def add_with_ids(self, *rules: tuple[str, str]) -> None:
        """Insert (id, pattern) pairs in the order given.

        `add` uses the pattern as the id, which makes id order and
        pattern order the same thing — fine for the boundary tests, and
        useless for a tie-break test, where the whole question is what
        happens when insertion order and id order disagree.
        """
        for rule_id, pattern in rules:
            self.connection.execute(
                "INSERT INTO merchant_rules (id, pattern, category_id, "
                "entity_id) VALUES (?, ?, ?, NULL)",
                (rule_id, pattern, pattern),
            )

    def close(self) -> None:
        self.connection.close()


@pytest.fixture()
def rules():
    store = _Rules()
    yield store
    store.close()


def _matched(rules: _Rules, description: str) -> str | None:
    rule = _match_merchant_rule(rules.connection, description)
    return rule["pattern"] if rule is not None else None


# --- the collisions that prompted this -------------------------------------


@pytest.mark.parametrize(
    "description",
    [
        "ZESSOR ACADEMY",
        "ESPRESSOVILLE CAFE",
        "QUINTESSO STUDIO",
        "BLOFESSORS PUB",
        "LESSONS ETC",
    ],
)
def test_esso_does_not_match_inside_a_longer_word(rules, description):
    rules.add("ESSO")

    assert _matched(rules, description) is None


@pytest.mark.parametrize(
    "description",
    ["ZUMSHELLA SALON", "QUIXSHELL SEAFOOD", "SHELLWORTZ MARKET"],
)
def test_shell_does_not_match_inside_a_longer_word(rules, description):
    rules.add("SHELL")

    assert _matched(rules, description) is None


# --- and the matches that must keep working --------------------------------


@pytest.mark.parametrize(
    "description",
    [
        "ESSO 1234 ANYTOWN",          # separated by spaces
        "FUELCO/ESSO",          # preceded by a slash, at end of string
        "ESSO",                       # whole description
        "ESSO*ANYTOWN XY",            # followed by a star
        "ESSO#4821",                  # followed by a hash
        "ESSO-ANYTOWN",               # followed by a hyphen
        "PAYMENT ESSO ",              # trailing space
    ],
)
def test_esso_still_matches_at_a_token_boundary(rules, description):
    rules.add("ESSO")

    assert _matched(rules, description) == "ESSO"


def test_a_pattern_containing_punctuation_still_matches(rules):
    """`Amazon.ca*482KX7RT2` is the real shape. The dot is part of the
    pattern and must be matched literally, not as a regex wildcard."""
    rules.add("Amazon.ca")

    assert _matched(rules, "Amazon.ca*482KX7RT2") == "Amazon.ca"


def test_a_dot_in_a_pattern_is_literal_not_a_wildcard(rules):
    """The flip side: if the pattern were compiled unescaped, `Amazon.ca`
    would also match `AmazonXca`. Pins that it does not."""
    rules.add("Amazon.ca")

    assert _matched(rules, "AmazonXca*482KX7RT2") is None


def test_a_multi_word_pattern_still_matches(rules):
    rules.add("AMZN Mktp CA")

    assert _matched(rules, "AMZN Mktp CA*1A2B3C") == "AMZN Mktp CA"


def test_matching_is_still_case_insensitive(rules):
    rules.add("Amazon")

    assert _matched(rules, "AMAZON MKTPLACE PMTS") == "Amazon"


# --- the digit-adjacency case, decided deliberately ------------------------


def test_a_pattern_running_straight_into_digits_does_not_match(rules):
    """`ESSO1234` does NOT match, and this is the deliberate reading.

    The rule is "preceded and followed by a non-alphanumeric character or a
    string boundary", and a digit is alphanumeric. The ticket lists
    "adjacent to digits" among the positive cases, which is satisfied by
    `Amazon.ca*482KX7RT2` — digits after a separator — rather than by a
    pattern running directly into a digit with nothing between.

    Treating digits as boundaries would reopen the bug from the other side:
    `SHELL` would match `SHELL5` but also, more to the point, any pattern
    ending where a word continues in digits is exactly the ambiguity that
    made bare substring unsafe. The failure mode here is a transaction
    landing in `unmatched` for a human to confirm, which is the safe
    direction — an unmatched row asks a question, a miscategorised row
    answers one wrongly and never asks again.
    """
    rules.add("ESSO")

    assert _matched(rules, "ESSO1234 ANYTOWN") is None


# --- longest-wins and the tie-break are unchanged --------------------------


def test_longest_pattern_still_wins_among_bounded_matches(rules):
    rules.add("AMAZON", "AMAZON WEB SERVICES")

    assert (
        _matched(rules, "AMAZON WEB SERVICES AWS.AMAZON.COM") == "AMAZON WEB SERVICES"
    )


def test_a_longer_pattern_that_is_not_token_bounded_does_not_win(rules):
    """Longest-wins operates only over patterns that actually matched. A
    longer pattern failing the boundary test must not beat a shorter one
    that passed — otherwise boundaries would be decorative."""
    rules.add("PUB", "BLOFESSORS PU")

    assert _matched(rules, "BLOFESSORS PUB") == "PUB"


def test_no_rules_means_no_match(rules):
    assert _matched(rules, "ANYTHING AT ALL") is None


# --- pattern shapes a boundary rule must keep matching -----------------------
#
# Invented patterns with the punctuation shapes bank descriptions use. These
# are the positives a boundary rule could plausibly break, which makes them
# the ones worth pinning: a boundary fix that silently drops a match trades
# a loud bug for a quiet one.


@pytest.mark.parametrize(
    "pattern,description",
    [
        # Internal hyphen in the PATTERN — the escape has to survive it, and
        # the boundary check looks outside the pattern, not inside it.
        ("MEGA-MART", "MEGA-MART #4321 ANYTOWN XY"),
        # Followed by a dot, then more alphanumerics.
        ("AMAZON", "AMAZON.CA*AB12"),
        # Dot inside the pattern, star immediately after.
        ("Amazon.ca", "Amazon.ca*7Q5ZD41K8"),
        # Spaces inside the pattern.
        ("AMZN Mktp CA", "AMZN Mktp CA*482KX7RT2"),
        # Hyphen immediately BEFORE the pattern, no space.
        ("Falafels", "TST-Falafels - 4321 Ma"),
    ],
)
def test_live_rule_shapes_still_match(rules, pattern, description):
    rules.add(pattern)

    assert _matched(rules, description) == pattern


def test_uber_matches_the_leading_token_but_not_inside_ubereats(rules):
    """One description containing both the good and the bad occurrence.

    "UBER CANADA/UBEREATS" holds `UBER` twice: once as a whole token at the
    start, once buried inside UBEREATS. The rule must find the first and
    not be fooled into counting the second — and because a rule either
    matches or doesn't, the observable result is simply that it matches.
    The case earns its place by proving the boundary check doesn't reject
    the whole description just because one occurrence fails.
    """
    rules.add("UBER")

    assert _matched(rules, "UBER CANADA/UBEREATS") == "UBER"


def test_uber_does_not_match_a_description_with_only_the_buried_occurrence(rules):
    """The other half, which the combined description cannot show: with no
    standalone `UBER`, there is nothing to match."""
    rules.add("UBER")

    assert _matched(rules, "UBEREATS ANYTOWN") is None


# --- equal-length ties: lowest id wins (D15) -------------------------------
#
# The matcher used to read an unordered `SELECT *` and rank with `max()`,
# which returns the FIRST maximal element — so two equal-length patterns
# matching one description were resolved by SQLite's scan order, which no
# statement guarantees, while the intake skill's preview resolved them by
# id. Preview and import could disagree on exactly the shape the preview
# exists to catch. Now: `ORDER BY length(pattern) DESC, id ASC`.
#
# Lowest id is arbitrary with respect to age — ids are uuid4 and the table
# has no created_at — but it is deterministic, stable across re-imports and
# identical to what the preview computes. Those are the properties wanted.

# Chosen so insertion order and id order disagree: whichever is inserted
# first here carries the HIGHER id. Building these through POST
# /merchant_rules instead would depend on which uuid4 the two requests
# happened to draw, and the test would pass or fail at random.
HIGH_ID = "ffffffff-0000-4000-8000-000000000001"
LOW_ID = "00000000-0000-4000-8000-000000000002"


def test_an_equal_length_tie_is_broken_by_lowest_id(rules):
    # ALPHA inserted first, so it has the lower rowid and the higher id.
    # Scan order says ALPHA; id order says BRAVO.
    rules.add_with_ids((HIGH_ID, "ALPHA"), (LOW_ID, "BRAVO"))

    assert _matched(rules, "ALPHA BRAVO PLAZA") == "BRAVO"


def test_the_tie_break_does_not_depend_on_insertion_order(rules):
    """The same two rules inserted the other way round give the same
    answer. This is the property the ORDER BY buys and the one scan order
    cannot have: re-importing a description after the rule set was
    rebuilt — restored from a backup, reseeded — must not silently
    recategorise it."""
    rules.add_with_ids((LOW_ID, "BRAVO"), (HIGH_ID, "ALPHA"))

    assert _matched(rules, "ALPHA BRAVO PLAZA") == "BRAVO"


def test_length_still_outranks_id(rules):
    """id is the tie-break, not the sort. A lower id must not beat a
    longer pattern — that would invert the precedence rule the tie-break
    is subordinate to."""
    rules.add_with_ids((LOW_ID, "ALPHA"), (HIGH_ID, "ALPHA BRAVO"))

    assert _matched(rules, "ALPHA BRAVO PLAZA") == "ALPHA BRAVO"


def test_a_tie_among_three_equal_length_patterns_takes_the_lowest(rules):
    rules.add_with_ids(
        ("ffffffff-0000-4000-8000-00000000000a", "ALPHA"),
        ("00000000-0000-4000-8000-00000000000b", "BRAVO"),
        ("11111111-0000-4000-8000-00000000000c", "DELTA"),
    )

    assert _matched(rules, "ALPHA BRAVO DELTA PLAZA") == "BRAVO"


def test_the_tie_break_runs_after_the_boundary_test(rules):
    """A lower-id equal-length pattern that fails the boundary test must
    not win. Ordering decides rank among survivors; it does not decide
    membership."""
    # BRAVO has the lower id but appears only inside "BRAVOS".
    rules.add_with_ids((HIGH_ID, "ALPHA"), (LOW_ID, "BRAVO"))

    assert _matched(rules, "ALPHA BRAVOS PLAZA") == "ALPHA"


def test_the_matcher_orders_before_it_ranks():
    """Pins the mechanism, not just today's answers.

    `max()` over an unordered SELECT gives the right answer on every
    non-tie case, so the behavioural tests can only reach it through a
    constructed tie. If the ORDER BY is dropped the tie-break silently
    reverts to scan order — a defect with no failing test unless
    something checks the shape.
    """
    import inspect

    import api.financial

    source = inspect.getsource(api.financial._match_merchant_rule)
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )

    assert "ORDER BY length(pattern) DESC, id ASC" in code
    assert "max(" not in code, (
        "max() returns the first maximal element, so it reintroduces the "
        "scan-order dependency the ORDER BY removes"
    )


# --- the preview and the import must agree ---------------------------------


def _preview_winner(rule_dicts: list[dict], description: str) -> dict | None:
    """The intake skill's local matcher, mirrored.

    A mirror on purpose rather than a call into `_match_merchant_rule`: a
    differential test that runs one implementation twice proves nothing.
    This is the code the skill's fence carries, and if the two drift,
    these tests stop describing the skill.
    """
    import re

    def matches(pattern: str, text: str) -> bool:
        return (
            re.search(
                rf"(?<![A-Za-z0-9]){re.escape(pattern)}(?![A-Za-z0-9])",
                text,
                re.IGNORECASE,
            )
            is not None
        )

    hits = [rule for rule in rule_dicts if matches(rule["pattern"], description)]
    if not hits:
        return None
    # min over a negated length, not max: max() breaks a tie toward the
    # first element of whatever order it was handed, which is precisely
    # the bug. Sorting on (-len, id) makes the answer independent of the
    # order the rules arrived in.
    return min(hits, key=lambda rule: (-len(rule["pattern"]), rule["id"]))


PARITY_DESCRIPTIONS = [
    "ALPHA BRAVO PLAZA",
    "ALPHA PLAZA",
    "BRAVO PLAZA",
    "CHARLIE PLAZA",
    "PREALPHA BRAVO",
    "alpha bravo plaza",
    "ALPHA-BRAVO",
    "ALPHA.BRAVO/PLAZA",
    "ALPHA1234 BRAVO",
    "ALPHAS BRAVO",
    "ALPHA BRAVO ALPHA",
    "NOTHING HERE",
]


@pytest.mark.parametrize("description", PARITY_DESCRIPTIONS)
def test_the_preview_and_the_matcher_agree(rules, description):
    """Parity across ties, single matches, no match, and the token
    boundaries — not only the tie.

    The ties are what was broken, but pinning only those would let a
    later change to `_pattern_matches` break parity everywhere else with
    nothing failing. `PREALPHA`, `ALPHAS` and `ALPHA1234` are the
    boundary cases: a pattern buried in a longer word matches on neither
    side.
    """
    rules.add_with_ids((HIGH_ID, "ALPHA"), (LOW_ID, "BRAVO"))
    rule_dicts = [
        dict(row)
        for row in rules.connection.execute("SELECT * FROM merchant_rules").fetchall()
    ]

    preview = _preview_winner(rule_dicts, description)
    matched = _match_merchant_rule(rules.connection, description)

    expected = None if preview is None else preview["pattern"]
    actual = None if matched is None else matched["pattern"]
    assert actual == expected, (
        f"preview and matcher disagree on {description!r}: "
        f"preview {expected}, matcher {actual}"
    )


def test_the_preview_and_a_real_import_agree_on_a_tie(client, test_settings):
    """The one end-to-end case, because the unit tests above share a
    connection and so cannot catch a divergence introduced by the route.

    The rule list is fetched the way a correct preview must fetch it —
    paged to `total`. `GET /merchant_rules` defaults to `limit=50`, so a
    single call is not the rule set once there are more than 50 rules
    (ticket T-47); a differential test that read one page would be
    comparing against a truncated rule list.
    """
    from api.tests.test_financial import _auth, _cents_account, _cents_import

    headers = _auth(test_settings)
    # limit=200 is MAX_LIMIT and there are 80 seeded categories, but
    # assert rather than assume: /categories defaults to 50 and a
    # truncated list here would quietly narrow which two categories the
    # test picks (D20's addendum — a truncated list answers wrongly in
    # the shape of a right answer).
    page = client.get("/categories?limit=200", headers=headers).json()
    categories = page["items"]
    assert len(categories) == page["total"], "categories listing was truncated"
    alpha_category, bravo_category = categories[0]["id"], categories[1]["id"]
    assert alpha_category != bravo_category

    connection = sqlite3.connect(test_settings.db_path)
    try:
        # ALPHA first: lower rowid, higher id.
        connection.execute(
            "INSERT INTO merchant_rules (id, pattern, category_id, entity_id) "
            "VALUES (?, 'ALPHA', ?, NULL)",
            (HIGH_ID, alpha_category),
        )
        connection.execute(
            "INSERT INTO merchant_rules (id, pattern, category_id, entity_id) "
            "VALUES (?, 'BRAVO', ?, NULL)",
            (LOW_ID, bravo_category),
        )
        connection.commit()
    finally:
        connection.close()

    fetched: list[dict] = []
    while True:
        page = client.get(
            f"/merchant_rules?limit=50&offset={len(fetched)}", headers=headers
        ).json()
        fetched.extend(page["items"])
        if len(fetched) >= page["total"] or not page["items"]:
            break

    preview = _preview_winner(fetched, "ALPHA BRAVO PLAZA")

    account = _cents_account(client, test_settings)
    _cents_import(
        client,
        test_settings,
        account["id"],
        [
            {
                "txn_date": "2026-03-04",
                "description": "ALPHA BRAVO PLAZA",
                "amount_cents": -100,
            }
        ],
    )
    imported = client.get(
        f"/transactions?account_id={account['id']}", headers=headers
    ).json()["items"][0]

    assert preview is not None
    assert imported["category_id"] == preview["category_id"] == bravo_category


# --- named rules whose id order and age disagree (impl-1) ------------------
#
# Written by impl-1 and folded in here. The cases above use synthetic
# ALPHA/BRAVO patterns with ids chosen to disagree; these use invented
# named rules with invented write days.
#
# The ids matter. MARTZ was written on day 2 and SHELL on day 9, but SHELL's
# uuid sorts lower, so "lowest id" and "oldest" name different winners here
# — which is what makes this the fixture that discriminates the two
# candidate keys. BREW/ESSO does not: BREW is both the oldest and the
# lowest id, so it would stay green under either, and under scan order too.

MARTZ_ID = "d0000000-0000-4000-8000-000000000001"  # written day 2
SHELL_ID = "c0000000-0000-4000-8000-000000000002"  # written day 9, lower id
BREW_ID = "80000000-0000-4000-8000-000000000003"  # written day 1
ESSO_ID = "90000000-0000-4000-8000-000000000004"  # written day 8
SUBSHOP_ID = "50000000-0000-4000-8000-000000000005"  # written day 3
BULKCO_ID = "40000000-0000-4000-8000-000000000006"  # written day 7, lower id


def test_equal_length_tie_prefers_the_lowest_id(rules):
    """Named for what it checks, not for what D15 first called it.

    The decision was recorded as "the oldest rule wins" and corrected:
    ids are uuid4 and there is no created_at, so lowest-id is arbitrary
    with respect to age. MARTZ was written first and SHELL wins, which is
    the whole point of the rename — a test called "prefers the oldest"
    would be asserting the opposite of its own name here.
    """
    rules.add_with_ids((MARTZ_ID, "MARTZ"), (SHELL_ID, "SHELL"))

    assert _matched(rules, "MARTZ SHELL PLAZA") == "SHELL"


def test_equal_length_tie_is_independent_of_insert_order(rules):
    """The same two rules inserted the other way round, same answer.

    impl-1's original net, and the only one of their three that was red
    against unfixed main: the unordered SELECT handed max() the rows in
    insertion order, so flipping the inserts flipped the winner while
    ids, patterns and description were all unchanged.
    """
    rules.add_with_ids((SHELL_ID, "SHELL"), (MARTZ_ID, "MARTZ"))

    assert _matched(rules, "MARTZ SHELL PLAZA") == "SHELL"


def test_a_tie_whose_id_order_and_age_agree_is_unchanged(rules):
    """BREW/ESSO, the pair D15 named. BREW is both the oldest rule and
    the lowest id, so the correction does not move it. Kept because the
    decision cites it: someone checking the runbook against the code
    should find the named example here, and find that it agrees."""
    rules.add_with_ids((BREW_ID, "BREW"), (ESSO_ID, "ESSO"))

    assert _matched(rules, "BREW ESSO PLAZA") == "BREW"


def test_a_longer_pattern_still_beats_a_lower_id_shorter_one(rules):
    """Length outranks id; the tie-break applies only WITHIN a length.

    Guards the obvious wrong fix of sorting by id alone, which would let
    every broad rule with a low id shadow every specific one with a high
    id and quietly undo longest-pattern-wins. BULKCO carries the lower
    id of the two, so ordering by id alone would pick it.
    """
    rules.add_with_ids((BULKCO_ID, "BULKCO"), (SUBSHOP_ID, "BULKCO GAS BAR"))

    assert (
        _matched(rules, "BULKCO GAS BAR #4321 ANYTOWN XY") == "BULKCO GAS BAR"
    )
