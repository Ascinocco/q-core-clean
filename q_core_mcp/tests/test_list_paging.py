"""Paging behaviour shared by every tool that takes a `limit`.

The defect these tests exist for: `GET /merchant_rules` returned one page of a
larger total and said so only in a `total` field nobody was obliged to
read. The count being present is not the same as the count being noticed
— the same shape as the per-page `extracted_chars` that let a half
extracted statement return 200 (ticket T-62).

The subject here is "a tool that takes a `limit`", NOT "a tool whose name
starts with list_". Three paged tools — get_ticket_history,
spending_summary and trend — are not named list_*, so enumerating by name
would leave exactly those three able to truncate silently while the suite
reported the rule enforced.
"""

import asyncio

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from api.db import MAX_LIMIT
from q_core_mcp.client import QCoreClient
from q_core_mcp.paging import LIST_ITEM_CAP
from q_core_mcp.server import build_server


def _server(test_settings, handler):
    return build_server(
        QCoreClient(test_settings, transport=httpx.MockTransport(handler))
    )


#: Every tool that takes a `limit`. Literal, so adding a paged tool makes
#: someone open this file and decide whether the new one is covered.
PAGED_TOOL_COUNT = 24  # + list_notes (ticket T-23), + list_artifact_links (Canvas, ticket T-59)


def _paged_tools(tools):
    """The subject of every enumeration test here.

    The count is asserted because without it this function is the single
    point where all of them go vacuous together: review-1 replaced the
    body with `return []` and the whole file still passed, since every
    test iterates over what this returns and an empty iteration satisfies
    them all. A discovery-based guard that discovers nothing reports the
    rule enforced across a set it never looked at — which is the exact
    failure this file was written to catch, one level up.
    """
    paged = [t for t in tools if "limit" in (t.input_schema.get("properties") or {})]
    assert len(paged) == PAGED_TOOL_COUNT, (
        f"expected {PAGED_TOOL_COUNT} tools taking a limit, found {len(paged)}: "
        f"{sorted(t.name for t in paged)}. If a paged tool was added, cover it "
        "and raise the count; if this found none, the discovery itself is broken "
        "and every test in this file is passing over an empty set."
    )
    return paged


def _pages(total: int, page_size: int = MAX_LIMIT):
    """A handler serving `total` synthetic rows, honouring limit/offset."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        limit = int(params.get("limit", 50))
        offset = int(params.get("offset", 0))
        items = [{"id": f"r{n}"} for n in range(offset, min(offset + limit, total))]
        return httpx.Response(
            200, json={"items": items, "total": total, "limit": limit, "offset": offset}
        )

    return handler, seen


def test_a_single_page_that_is_not_the_whole_set_says_truncated(
    test_settings, call_tool
):
    """The defect: more rules than a page, 50 returned, and nothing said so."""
    handler, _ = _pages(75)

    result = call_tool(_server(test_settings, handler), "list_merchant_rules", {})

    assert result["total"] == 75, "every matching row"
    assert result["returned"] == 50, "what you are actually holding"
    assert "truncated" not in result, "no marker: the two counts are the signal"


def test_a_single_page_never_carries_a_truncation_marker(test_settings, call_tool):
    """Two earlier versions of this PR had a marker. Both were wrong.

    First an always-present boolean, then present-only-when-true. The
    second was better and still wrong for the reason the original bug
    happened: `total` was in every one of those N-of-M responses and
    nobody compared it. A second field to not-check has a poor prior, and
    a marker distributes the obligation to every caller that ever lists
    anything — forever, including callers not yet written — while a
    refusal concentrates it on the one call that actually hits the cap.
    """
    handler, _ = _pages(7)

    result = call_tool(_server(test_settings, handler), "list_merchant_rules", {})

    assert "truncated" not in result
    assert result["returned"] == 7
    assert result["total"] == 7


def test_the_tool_sends_no_limit_when_the_caller_gave_none(test_settings, call_tool):
    """The default of 50 belongs to the API and to nowhere else.

    Restating it in the tool put one default in thirteen places, which is
    how two of them come to disagree.
    """
    handler, seen = _pages(7)

    call_tool(_server(test_settings, handler), "list_merchant_rules", {})

    assert "limit" not in seen[0], seen[0]


def test_the_tool_sends_the_limit_the_caller_gave(test_settings, call_tool):
    handler, seen = _pages(60)

    call_tool(_server(test_settings, handler), "list_merchant_rules", {"limit": 5})

    assert seen[0]["limit"] == "5"


def test_all_pages_through_to_the_whole_set(test_settings, call_tool):
    handler, seen = _pages(60)

    result = call_tool(
        _server(test_settings, handler), "list_merchant_rules", {"all": True}
    )

    assert len(result["items"]) == 60
    assert result["returned"] == 60
    assert "truncated" not in result, "all=true is complete-or-refuse, so a result is whole"
    assert result["total"] == 60
    assert all(int(p["limit"]) == MAX_LIMIT for p in seen), "pages at the API maximum"


def test_all_stops_rather_than_asking_forever(test_settings, call_tool):
    """A server that ignores `offset` would page for ever.

    The loop stops on a page that adds nothing, so a broken or unusual
    endpoint costs one wasted request rather than an unbounded run — and
    then refuses, because nothing was read while `total` claims otherwise.
    Termination and honesty are separate properties and this asserts both:
    bounding the cost without refusing would return an empty set that
    looks complete.
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # total lies: it claims more than it will ever serve.
        return httpx.Response(200, json={"items": [], "total": 999, "limit": 200, "offset": 0})

    with pytest.raises(ToolError) as excinfo:
        call_tool(_server(test_settings, handler), "list_merchant_rules", {"all": True})

    assert calls["n"] == 1, "an empty page ends it — one wasted request, not a loop"
    # It used to return [] here, and that was wrong for the same reason
    # everything else in this file is: an empty set while `total` says 999
    # is a truncated result wearing the shape of a complete one. The loop
    # guard bounds the cost; the refusal is what stops it being silent.
    assert "truncated_result" in str(excinfo.value)


