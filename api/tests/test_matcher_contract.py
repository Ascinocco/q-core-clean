"""`MATCHER_CONTRACT` must describe the matcher it sits beside.

A tool description that states behaviour is an untested claim. The one
this exists for said "matching is by substring" for four PRs after #72
made it token-bounded, and was noticed only because someone happened to
be editing four lines above — a model reading it would expect `ACME FUEL`
to match `ACMEFUEL41` and write rules on that basis.

Exporting the sentence as a constant fixes half of that: the four
descriptions interpolate it, so they cannot disagree with each other or
with the constant. It does NOT fix the other half. A constant can go
stale beside its code exactly as prose can — nothing about
`MATCHER_CONTRACT = "..."` forces the matcher to behave that way.

So this file closes the second half: every clause of the contract is
exercised against the real matcher, and the contract string is asserted
to name the property each clause tests. Change the boundary regex or the
ORDER BY, and a test named for the contract fails — which is what makes
the description trustworthy rather than merely consistent.
"""

import sqlite3

import pytest

from api.financial import MATCHER_CONTRACT, _match_merchant_rule, _pattern_matches
from api.tests.test_merchant_matching import _Rules


def test_the_contract_is_pinned_exactly():
    """Exact match, not "mentions these words".

    The first version of this test asserted the contract CONTAINED
    "token-bounded", "longest" and "lowest id". That is a lower bound with
    no upper bound, and review-1 found what it lets through:

        MATCHER_CONTRACT = "token-bounded and case-sensitive, longest
                            pattern wins, lowest id on ties"

    still contains all three, so all six tests passed — while shipping a
    FALSE claim (the matcher is case-insensitive) into four tool
    descriptions. Dropping a clause was caught; changing behaviour was
    caught; ADDING a claim was not. And an addition is precisely the edit
    that reaches four descriptions without passing through anything.

    An exact assertion has both bounds. Any reword — however small, and
    especially one that only adds — forces a deliberate update here, where
    the person doing it has to look at the clause tests below and decide
    whether the new claim is one of them.
    """
    assert MATCHER_CONTRACT == (
        "token-bounded and case-insensitive, longest pattern wins, "
        "lowest id on ties"
    ), (
        "the contract changed. Update this pin ONLY after adding or "
        "amending a clause test below, so every claim in the sentence is "
        "one something exercises."
    )


def test_the_matcher_is_case_insensitive_as_the_contract_says():
    """Clause 2. The property the false clause would have denied.

    Nothing pinned this before: the matcher passes re.IGNORECASE and a
    comment explains why, but no test would have noticed it going away —
    and a rule pattern differing only in case from a real description
    would then silently never match. No error, no unmatched flag, just
    permanently uncategorized.
    """
    assert _pattern_matches("esso", "ESSO 1234 ANYTOWN")
    assert _pattern_matches("ESSO", "Esso 1234 Anytown")
    assert _pattern_matches("AmAzOn", "AMAZON MKTPLACE")


def test_the_matcher_is_token_bounded_as_the_contract_says():
    """Clause 1. Fails if the boundary regex is relaxed."""
    assert _pattern_matches("ESSO", "ESSO 1234 ANYTOWN")
    assert _pattern_matches("ESSO", "FUELCO/ESSO")
    # The two real miscategorisations that prompted #72.
    assert not _pattern_matches("ESSO", "ZESSOR ACADEMY")
    assert not _pattern_matches("SHELL", "ZUMSHELLA SALON")
    # Digits are alphanumeric, so a pattern abutting one does not match.
    assert not _pattern_matches("ACME FUEL", "ACMEFUEL41")


def test_the_longest_pattern_wins_as_the_contract_says():
    """Clause 2. Fails if the ORDER BY stops leading with length."""
    rules = _Rules()
    rules.add("AMAZON", "AMAZON WEB SERVICES")

    winner = _match_merchant_rule(rules.connection, "AMAZON WEB SERVICES INC")

    assert winner["pattern"] == "AMAZON WEB SERVICES"


def test_ties_go_to_the_lowest_id_as_the_contract_says():
    """Clause 3. Fails if the ORDER BY stops breaking ties by id.

    Insertion order and id order are deliberately opposed, so a matcher
    that returned "whichever the scan reached first" would pass by
    accident half the time and could not be told from a correct one.
    """
    rules = _Rules()
    rules.add_with_ids(("zzz", "SHOP A"), ("aaa", "SHOP B"))

    winner = _match_merchant_rule(rules.connection, "SHOP A AND SHOP B")

    assert winner["id"] == "aaa", "equal-length patterns must resolve to the lowest id"


def test_the_contract_is_what_the_descriptions_actually_carry():
    """The other half: the four claiming descriptions interpolate it.

    Asserted against the rendered tool descriptions rather than the
    source, because a description assembled from f-strings can be correct
    in the file and wrong once built.
    """
    import asyncio

    import httpx

    from api.config import REPO_ROOT, Settings
    from q_core_mcp.client import QCoreClient
    from q_core_mcp.server import build_server

    settings = Settings(
        _env_file=None, api_token="t", db_path="/tmp/contract.db",
        documents_dir="/tmp/d", jyra_dir="/tmp/j", logs_dir="/tmp/l",
        intake_dir="/tmp/i", inbox_dir="/tmp/in",
        schema_path=str(REPO_ROOT / "db" / "schema.sql"),
        seed_categories_path=str(REPO_ROOT / "db" / "seed_categories.sql"),
        migrations_dir=str(REPO_ROOT / "db" / "migrations"),
    )
    server = build_server(
        QCoreClient(
            settings, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
        )
    )
    tools = {t.name: (t.description or "") for t in asyncio.run(server.list_tools())}

    claimants = (
        "create_merchant_rule",
        "update_merchant_rule",
        "list_merchant_rules",
        "apply_transaction_as_rule",
    )
    missing = [n for n in claimants if MATCHER_CONTRACT not in tools[n]]
    assert missing == [], f"these claim matcher behaviour without the contract: {missing}"

    # And no description claims substring matching, in any wording.
    #
    # Broadened from "bare substring" to "substring": the narrow phrase
    # only caught the exact sentence #72 left behind, so rewording the
    # contract to "substring matching, first rule wins" would have shipped
    # the same false claim past it. No description legitimately says
    # "substring" now — the contract replaced the only one that did — so
    # the word itself is the signal.
    #
    # This is deliberately independent of the exact pin above. The pin
    # guards the CONSTANT; this guards the RENDERED descriptions. A future
    # description that reintroduces the claim without touching the
    # constant is caught here and nowhere else.
    stale = [n for n, d in tools.items() if "substring" in d.lower()]
    assert stale == [], f"a substring-matching claim is back in: {stale}"


def test_the_server_side_id_claim_is_true_of_every_create_model():
    """Three descriptions say "the id is generated server-side; never
    supply one". That is a claim about the request models, and it is
    cheap to check against them rather than trusted.

    Asserted across EVERY `*Create` model, not only the three whose tools
    say it: a model that started accepting an `id` would make the claim
    false for a tool that does not mention it, and the tools that DO
    mention it would still read correctly. The claim is really about the
    API's shape, so it is checked there.
    """
    import api.models as models

    accepting = []
    for name in dir(models):
        candidate = getattr(models, name)
        if isinstance(candidate, type) and name.endswith("Create"):
            fields = getattr(candidate, "model_fields", None)
            if fields is not None and "id" in fields:
                accepting.append(name)

    assert accepting == [], (
        f"these request models accept a client-supplied id, so 'generated "
        f"server-side; never supply one' is no longer true: {accepting}"
    )
