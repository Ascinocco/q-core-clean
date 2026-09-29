"""One convention for a read that names something which does not exist.

An empty page is a confident "there are none", and a caller cannot tell
it from the true one. Eleven of the fourteen id-shaped FILTERS on this
API answered a ghost id that way, while five other reads refused --
including `list_relationships` and `list_reminders`, two `entity_id`
filters in the same surface answering the same mistake in opposite ways
(ticket T-08).

THE CONVENTION, by D17's own table:

  * a value INSIDE the request that refers to something absent is a 400
    `invalid_reference` -- the URL addresses a real thing, the filter
    does not. Every id-shaped query parameter.
  * the thing ADDRESSED BY THE URL not existing is a 404 `not_found`.
    Every id-shaped path parameter.

The filter list is DISCOVERED from the routes, not written here: a list
in a test file is a list someone has to remember to extend, and the next
read added with an unvalidated filter is exactly the case this exists to
catch.

Driven through the API rather than read out of the source. A
source-reading test would assert that `require_reference` is CALLED,
which is not the claim -- the claim is that the request is refused, and
a call that passed the wrong table or the wrong variable would satisfy
the source and still answer 200.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from api.tests.test_financial import _auth

API = pathlib.Path(__file__).resolve().parent.parent

GHOST = "00000000-0000-4000-8000-000000000000"

#: Path-addressed reads: the URL names the thing, so a miss is a 404.
#: Short enough to be literal, and each is checked below.
PATH_ADDRESSED_READS = [
    f"/entities/{GHOST}/relationships",
    f"/entities/{GHOST}/cost_of_ownership",
    f"/tickets/{GHOST}/transitions",
    f"/merchant_rules/{GHOST}/history",
]


def _id_filters() -> list[tuple[str, str]]:
    """(route path, query parameter) for every id-shaped read filter."""
    found: list[tuple[str, str]] = []
    for path in sorted(API.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            gets = [
                d
                for d in node.decorator_list
                if isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "get"
            ]
            if not gets or not gets[0].args:
                continue
            route = gets[0].args[0].value
            for arg in node.args.args:
                if arg.arg.endswith("_id") and "{" + arg.arg + "}" not in route:
                    found.append((route, arg.arg))
    return sorted(found)


def test_the_discovery_finds_the_filters_it_should():
    """Guards the guard.

    A walk that found nothing would report the convention enforced
    across a set it never looked at -- the failure this file exists to
    catch, one level up. Asserted by naming two filters that must be
    there and that live in different modules, rather than by a count: a
    count only has to be lowered once to stop meaning anything.
    """
    found = _id_filters()
    assert found, "discovery found no id-shaped read filters -- the walk broke"
    assert ("/transactions", "account_id") in found, found
    assert ("/reminders", "entity_id") in found, found
    # The two that disagreed with each other before this change.
    assert ("/boards", "entity_id") in found, found


@pytest.mark.parametrize("route, param", _id_filters())
def test_every_id_filter_refuses_an_id_that_names_nothing(
    client, test_settings, route, param
):
    """400, naming the thing, for every one of them.

    Parametrized over the DISCOVERED set, so a read added later with an
    unvalidated filter fails here by existing.
    """
    response = client.get(f"{route}?{param}={GHOST}", headers=_auth(test_settings))

    assert response.status_code == 400, (
        f"GET {route}?{param}= a ghost id answered {response.status_code}. "
        f"An empty page here is indistinguishable from the true answer: "
        f"{response.text[:200]}"
    )
    body = response.json()["error"]
    assert body["code"] == "invalid_reference", body
    assert GHOST in body["message"], (
        f"the refusal must name the id that was not found: {body['message']}"
    )


@pytest.mark.parametrize("url", PATH_ADDRESSED_READS)
def test_a_path_addressed_read_is_a_404_not_a_400(client, test_settings, url):
    """The other half of the convention, and the reason it is two rules.

    D17 splits them: the URL naming nothing is 404, a value inside the
    request naming nothing is 400. Without this half, "make them all
    agree" would flatten a distinction the error vocabulary depends on.
    """
    response = client.get(url, headers=_auth(test_settings))

    assert response.status_code == 404, response.text[:200]
    assert response.json()["error"]["code"] == "not_found", response.text[:200]


# --- the three siblings, same family: an input error rendered as empty ---


def test_an_unknown_doc_type_is_refused_naming_the_nine(client, test_settings):
    """Ticket T-11. The WRITER knew the enum and the READER did not.

    `register_document` refuses "wombat" and names all nine permitted
    values; `list_documents` took it and answered with an empty page, so
    a caller who wrote "policy" for "insurance_policy" was told there are
    no documents of that type. One call earlier, the same mistake would
    have been corrected with the list.
    """
    from api.models import DOC_TYPES

    response = client.get(
        "/documents?doc_type=wombat", headers=_auth(test_settings)
    )

    assert response.status_code == 422, response.text[:200]
    message = response.json()["error"]["details"][0]["message"]
    for permitted in DOC_TYPES:
        assert permitted in message, (
            f"the refusal must name every permitted value; {permitted!r} "
            f"is missing from: {message}"
        )


def test_the_doc_type_vocabulary_is_one_list_not_two(client, test_settings):
    """DOC_TYPES is derived from the DocType Literal with get_args.

    Two lists describing one set drift, and this pair had already drifted
    into one place enforcing it and one place not. Asserting the derived
    tuple against the Literal keeps the reader's filter and the writer's
    field from separating again.
    """
    from typing import get_args

    from api.models import DOC_TYPES, DocType

    assert DOC_TYPES == get_args(DocType)
    assert "insurance_policy" in DOC_TYPES


def test_a_reversed_date_range_is_refused_naming_both_dates(
    client, test_settings
):
    """Ticket T-15, and /due already did this.

    An impossible range cannot match anything, so an empty page is not an
    answer -- it is a refusal wearing the clothes of one. Two endpoints
    taking the same pair of dates should not disagree about whether
    swapping them is a mistake.
    """
    response = client.get(
        "/transactions?date_from=2026-09-30&date_to=2026-09-01",
        headers=_auth(test_settings),
    )

    assert response.status_code == 422, response.text[:200]
    message = response.json()["error"]["details"][0]["message"]
    assert "2026-09-30" in message and "2026-09-01" in message, message


def test_due_and_transactions_refuse_a_reversed_range_the_same_way(
    client, test_settings
):
    """The point is that they AGREE, so the test reads both.

    Asserting only the new one would let the pair drift apart again
    from the other side.
    """
    headers = _auth(test_settings)
    transactions = client.get(
        "/transactions?date_from=2026-09-30&date_to=2026-09-01", headers=headers
    )
    due = client.get("/due?from=2026-09-30&to=2026-09-01", headers=headers)

    assert transactions.status_code == due.status_code == 422
    assert (
        transactions.json()["error"]["code"] == due.json()["error"]["code"]
    ), "the same mistake must carry the same error code on both reads"


def test_a_deleted_rules_history_still_reads(client, test_settings):
    """THE CASE THE TICKET WOULD HAVE BROKEN.

    ticket T-16 asked for a plain 404 on a rule that does not exist.
    `merchant_rule_history`'s docstring already refused that, and it is
    right: "what did this say before it was removed, and who removed it"
    is unanswerable if the lookup 404s on the deletion being asked
    about. `merchant_rule_changes.rule_id` carries no foreign key for
    exactly that reason.

    So the gate is on the id having been SEEN, not on the rule existing.
    """
    headers = _auth(test_settings)
    rule = client.post(
        "/merchant_rules",
        json={"pattern": "TEMPORARY", "category_id": "food", "actor": "tester"},
        headers=headers,
    ).json()
    client.patch(
        f"/merchant_rules/{rule['id']}",
        json={"category_id": "transfers_account_transfer", "actor": "tester"},
        headers=headers,
    )
    client.delete(
        f"/merchant_rules/{rule['id']}?actor=tester", headers=headers
    )

    response = client.get(
        f"/merchant_rules/{rule['id']}/history", headers=headers
    )

    assert response.status_code == 200, (
        "a deleted rule's history is the primary reason this endpoint is "
        f"not gated on the rule existing: {response.text[:200]}"
    )
    assert response.json()["total"] >= 1


def test_a_rule_id_nobody_has_ever_used_is_a_404(client, test_settings):
    """The half of ticket T-16 that was real.

    A mistyped id returned the same empty body as a real rule with no
    changes, so "you asked about nothing" and "this rule was never
    edited" were indistinguishable -- which is the one question an audit
    trail exists to answer.
    """
    response = client.get(
        f"/merchant_rules/{GHOST}/history", headers=_auth(test_settings)
    )

    assert response.status_code == 404, response.text[:200]
    assert GHOST in response.json()["error"]["message"]


def test_a_live_rule_with_no_changes_is_an_empty_page_not_a_404(
    client, test_settings
):
    """The discrimination the 404 exists to make, from the other side.

    If this 404'd, the gate would be answering "has it changed" instead
    of "does this id mean anything" -- and a caller could not tell an
    unedited rule from a typo, which is where we started.
    """
    headers = _auth(test_settings)
    rule = client.post(
        "/merchant_rules",
        json={"pattern": "UNTOUCHED", "category_id": "food", "actor": "tester"},
        headers=headers,
    ).json()

    response = client.get(
        f"/merchant_rules/{rule['id']}/history", headers=headers
    )

    assert response.status_code == 200, response.text[:200]