def test_all_above_the_cap_refuses_and_withholds_the_rows(test_settings, call_tool):
    """Named `truncated_result`, and it hands back nothing.

    Returning 2000 of 3000 while calling it truncated would be handing the
    thing over while calling it a refusal — which is exactly what
    partial_extraction declines to do (D26). This is that same shape, so
    the codebase has one answer to "the result is incomplete" rather than
    two decided hours apart.

    The refusal names the cap AND the total, because "too many" without
    either number leaves the caller unable to choose a narrower filter.
    """
    handler, _ = _pages(LIST_ITEM_CAP + 1)

    with pytest.raises(ToolError) as excinfo:
        call_tool(_server(test_settings, handler), "list_merchant_rules", {"all": True})

    message = str(excinfo.value)
    assert "truncated_result" in message
    assert str(LIST_ITEM_CAP) in message
    assert str(LIST_ITEM_CAP + 1) in message
    # No rows anywhere in the refusal: a ToolError carries a message, and
    # the message must not become a delivery mechanism for the data.
    assert "r0" not in message, "the refusal must not carry the rows"


def test_allow_truncated_opts_in_and_says_so(test_settings, call_tool):
    """The opt-in, exactly as allow_partial works in #88.

    The flag on the way back is the caller's own recorded acceptance, not
    a warning they must remember to look for — which is why a marker is
    right here and wrong on an ordinary page.
    """
    handler, _ = _pages(LIST_ITEM_CAP + 500)

    result = call_tool(
        _server(test_settings, handler),
        "list_merchant_rules",
        {"all": True, "allow_truncated": True},
    )

    assert result["truncated"] is True
    assert result["returned"] == LIST_ITEM_CAP
    assert result["total"] == LIST_ITEM_CAP + 500
    assert len(result["items"]) == LIST_ITEM_CAP


def test_exactly_the_cap_is_complete_and_not_an_error(test_settings, call_tool):
    """The boundary the amendment asks for explicitly.

    The cap and the truncation marker are computed from different facts —
    "more than all=true will read" versus "more than this page holds". If
    they were the same fact, a set of exactly the cap would either refuse
    or come back flagged, and an honest full read would cry wolf. It does
    neither: it is complete, unflagged, and not an error.
    """
    handler, _ = _pages(LIST_ITEM_CAP)

    result = call_tool(
        _server(test_settings, handler), "list_merchant_rules", {"all": True}
    )

    assert result["returned"] == LIST_ITEM_CAP
    assert result["total"] == LIST_ITEM_CAP
    assert "truncated" not in result


