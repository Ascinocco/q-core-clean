"""The statement-intake preview must predict what the import actually does.

Both fences are EXTRACTED from `plugin/skills/statement-intake/SKILL.md`
rather than retyped, so these pin the text a future session will follow.
A copy here would drift from the skill silently, and the drift is the
failure: the preview's only value is that it predicts the import.

The two sides come from DIFFERENT SOURCES on purpose:

  API side   -- drive source preview/commit and read back the categories it
                really wrote, against the full rule set.
  skill side -- run the skill's own paged fetch and its own matcher over
                whatever that fetch actually returns.

An earlier version of this harness built both sides from one in-memory
rule list. That made it structurally blind to the class of bug where the
skill fetches the wrong NUMBER of rules -- which was real (ticket
ticket T-47: more rules than one default page of 50) and which a same-source
harness stayed green throughout. The rule that came out of it: if a test
cannot tell you how many records each side saw, it cannot see this class.
"""

from tests.source_import_support import seed_source_batch

import re

import pytest

from api.config import REPO_ROOT

H = {"Authorization": "Bearer test-token"}
SKILL = REPO_ROOT / "plugin/skills/statement-intake/SKILL.md"

# Deliberately more than one default page (50), so the pagination class is
# reachable. A fixture that fits in one page cannot fail the way this file
# exists to catch.
RULE_COUNT = 60


def _fence(defines: str):
    """The one python fence in SKILL.md that defines `defines`."""
    blocks = re.findall(r"```python\n(.*?)```", SKILL.read_text(), re.S)
    hits = [b for b in blocks if defines in b]
    assert len(hits) == 1, f"expected 1 fence defining {defines!r}, got {len(hits)}"
    return compile(hits[0], f"<skill:{defines}>", "exec")


def _skill_fetch_all(list_tool):
    namespace: dict = {}
    exec(_fence("def fetch_all"), namespace)
    return namespace["fetch_all"](list_tool)


def _skill_winner(rules, description):
    """Run the matcher fence WHOLE -- driver lines included.

    Executing only `def matches` and re-implementing the driver here would
    leave SKILL.md's filter-then-longest ordering untested, which is the
    half that decides the category.
    """
    namespace = {"rules": rules, "description": description}
    exec(_fence("def matches"), namespace)
    return namespace["winner"]


def _list_tool(client):
    def tool(limit, offset):
        return client.get(
            "/merchant_rules", headers=H, params={"limit": limit, "offset": offset}
        ).json()

    return tool


@pytest.fixture()
def seeded(client):
    categories = [
        c["id"]
        for c in client.get("/categories", headers=H, params={"limit": 200}).json()["items"]
    ]
    assert len(categories) >= 2

    patterns = [f"MERCHANT {i:03d}" for i in range(RULE_COUNT)]
    for index, pattern in enumerate(patterns):
        response = client.post(
            "/merchant_rules",
            headers=H,
            json={"pattern": pattern, "category_id": categories[index % len(categories)], "actor": "test"},
        )
        assert response.status_code in (200, 201), response.text

    account = client.post(
        "/entities",
        headers=H,
        json={
            "type": "account",
            "name": "Test Chequing",
            "attributes": {"institution": "Bank C", "last4": "4321"},
        },
    )
    assert account.status_code == 200, account.text
    return {"account_id": account.json()["id"], "patterns": patterns}


def _descriptions(patterns):
    """Realistic statement shapes spread across the WHOLE rule set.

    Includes rules near the end deliberately: under a single unpaginated
    fetch those are exactly the ones the preview cannot see.
    """
    out = []
    for pattern in patterns:
        out += [
            f"{pattern} #4321 ANYTOWN XY",   # separated by a space
            f"{pattern}*AB12",               # separated by a star
            # NOT token-bounded: an alphanumeric on one side. A bare
            # substring matcher matches these and the API does not, so
            # these are the descriptions that discriminate the predicate.
            # Without them the whole corpus agrees under either rule --
            # measured: with the predicate mutated to bare substring and
            # only the two shapes above, all four tests still passed.
            f"{pattern}9",
            f"X{pattern}",
        ]
    return out + ["ENTIRELY UNKNOWN VENDOR 77", "MERCHANT999 NO BOUNDARY"]


