"""What `total` MEANS, pinned — not merely that it exists.

Every list endpoint returns `{items, total, limit, offset}` through
`paginate` in api/db.py, and both SKILL.md fences terminate their paging
loop on `len(items) >= page["total"]`. That makes the *meaning* of `total`
load-bearing in a way its presence is not.

If `total` were ever redefined as "rows in this response" — a plausible
tidy-up, since every other field in the envelope describes the page — the
fences would terminate after page one, silently and always, and every
existing test would still pass: they assert `total` is present and, where
they assert a number, they assert it for a set that fits in one page.
Removal is a loud KeyError. Redefinition is invisible. This file exists
for the invisible one.

Raised by impl-1 against the pagination work (tickets T-45/T-47), whose
own helper terminates on the same comparison — so a redefinition would
defeat the fix written to prevent exactly this class of bug.
"""

from api.config import get_settings
from api.db import get_connection, paginate


def _auth(settings):
    return {"Authorization": f"Bearer {settings.api_token}"}


def _make_entities(client, settings, count):
    for number in range(count):
        response = client.post(
            "/entities",
            json={"type": "pet", "name": f"Pet {number}"},
            headers=_auth(settings),
        )
        assert response.status_code == 200, response.text


def test_total_counts_every_match_not_the_rows_returned(client, test_settings):
    """The contract, through the endpoint a caller actually uses."""
    _make_entities(client, test_settings, 5)

    body = client.get(
        "/entities", params={"type": "pet", "limit": 2}, headers=_auth(test_settings)
    ).json()

    assert len(body["items"]) == 2, "the page"
    assert body["total"] == 5, "every matching row, not the two returned"
    assert body["total"] != len(body["items"]), (
        "if these are ever equal by construction, a paging loop that stops "
        "on len(items) >= total stops after one page"
    )


def test_total_is_unchanged_by_how_far_the_caller_has_paged(client, test_settings):
    """`total` describes the set, not the caller's position in it.

    A `total` that shrank as the caller advanced would also satisfy "the
    count of something", and would break termination in the other
    direction — the loop would stop early rather than late.
    """
    _make_entities(client, test_settings, 5)

    totals = [
        client.get(
            "/entities",
            params={"type": "pet", "limit": 2, "offset": offset},
            headers=_auth(test_settings),
        ).json()["total"]
        for offset in (0, 2, 4)
    ]

    assert totals == [5, 5, 5], totals


def test_total_survives_an_offset_past_the_end(client, test_settings):
    """An empty page still reports the size of the set it is past.

    This is the case where "rows returned" and "rows matching" differ most
    starkly: zero against five.
    """
    _make_entities(client, test_settings, 5)

    body = client.get(
        "/entities",
        params={"type": "pet", "limit": 2, "offset": 99},
        headers=_auth(test_settings),
    ).json()

    assert body["items"] == []
    assert body["total"] == 5


def test_paginate_itself_holds_the_contract(test_settings):
    """Pinned at the one place it is computed.

    Every list endpoint routes through `paginate`, so asserting the
    property here covers endpoints this file does not name — including
    ones added later, which is the point.
    """
    generator = get_connection(settings=test_settings)
    connection = next(generator)
    try:
        result = paginate(
            connection,
            "SELECT id, name FROM categories WHERE parent_id IS NULL ORDER BY name",
            (),
            limit=3,
            offset=0,
        )
    finally:
        generator.close()

    assert len(result["items"]) == 3, "the page is the size we asked for"
    # The PROPERTY, not a number: `total` exceeds what this page holds.
    # It said `> 3` before, which is a floor tied to how many categories
    # happen to be seeded -- and a floor stops discriminating as soon as
    # the population grows past it. Comparing against the page itself
    # cannot drift with the seed data, and fails immediately if `total`
    # is ever redefined as "rows in this response", which is the exact
    # redefinition this file exists to catch.
    assert result["total"] > len(result["items"]), (
        "the whole matching set, not this page"
    )