def test_all_never_returns_a_capped_set_with_a_flag(test_settings, call_tool):
    """Complete or refuse, with nothing in between.

    Returning 2000 of 3000 plus a marker would be the original defect with
    a longer fuse: `all=true` asserts completeness in its own name, so a
    caller has MORE reason to stop checking, not less.
    """
    handler, _ = _pages(LIST_ITEM_CAP + 500)

    with pytest.raises(ToolError):
        call_tool(_server(test_settings, handler), "list_merchant_rules", {"all": True})
    # and the opt-in is the ONLY way to get a capped set, never a default.


def test_all_refuses_when_the_server_serves_fewer_than_it_claims(
    test_settings, call_tool
):
    """A short read is still a partial set, and must not sneak past.

    Found by impl-3 asking whether all=true reports paginate's total or
    derives its own. It reports paginate's — but the branch that handles
    "fewer items than total" was reachable WITHOUT allow_truncated, and
    the comment above it claimed otherwise. A server that stops early, or
    a `total` that overstates what will be served, produced a partial set
    plus a marker and no opt-in: precisely the shape D27 forbids, arrived
    at by a route nobody was looking at.

    The comment asserting the property is not the guard enforcing it.
    """
    def handler(request):
        params = dict(request.url.params)
        offset = int(params.get("offset", 0))
        # Claims 60, serves 40 and then stops.
        items = [{"id": f"r{n}"} for n in range(offset, min(offset + 200, 40))]
        return httpx.Response(200, json={"items": items, "total": 60})

    with pytest.raises(ToolError) as excinfo:
        call_tool(_server(test_settings, handler), "list_merchant_rules", {"all": True})

    message = str(excinfo.value)
    assert "truncated_result" in message
    assert "40" in message and "60" in message, "both counts, so the gap is visible"


def test_all_reports_the_api_total_rather_than_recounting_what_it_fetched(
    test_settings, call_tool
):
    """`total` on the all=true path comes from the API, not from len().

    impl-3's question: the exposure is a path that computes its own count,
    because `total == returned` by construction makes a fence terminating
    on `len(items) >= total` true after page one. Asserted where the two
    genuinely differ — the opt-in read — because that is the only place a
    recount is observable.
    """
    handler, _ = _pages(LIST_ITEM_CAP + 500)

    result = call_tool(
        _server(test_settings, handler),
        "list_merchant_rules",
        {"all": True, "allow_truncated": True},
    )

    assert result["total"] == LIST_ITEM_CAP + 500, "the API's count, not a recount"
    assert result["total"] != result["returned"], (
        "if these were equal by construction, a fence stopping on "
        "len(items) >= total would stop after one page"
    )


def test_no_paged_tool_restates_the_api_default(test_settings, tools_by_name):
    """Enumerated by signature, not by name — see the module docstring."""
    tools = tools_by_name(
        _server(test_settings, lambda r: httpx.Response(200, json={}))
    )
    offenders = []
    for tool in _paged_tools(tools.values()):
        schema = tool.input_schema["properties"]["limit"]
        if schema.get("default") == 50 or "50" in (tool.description or ""):
            offenders.append(tool.name)

    assert offenders == [], f"the API's default restated in the tool layer: {offenders}"


def test_every_paged_tool_describes_its_paging(test_settings, tools_by_name):
    """A caller who cannot see that a call is paged cannot page it."""
    tools = tools_by_name(
        _server(test_settings, lambda r: httpx.Response(200, json={}))
    )
    missing = {}
    for tool in _paged_tools(tools.values()):
        first = (tool.description or "").split(".")[0].lower()
        absent = [
            word
            for word in (
                "paginated", "returned", "total", "all=true", "complete",
                "every matching row",
            )
            if word not in first
        ]
        if absent:
            missing[tool.name] = absent

    assert missing == {}, f"first line does not state paging: {missing}"


