"""The skill's fence must resolve EQUAL-LENGTH ties the way the API does.

Separate from the preview-parity tests because those cannot see this:
their fixture seeds patterns of a single length, so no description ever
produces a tie and the tie-break key is never exercised. Measured — with
the fence's tie direction inverted, that suite still passes 4/4. A test
that cannot reach the case cannot vouch for it.

The fence is EXTRACTED from SKILL.md rather than retyped, so this fails
if the skill drifts from `api/financial.py`'s
`ORDER BY length(pattern) DESC, id ASC`.
"""

from tests.source_import_support import seed_source_batch

import re

import pytest

from api.config import REPO_ROOT

H = {"Authorization": "Bearer test-token"}
SKILL = REPO_ROOT / "plugin/skills/statement-intake/SKILL.md"

# Equal length, and both match the one description below: an invented
# equal-length collision.
TIE_A, TIE_B = "BREW", "FUEL"
TIE_DESCRIPTION = "BREW FUEL PLAZA"


def _skill_winner(rules, description):
    blocks = re.findall(r"```python\n(.*?)```", SKILL.read_text(), re.S)
    hits = [b for b in blocks if "def matches(" in b]
    assert len(hits) == 1, f"expected 1 matcher fence, got {len(hits)}"
    namespace = {"rules": rules, "description": description}
    exec(compile(hits[0], "<skill:matcher>", "exec"), namespace)
    return namespace["winner"]


@pytest.fixture()
def tied(client):
    """Two equal-length rules with different categories, both matching.

    Ids are uuid4 assigned by the API, so which one is lower is not
    controllable — it is read back. That is stronger than fixing the ids:
    it tests whatever the API actually assigned, which is what the live
    tie-break will rank.
    """
    categories = [
        c["id"]
        for c in client.get("/categories", headers=H, params={"limit": 200}).json()["items"]
    ]
    rules = []
    for pattern, category in ((TIE_A, categories[0]), (TIE_B, categories[1])):
        response = client.post(
            "/merchant_rules", headers=H, json={"pattern": pattern, "category_id": category, "actor": "test"}
        )
        assert response.status_code in (200, 201), response.text
        rules.append(response.json())
    assert len({len(r["pattern"]) for r in rules}) == 1, "fixture must be equal-length"
    assert rules[0]["category_id"] != rules[1]["category_id"]

    account = client.post(
        "/entities",
        headers=H,
        json={"type": "account", "name": "Tie Test", "attributes": {"last4": "0001"}},
    )
    assert account.status_code == 200, account.text
    return {"rules": rules, "account_id": account.json()["id"]}


def test_the_fence_and_the_import_agree_on_an_equal_length_tie(client, tied):
    expected = min(tied["rules"], key=lambda r: r["id"])

    listed = client.get("/merchant_rules", headers=H, params={"limit": 200}).json()["items"]

    # Handed to the fence REVERSED, and that is the whole point of this
    # test rather than a flourish. `list_merchant_rules` is ORDER BY id,
    # and `max` breaks ties toward the first element of the list it is
    # given -- so over an id-ordered list the OLD fence already resolved
    # to the lowest id, by coincidence of someone else's ORDER BY. Fed
    # id-ordered input this test passes against the pre-mirror fence and
    # proves nothing (measured: it did). Reversed, it observes the change.
    #
    # It is also the realistic hazard: any client that re-sorts, or pages
    # and concatenates out of order, hands the fence exactly this.
    predicted = _skill_winner(list(reversed(listed)), TIE_DESCRIPTION)
    assert predicted is not None

    response = seed_source_batch(client, headers=H, json={
            "account_id": tied["account_id"],
            "period_start": "2026-05-01",
            "period_end": "2026-05-31",
            "transactions": [
                {"txn_date": "2026-03-12", "description": TIE_DESCRIPTION, "amount_cents": -100}
            ],
        })
    assert response.status_code == 200, response.text
    stored = client.get(
        "/transactions", headers=H, params={"account_id": tied["account_id"]}
    ).json()["items"]
    assert len(stored) == 1

    # The import is the authority; the fence must have predicted it, and
    # both must be the lowest id.
    assert stored[0]["category_id"] == expected["category_id"]
    assert predicted["category_id"] == stored[0]["category_id"], (
        "the skill's fence disagrees with the import on a tie"
    )


def test_a_longer_pattern_still_beats_a_lower_id():
    """Length outranks id -- the tie-break applies only WITHIN a length.

    Ids are constructed here rather than taken from the API, and that is
    the point: uuid4 ids are random, so an API-seeded version of this test
    only catches the bug when the longer rule happens to draw a high id.
    Written that way first, it passed against a fence ranking by id alone
    -- green by luck, which is the same trap that made BREW/FUEL a bad
    tie fixture.

    Here the longer pattern is given the HIGHEST id, so ranking by id
    alone must pick a shorter one and fail.
    """
    rules = [
        {"id": "0001", "pattern": "BREW", "category_id": "coffee"},
        {"id": "0002", "pattern": "FUEL", "category_id": "auto_fuel"},
        {"id": "9999", "pattern": "BREW FUEL", "category_id": "combined"},
    ]
    winner = _skill_winner(rules, TIE_DESCRIPTION)
    assert winner["pattern"] == "BREW FUEL", "length must outrank id"
    assert winner["category_id"] == "combined"


def test_the_lowest_id_wins_among_equal_lengths():
    """The tie-break itself, with ids that make the direction observable.

    Insertion order is deliberately the reverse of id order, so a fence
    that returned the first match in list order would also fail here.
    """
    rules = [
        {"id": "0002", "pattern": "FUEL", "category_id": "auto_fuel"},
        {"id": "0001", "pattern": "BREW", "category_id": "coffee"},
    ]
    winner = _skill_winner(rules, TIE_DESCRIPTION)
    assert winner["category_id"] == "coffee", "lowest id must win"
