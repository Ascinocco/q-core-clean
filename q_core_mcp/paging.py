"""One place that knows how a list call is paged.

Every tool taking a `limit` routes through here. Twelve tools each doing
their own offset arithmetic is the same hazard as twelve tools each
restating the API's default: the copies are identical until one of them
is not, and nothing reports the divergence.

The rule this encodes: a caller must never be able to mistake one page
for the whole set. `total` alone did not achieve that — it was present
throughout the N-of-M truncation defect and nothing was obliged to read it.

The answer is NOT a second field to not-check. A single page carries
`items`, `returned` and `total`, and no truncation marker at all: two
earlier versions of this module had one, and both were wrong for the
reason the original bug happened. The obligation goes where it can be
discharged once — `all=True`, which returns the complete set or refuses,
with `allow_truncated` as the only route to a partial one.
"""

from mcp.server.mcpserver.exceptions import ToolError

from api.db import MAX_LIMIT

#: Re-exported: the tool descriptions state the range, and stating it
#: from the constant is what keeps prose and behaviour from drifting.
__all__ = ["MAX_LIMIT", "PAGING_NOTE", "LIST_ITEM_CAP", "list_request"]

#: Hard ceiling on a single `all=True` fetch. Above this the tool refuses
#: rather than returning a partial answer that looks complete, and rather
#: than returning everything: an unbounded result is how one tool response
#: reached 92 KB of context.
LIST_ITEM_CAP = 2000


#: The first line of every paged tool's description. Defined once: twelve
#: copies of one sentence drift exactly the way twelve copies of one
#: default do.
PAGING_NOTE = (
    "Paginated: returned is this page, total is every matching row, and "
    "returned < total means you are holding a page — use all=true, which "
    "returns the complete set or refuses rather than truncating. A refusal "
    "is a STOP, not a retry: the set is too large to read in one call, so "
    "narrow it with a filter or page it deliberately — calling all=true "
    "again fails identically. all=true takes no limit or offset — those "
    "ask for one slice and all=true asks for the whole set, so sending "
    "both is refused rather than one of them being ignored."
)


def _with_counts(payload: dict) -> dict:
    """Add `returned`. Deliberately no truncation marker.

    Two earlier versions of this carried one — first an always-present
    boolean, then present-only-when-true — and both were wrong for the
    same reason, which is the reason the original bug happened: `total`
    was in every one of those N-of-M responses and nobody compared it.
    A second field to not-check has a poor prior. The obligation belongs
    where it can be discharged once — `all=true` — not distributed to
    every caller that ever lists anything.

    So a single page states the two facts and nothing else: `returned` is
    what you are holding, `total` is what exists. If they differ you have
    a page.
    """
    if not isinstance(payload, dict) or "items" not in payload:
        return payload
    payload["returned"] = len(payload["items"])
    return payload