def test_every_paged_tool_says_a_refusal_is_a_stop(test_settings, tools_by_name):
    """The obligation `all=true` creates, which the fence never had.

    A `fetch_all` fence loops until it has everything; it cannot refuse,
    so a caller using one never has to decide what a refusal means.
    `all=true` is complete-or-refuse (D27), so migrating from the fence to
    the tool ADDS a case the caller must handle. The obligation moves on
    migration, it does not vanish — and a model that has not been told
    will read a refusal as a transient failure and retry it, which fails
    identically and looks like the tool being flaky rather than the query
    being too broad.
    """
    tools = tools_by_name(
        _server(test_settings, lambda r: httpx.Response(200, json={}))
    )
    missing = []
    for tool in _paged_tools(tools.values()):
        description = (tool.description or "").lower()
        if "stop" not in description or "retry" not in description:
            missing.append(tool.name)

    assert missing == [], f"a refusal reads as retryable in: {missing}"


def test_every_paged_tool_offers_all(test_settings, tools_by_name):
    tools = tools_by_name(
        _server(test_settings, lambda r: httpx.Response(200, json={}))
    )
    without = [
        t.name
        for t in _paged_tools(tools.values())
        if "all" not in (t.input_schema.get("properties") or {})
    ]

    assert without == [], f"paged but cannot be fetched whole: {without}"


def test_a_filter_survives_every_page_of_an_all_fetch(test_settings, call_tool):
    """The collision between #93 and #90, asserted rather than assumed.

    #93 added `exclude_transfers` to spending_summary and trend while #90
    moved page selection into `list_request`. Resolving that meant deciding
    which arguments are FILTERS (they belong in params and must be sent on
    every request) and which are PAGE SELECTION (they belong to
    list_request and must not be restated).

    Getting it backwards fails silently in the worst way: the first page
    would exclude transfers and later pages would not, so an all=true
    total would be correct for the first 200 rows and wrong after that —
    a number that is right exactly where anyone would spot-check it.
    """
    pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        pages.append(params)
        offset = int(params.get("offset", 0))
        items = [{"key": f"k{n}", "total": -1} for n in range(offset, min(offset + 200, 450))]
        return httpx.Response(200, json={"items": items, "total": 450})

    call_tool(
        _server(test_settings, handler), "spending_summary",
        {"all": True, "exclude_transfers": False},
    )

    assert len(pages) >= 3, f"expected several pages, got {len(pages)}"
    assert all(p.get("exclude_transfers") == "false" for p in pages), (
        f"the filter must travel with every page, got {pages}"
    )
    assert all(p.get("group_by") == "category" for p in pages), "and so must group_by"


# --- all=true is the whole set, never a slice of it (ticket T-63) -------------


@pytest.mark.parametrize(
    "extra, named",
    [
        ({"offset": 2}, "offset=2"),
        ({"limit": 2}, "limit=2"),
        ({"limit": 2, "offset": 2}, "limit=2 and offset=2"),
    ],
)
def test_all_with_a_slice_argument_is_refused_naming_what_was_sent(
    test_settings, call_tool, extra, named
):
    """Both halves of ticket T-63, and they failed in opposite directions.

    `offset` was OBEYED and then held against the caller: the walk read
    every row from that offset, compared what it held to a `total`
    counting all matching rows, and refused itself -- "only 5 of 7 items
    could be read" when 5 IS the complete answer from offset 2. The
    remedy it suggested, "page it with limit and offset", was what the
    caller had just done.

    `limit` was IGNORED: all=true with limit=2 returned 7 rows and said
    nothing, which is the silent-no-op shape this codebase refuses
    elsewhere rather than swallows.

    One refusal for both, because the fault is the same: `all` and a
    slice ask for opposite things, and there is no defensible way to
    pick. It also removes the state in which the completeness check was
    wrong, rather than teaching the check to subtract.
    """
    handler, seen = _pages(7)

    with pytest.raises(ToolError) as excinfo:
        call_tool(
            _server(test_settings, handler),
            "list_merchant_rules",
            {"all": True, **extra},
        )

    message = str(excinfo.value)
    assert named in message, f"the refusal must quote what was sent: {message}"
    assert "all=true" in message
    # THE REMEDY, not just the complaint. review-1 measured the refusal
    # cut down to "...ask for opposite things." still passing: a caller
    # told only that they are wrong, with no way out, has to guess which
    # of the two arguments to drop -- and guessing is what the refusal
    # exists to prevent one layer up.
    assert "all=true on its own" in message, (
        f"the refusal must say how to get everything: {message}"
    )
    assert "limit and offset without all" in message, (
        f"the refusal must say how to page deliberately: {message}"
    )
    assert seen == [], "a refused call must not fetch anything"