def test_the_skill_fetch_returns_every_rule(client, seeded):
    """The count guard. Necessary, and NOT sufficient -- see the test below."""
    rules = _skill_fetch_all(_list_tool(client))
    assert len(rules) == RULE_COUNT
    assert len({rule["id"] for rule in rules}) == RULE_COUNT, "dupes or gaps"


def test_the_skill_fetch_terminates_on_an_empty_page():
    """An empty page with a non-zero total must not spin forever.

    `total` is counted in a separate query from the page, so a row deleted
    between the two leaves `total` permanently above what any offset can
    return. Without the guard the loop advances by zero and an unattended
    run hangs silently rather than failing. This test was written before
    the guard and failed against the unguarded loop.
    """
    calls = {"n": 0}

    def hostile(limit, offset):
        calls["n"] += 1
        if calls["n"] > 50:
            raise AssertionError("fetch_all did not terminate")
        return {"items": [], "total": 7, "limit": limit, "offset": offset}

    assert _skill_fetch_all(hostile) == []


def test_preview_predicts_every_category_the_import_writes(client, seeded):
    account_id, patterns = seeded["account_id"], seeded["patterns"]
    descriptions = _descriptions(patterns)

    rules = _skill_fetch_all(_list_tool(client))
    predicted = {d: _skill_winner(rules, d) for d in descriptions}

    transactions = [
        {"txn_date": "2026-03-12", "description": description, "amount_cents": -100 - i}
        for i, description in enumerate(descriptions)
    ]
    response = seed_source_batch(client, headers=H, json={
            "account_id": account_id,
            "period_start": "2026-05-01",
            "period_end": "2026-05-31",
            "transactions": transactions,
        })
    assert response.status_code == 200, response.text

    # Drained, not a single call. A `limit=200` read-back silently caps at
    # MAX_LIMIT, so once the corpus grew past 200 rows the API side of this
    # differential was truncated -- the exact bug class this file exists to
    # catch, in the file itself. Found by growing the fixture, not by review.
    stored = _skill_fetch_all(
        lambda limit, offset: client.get(
            "/transactions",
            headers=H,
            params={"account_id": account_id, "limit": limit, "offset": offset},
        ).json()
    )
    actual = {t["description"]: t["category_id"] for t in stored}
    assert len(actual) == len(descriptions)

    mismatches = [
        (d, actual[d], predicted[d]["category_id"] if predicted[d] else None)
        for d in descriptions
        if actual[d] != (predicted[d]["category_id"] if predicted[d] else None)
    ]
    assert not mismatches, (
        f"preview disagreed with the import on {len(mismatches)} of "
        f"{len(descriptions)}: {mismatches[:5]}"
    )


def test_a_truncated_fetch_makes_the_preview_disagree(client, seeded):
    """The differential must discriminate a truncated fetch ON ITS OWN.

    Without this, the only thing catching ticket T-47 above is the
    count guard -- and a guard firing is an ADJACENT result, not proof the
    differential can see the class. Measured: with the guard neutralised
    and the fetch truncated to one page, 20 of 122 descriptions disagree,
    every one of them the import assigning a category where the preview
    predicted None.
    """
    first_page = client.get("/merchant_rules", headers=H).json()
    assert len(first_page["items"]) < first_page["total"], "need >1 page"

    seen = {rule["pattern"] for rule in first_page["items"]}
    invisible = [p for p in seeded["patterns"] if p not in seen]
    assert invisible, "expected rules past the first page"

    description = f"{invisible[0]} #4321 ANYTOWN XY"
    assert _skill_winner(first_page["items"], description) is None
    assert _skill_winner(_skill_fetch_all(_list_tool(client)), description) is not None