async def list_request(
    client,
    path: str,
    *,
    params: dict | None = None,
    limit: int | None = None,
    offset: int = 0,
    fetch_all: bool = False,
    allow_truncated: bool = False,
    metadata_keys: tuple[str, ...] = (),
) -> dict:
    """One page, or every page when `fetch_all` — never a slice of every.

    `limit` is sent only when the caller supplied one, so the default
    lives in the API and nowhere else.

    `fetch_all` is exclusive with both `limit` and `offset`; see the
    refusal below for why that is the fix and not merely a rule.
    """
    base = {key: value for key, value in (params or {}).items() if value is not None}

    if not fetch_all:
        query = dict(base)
        if limit is not None:
            query["limit"] = limit
        if offset:
            query["offset"] = offset
        return _with_counts(await client.request("GET", path, params=query))

    # `all` and a slice are contradictory requests, and this refuses
    # rather than picking one -- the same call apply_clear makes, for the
    # same reason: both readings are defensible, so guessing is a silent
    # wrong answer.
    #
    # It is also what FIXES the completeness check rather than patching
    # it. `total` counts every matching row while `items` held only those
    # from `offset` on, so the comparison below was wrong by exactly
    # `offset` and `all=true` with any offset refused itself:
    # "only 5 of 7 items could be read" for a query whose complete answer
    # from offset 2 IS those 5, suggesting the caller "page it with limit
    # and offset" -- which is what they had just done. Rather than teach
    # the comparison about offset, this removes the state in which the
    # two disagree, so below `cursor` starts at 0 always and
    # `len(items) < total` means what it says again.
    #
    # `limit` was worse than wrong, it was ignored: all=true with
    # limit=2 returned 7 rows and said nothing.
    contradictions = []
    if limit is not None:
        contradictions.append(f"limit={limit}")
    if offset:
        contradictions.append(f"offset={offset}")
    if contradictions:
        raise ToolError(
            f"all=true was sent with {' and '.join(contradictions)}, which "
            "ask for opposite things: all=true returns the COMPLETE set "
            "and a limit or offset asks for one slice of it. Send one or "
            "the other — all=true on its own for everything, or limit and "
            "offset without all to page through it deliberately."
        )

    items: list = []
    # 0 rather than `offset`, and the two are EQUIVALENT here -- the
    # guard above has already refused every nonzero offset, so writing
    # `cursor = offset` passes the whole suite (verified; it is an
    # equivalent mutant, not a hole). Written as 0 anyway so the walk's
    # start does not read as configurable, and so that relaxing the
    # guard cannot quietly restore the comparison bug it was refusing.
    cursor = 0
    total: int | None = None
    metadata: dict | None = None

    while True:
        page = await client.request(
            "GET", path, params={**base, "limit": MAX_LIMIT, "offset": cursor}
        )
        if metadata_keys:
            current_metadata = {key: page[key] for key in metadata_keys}
            if metadata is not None and current_metadata != metadata:
                raise ToolError("source_changed: paginated source metadata changed; no mixed result returned")
            metadata = current_metadata
        batch = page.get("items", [])
        if page.get("total") is not None:
            total = page["total"]

        # Checked on the first page, before accumulating: refusing after
        # reading everything would incur the cost the cap exists to avoid.
        if isinstance(total, int) and total > LIST_ITEM_CAP and not allow_truncated:
            # Withholds the rows, exactly as partial_extraction does (D26).
            # Returning 2000 of 3000 while calling it truncated would be
            # handing the thing over while calling it a refusal — and
            # `all=true` asserts completeness in its own name, so the
            # caller has more reason to stop checking, not less.
            raise ToolError(
                f"truncated_result: {total} items exceeds the all=true cap of "
                f"{LIST_ITEM_CAP}, so no rows are returned. Narrow the query "
                "with a filter, page it with limit and offset, or pass "
                "allow_truncated=true to accept the first "
                f"{LIST_ITEM_CAP}."
            )

        items.extend(batch)

        # Two independent stops. `len(items) >= total` is the ordinary one;
        # the empty page is what bounds a server that ignores `offset`, or
        # a `total` that overstates what will actually be served. Either
        # alone leaves a way to loop for ever.
        if not batch:
            break
        if isinstance(total, int) and len(items) >= total:
            break
        if len(items) >= LIST_ITEM_CAP:
            # Reached only with allow_truncated, or when `total` understated
            # what the endpoint would serve. Either way, stop at the cap.
            break
        cursor += len(batch)

    # No `truncated` key at all: all=true is complete-or-refuse, so a
    # result that exists is by construction the whole set. A flag saying
    # "complete" would be the marker this design exists to avoid, and
    # `all=true` already asserts completeness in its own name.
    if isinstance(total, int) and len(items) < total:
        # An earlier version commented that this was "only reachable via
        # allow_truncated" and did not check. It was not: a server that
        # stops early, or a `total` that overstates what it will serve,
        # lands here too — and returned a partial set with a marker and no
        # opt-in, which is the one shape D27 forbids. The comment asserting
        # the property was not the guard enforcing it.
        if not allow_truncated:
            raise ToolError(
                f"truncated_result: only {len(items)} of {total} items could "
                "be read, so none are returned. Page it with limit and "
                "offset, or pass allow_truncated=true to accept a partial "
                "set."
            )
        # Reached only by explicit opt-in, so the flag is the caller's own
        # recorded acceptance rather than a warning to remember.
        return {
            **(metadata or {}),
            "items": items[:LIST_ITEM_CAP],
            "total": total,
            "returned": len(items[:LIST_ITEM_CAP]),
            "truncated": True,
        }

    # No marker: all=true is complete-or-refuse, so a result that exists is
    # by construction the whole set.
    return {
        **(metadata or {}),
        "items": items,
        "total": total if total is not None else len(items),
        "returned": len(items),
    }