def test_all_with_offset_zero_is_not_a_slice_and_still_works(
    test_settings, call_tool
):
    """`offset` defaults to 0, so the guard has to distinguish "did not
    ask" from "asked for 0" -- a truthiness check that treated the
    default as a contradiction would refuse every ordinary all=true."""
    handler, _ = _pages(7)

    result = call_tool(
        _server(test_settings, handler),
        "list_merchant_rules",
        {"all": True, "offset": 0},
    )

    assert result["returned"] == 7
    assert result["total"] == 7


def test_all_starts_its_walk_at_the_beginning(test_settings, call_tool):
    """The walk must start at 0, not at whatever `offset` held.

    Pinned on the REQUEST rather than the result: a walk starting late
    over a set smaller than one page still returns rows, so only the
    offset actually sent distinguishes the two.
    """
    handler, seen = _pages(300)

    call_tool(_server(test_settings, handler), "list_merchant_rules", {"all": True})

    assert seen[0].get("offset", "0") == "0", seen[0]
    assert [page.get("offset", "0") for page in seen] == ["0", "200"], seen


def test_paging_by_limit_and_offset_without_all_is_untouched(
    test_settings, call_tool
):
    """The refusal must not cost the deliberate route it recommends.

    The error tells a caller to use limit and offset without `all`; if
    that had also broken, the message would be advice to a dead end.
    """
    handler, seen = _pages(7)

    result = call_tool(
        _server(test_settings, handler),
        "list_merchant_rules",
        {"limit": 2, "offset": 2},
    )

    assert result["returned"] == 2
    assert result["total"] == 7
    assert seen[0]["limit"] == "2" and seen[0]["offset"] == "2"


#: The clause, exact. Pinned as a constant the way ATTACHMENT_CONTRACT
#: is, so a reworded note is a deliberate edit here rather than a silent
#: one -- and so the per-tool test below compares against one spelling.
EXCLUSIVITY_CLAUSE = (
    "all=true takes no limit or offset \u2014 those ask for one slice and "
    "all=true asks for the whole set, so sending both is refused rather "
    "than one of them being ignored."
)


def test_the_paging_note_carries_the_exclusivity_clause_exactly(test_settings):
    """The constant, pinned word for word.

    An assertion that merely looks for a few words passes a note that has
    been reworded into something weaker, and the note is the only place a
    model learns the rule before making the call.
    """
    from q_core_mcp.paging import PAGING_NOTE

    assert EXCLUSIVITY_CLAUSE in PAGING_NOTE, (
        "PAGING_NOTE no longer carries the clause verbatim:\n" + PAGING_NOTE
    )


def test_every_paged_tool_actually_renders_the_clause(test_settings, tools_by_name):
    """WHAT SHIPS, not what the constant says.

    review-1 on #138: asserting the clause on PAGING_NOTE proves nothing
    about the descriptions. A tool that stopped interpolating the note --
    or interpolated an older copy of it -- passes the constant test and
    ships without the rule. The thing a model reads is the rendered
    description, so that is what this reads, for every paged tool
    discovered rather than listed.
    """
    tools = tools_by_name(
        _server(test_settings, lambda r: httpx.Response(200, json={"items": []}))
    )
    paged = _paged_tools(list(tools.values()))

    missing = sorted(
        tool.name
        for tool in paged
        if EXCLUSIVITY_CLAUSE not in (tool.description or "")
    )
    assert not missing, (
        f"paged tools whose SHIPPED description omits the exclusivity "
        f"clause: {missing}. The constant carrying it is not the same as "
        f"the tool rendering it."
    )
