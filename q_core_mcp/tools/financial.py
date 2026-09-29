"""Tools over api/financial.py: statements, transactions, rules, reports.

Several of these wrap endpoints whose behaviour is correct but surprising,
where a model that has not been told will produce a confidently wrong
answer rather than an error. Those warnings live in the tool descriptions
and are pinned by tests.
"""

from api.financial import (
    LIST_TRANSACTIONS_ORDER,
    MATCHER_CONTRACT,
    NULL_BUCKET_CONTRACT,
    RULE_HISTORY_ORDER,
    TRANSFERS_CONTRACT,
)
from q_core_mcp.paging import PAGING_NOTE, list_request
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from q_core_mcp.clearing import CLEAR_NOTE, apply_clear
from q_core_mcp.annotations import (
    ADDITIVE,
    ADDITIVE_IDEMPOTENT,
    DESTRUCTIVE,
    READ_ONLY,
)
from q_core_mcp.client import QCoreClient



def register_financial_tools(server: MCPServer, client: QCoreClient) -> None:
    @server.tool(annotations=DESTRUCTIVE, description=(
        'Correct only an active statement period using source evidence. Requires the inspected old dates, '
        'new dates, actor, reason and a UUID correction_id. Keeps statement/transaction IDs and financial '
        'facts unchanged. Refuses stale state or duplicate periods. Same correction_id and identical request '
        'replays the original audit result without another write; it is not a fresh state read.'))
    async def correct_statement_period(statement_id: str, expected_period_start: str, expected_period_end: str,
                                       period_start: str, period_end: str, actor: str, reason: str,
                                       correction_id: str) -> dict:
        return await client.request('POST', f'/statements/{statement_id}/correct-period', json={
            'expected_period_start': expected_period_start, 'expected_period_end': expected_period_end,
            'period_start': period_start, 'period_end': period_end, 'actor': actor,
            'reason': reason, 'correction_id': correction_id})

    @server.tool(annotations=DESTRUCTIVE, description=(
        'Move one active transaction to an existing active statement on the SAME account, using source '
        'evidence. Requires inspected expected_statement_id, destination statement_id, actor, reason and UUID '
        'correction_id. Preserves transaction ID, date, description, amount, category, entity and edit marker. '
        'Dates may be outside the statement period when source table evidence supports membership. '
        'Refuses stale state or archived parents. Identical retries with the same correction_id replay the '
        'original audit, not a fresh read. No reimport or reclassification.'))
    async def reassign_transaction_statement(transaction_id: str, expected_statement_id: str,
                                             statement_id: str, actor: str, reason: str,
                                             correction_id: str) -> dict:
        return await client.request('POST', f'/transactions/{transaction_id}/reassign-statement', json={
            'expected_statement_id': expected_statement_id, 'statement_id': statement_id,
            'actor': actor, 'reason': reason, 'correction_id': correction_id})

    @server.tool(annotations=READ_ONLY, description=(
        'Read the immutable audit record of a financial metadata correction by its UUID. '
        'Shows actor, reason, target, timestamp and before/after values; not current ledger state.'))
    async def get_financial_correction(correction_id: str) -> dict:
        return await client.request('GET', f'/financial-corrections/{correction_id}')

    @server.tool(
        annotations=READ_ONLY,
        description=(
            f"{PAGING_NOTE} "
            "List spending categories. The taxonomy is a fixed two-level "
            "tree installed at setup — pass parent_id to list one parent's "
            "children, or omit it for all of them. Use this to find the "
            "category_id needed when correcting a transaction. Categories "
            "cannot be created or edited through these tools, by design."
        ),
    )
    async def list_categories(
        parent_id: str | None = None, limit: int | None = None, offset: int = 0, all: bool = False,
        allow_truncated: bool = False
    ) -> dict:
        params: dict = {}
        if parent_id is not None:
            params["parent_id"] = parent_id
        return await list_request(
            client, "/categories", params=params,
            limit=limit, offset=offset, fetch_all=all,
            allow_truncated=allow_truncated,
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            f"{PAGING_NOTE} "
            "List imported bank and credit-card statements, newest period "
            "first. Pass account_id to limit it to one account. Use this to "
            "check whether a period has already been imported before "
            "importing it again."
        ),
    )
    async def list_statements(
        account_id: str | None = None, limit: int | None = None, offset: int = 0, all: bool = False,
        allow_truncated: bool = False
    ) -> dict:
        params: dict = {}
        if account_id is not None:
            params["account_id"] = account_id
        return await list_request(
            client, "/statements", params=params,
            limit=limit, offset=offset, fetch_all=all,
            allow_truncated=allow_truncated,
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            f"{PAGING_NOTE} "
            f"List transactions, {LIST_TRANSACTIONS_ORDER}. Filters, all optional and "
            "combinable: account_id, entity_id (what the spending was for), "
            "category_id, and a date_from/date_to range as YYYY-MM-DD. Use "
            "this to see the transactions behind a total, or to review what "
            "an import left uncategorized."
        ),
    )
    async def list_transactions(
        account_id: str | None = None,
        entity_id: str | None = None,
        category_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        all: bool = False,
        allow_truncated: bool = False,
    ) -> dict:
        params: dict = {}
        for name, value in (
            ("account_id", account_id),
            ("entity_id", entity_id),
            ("category_id", category_id),
            ("date_from", date_from),
            ("date_to", date_to),
        ):
            if value is not None:
                params[name] = value
        return await list_request(
            client, "/transactions", params=params,
            limit=limit, offset=offset, fetch_all=all,
            allow_truncated=allow_truncated,
        )

    @server.tool(
        annotations=DESTRUCTIVE,
        description=(
            "Correct which category or entity a transaction is booked "
            "against. This is correction-only: the date, description and "
            "amount come from the imported statement and CANNOT be changed "
            "here, deliberately, so the record keeps matching the document "
            "it came from. Pass category_id, entity_id or both; anything "
            "omitted is left as it is. To make the same correction apply to "
            "future imports of this merchant, follow up with "
            "apply_transaction_as_rule on the same transaction.\n\n"
            f"{CLEAR_NOTE} Clearing a classification records a DECISION "
            "that this transaction belongs to no category or entity, and "
            "reapply_merchant_rules will not overwrite it."
        ),
    )
    async def update_transaction(
        transaction_id: str,
        category_id: str | None = None,
        entity_id: str | None = None,
        clear: list[str] | None = None,
        # Accepted ONLY to be refused. The SDK drops unknown arguments
        # before the tool body runs, so leaving these out of the signature
        # does not make the attempt impossible — it makes it invisible:
        # update_transaction(amount_cents=5) would send an empty PATCH and
        # return 200, reporting success for an operation the API refuses.
        # Naming them here turns a silent no-op into a loud refusal.
        txn_date: str | None = None,
        description: str | None = None,
        amount_cents: int | None = None,
    ) -> dict:
        refused = {
            "txn_date": txn_date,
            "description": description,
            "amount_cents": amount_cents,
        }
        attempted = {name: value for name, value in refused.items() if value is not None}
        if attempted:
            fields = ", ".join(sorted(attempted))
            values = ", ".join(f"{name}={value!r}" for name, value in sorted(attempted.items()))
            # ToolError, not ApiError: nothing was sent, so there is no API
            # call to have failed. ApiError's docstring opens "An API call
            # failed", which would send a reader hunting for an HTTP
            # response that never existed. Matches the convention in
            # tools/jyra.py.
            raise ToolError(
                f"update_transaction cannot change {fields} — those come "
                "from the imported statement and are deliberately immutable, "
                "so the record keeps matching the document it came from. "
                f"Nothing was changed (attempted: {values}). If the imported "
                "data itself is wrong, use the audited correction tools or "
                "preview_source_import for a reviewed new source; this tool "
                "only corrects category_id "
                "and entity_id."
            )

        body: dict = {}
        if category_id is not None:
            body["category_id"] = category_id
        if entity_id is not None:
            body["entity_id"] = entity_id
        # Clearing a classification is a DECISION -- "this belongs to
        # nothing" -- not an absence, and reapply_merchant_rules skips
        # edited rows precisely so it cannot overwrite one.
        apply_clear(body, clear, "update_transaction")
        return await client.request(
            "PATCH", f"/transactions/{transaction_id}", json=body
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            f"{PAGING_NOTE} "
            f"Every recorded change to ONE merchant rule, {RULE_HISTORY_ORDER}: "
            "who changed it, when, which field, and from what to what. A "
            "row with no field is the rule's creation or its deletion. "
            "Readable after the rule is deleted, which is exactly when "
            "you want it. Use this to answer 'why does this transaction "
            "categorize the way it does' when a rule looks wrong."
        ),
    )
    async def merchant_rule_history(
        rule_id: str,
        limit: int | None = None,
        offset: int = 0,
        all: bool = False,
        allow_truncated: bool = False,
    ) -> dict:
        return await list_request(
            client, f"/merchant_rules/{rule_id}/history",
            limit=limit, offset=offset, fetch_all=all,
            allow_truncated=allow_truncated,
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            f"{PAGING_NOTE} "
            "List the merchant rules that auto-categorize imported "
            "transactions. Each maps a description pattern to a category, "
            "and optionally to an entity. When more than one pattern "
            f"matches a transaction: {MATCHER_CONTRACT}."
        ),
    )
    async def list_merchant_rules(limit: int | None = None, offset: int = 0, all: bool = False,
        allow_truncated: bool = False) -> dict:
        return await list_request(
            client, "/merchant_rules",
            limit=limit, offset=offset, fetch_all=all,
            allow_truncated=allow_truncated,
        )

    @server.tool(
        annotations=ADDITIVE,
        description=(
            "Create a merchant rule from a pattern you choose, so future "
            "imports whose description CONTAINS that pattern are "
            "categorized automatically. Use a generalized pattern — "
            "'MAPLE DONUTS', not 'MAPLE DONUTS #4821 ANYTOWN XY' — because "
            "bank descriptions carry store numbers, locations and "
            "transaction ids that differ every time.\n\n"
            f"Broad patterns are safe here: matching is {MATCHER_CONTRACT}, "
            "so a more specific rule added later takes over for that "
            "merchant without editing or breaking this one. See "
            "runbooks/merchant-rules-conventions.md.\n\n"
            "This is the broad path. apply_transaction_as_rule is the "
            "narrow one — it learns an existing transaction's EXACT "
            "description, which is right for 'always categorize precisely "
            "this' and wrong for a merchant in general. "
            "At least one of category_id or entity_id is required: a rule "
            "that sets neither classifies nothing and is refused."
            "\n\nPass actor: who is making this change, recorded in "
            "the rule's audit trail. A merchant rule decides how spending "
            "is classified, so an unrecorded correction is "
            "indistinguishable from the rule always having said that. Use "
            "your own agent name."
        ),
    )
    async def create_merchant_rule(
        pattern: str,
        actor: str,
        category_id: str | None = None,
        entity_id: str | None = None,
    ) -> dict:
        body: dict = {"pattern": pattern, "actor": actor}
        # Omitted rather than sent as null so the request body pins only
        # what the caller actually set, which is what makes the recorded-body
        # test meaningful and matches the other write tools.
        #
        # Not because the API distinguishes them: it does not. Both fields
        # are `str | None = None`, so null and absent take the same branch
        # everywhere, including the at-least-one check — measured, after an
        # earlier version of this comment claimed otherwise.
        if category_id is not None:
            body["category_id"] = category_id
        if entity_id is not None:
            body["entity_id"] = entity_id
        return await client.request("POST", "/merchant_rules", json=body)

    @server.tool(
        # DESTRUCTIVE even though dry_run defaults to true. The annotation is
        # static and the behaviour is conditional, so it has to describe the
        # worst thing the tool can do rather than its default. Labelling it
        # by the safe default would put the honest label on the call that
        # writes.
        annotations=DESTRUCTIVE,
        description=(
            "Apply the current merchant rules to transactions that were "
            "never classified. Rules run at import time only, so adding or "
            "correcting a rule changes nothing already stored — 'we added "
            "the rules' and 'the totals reflect them' are different claims "
            "until this runs.\n\n"
            "DRY RUN BY DEFAULT. It reports what it would change and writes "
            "nothing. Read the result back — row count, cents per category — "
            "and call again with dry_run=false only once the user has seen "
            "it.\n\n"
            "It NEVER OVERWRITES. Only rows with no category and no entity "
            "are touched, so an existing classification — from an earlier "
            "import or from a person — is left alone. A consequence worth "
            "stating plainly: correcting a WRONG rule does NOT retroactively "
            "fix the rows it already miscategorized. Those keep the wrong "
            "category and need a separate decision.\n\n"
            "There is no undo. The response lists every id it changed, and "
            "that list is the only record of what moved."
        ),
    )
    async def reapply_merchant_rules(
        dry_run: bool = True,
        statement_id: str | None = None,
        entity_id: str | None = None,
    ) -> dict:
        body: dict = {"dry_run": dry_run}
        if statement_id is not None:
            body["statement_id"] = statement_id
        if entity_id is not None:
            body["entity_id"] = entity_id
        return await client.request("POST", "/merchant_rules/reapply", json=body)

    @server.tool(
        annotations=DESTRUCTIVE,
        description=(
            "Correct the classification of an existing merchant rule — the "
            "category or entity it assigns. Use this when a rule is simply "
            "wrong: it matches the right merchant but files it under the "
            "wrong category.\n\n"
            "THERE IS DELIBERATELY NO `pattern` PARAMETER. A rule's pattern "
            f"decides precedence ({MATCHER_CONTRACT}), so editing patterns "
            "is how precedence gets fought by "
            "hand, one rule at a time, until nothing is predictable. If the "
            "pattern is wrong, create a new more specific rule instead: the "
            "longer pattern takes over by itself without touching anything "
            "else.\n\n"
            "This edits the rule in place rather than replacing it, and that "
            "is deliberate. Deleting and re-creating would assign a new id, "
            "and since ties between equal-length patterns are broken by "
            "lowest id, a new id can silently change which OTHER rule wins a "
            "tie. Editing in place preserves the rule's position.\n\n"
            "At least one of category_id or entity_id is required; an update "
            "that sets neither would report success while changing nothing "
            "— naming one in clear counts.\n\n"
            f"{CLEAR_NOTE}\n\n"
            "Correcting a rule changes how FUTURE imports categorize. It does "
            "not recategorize transactions already imported — their category "
            "was written at import time. Say so rather than implying the past "
            "is fixed."
            "\n\nPass actor: who is making this change, recorded in "
            "the rule's audit trail. A merchant rule decides how spending "
            "is classified, so an unrecorded correction is "
            "indistinguishable from the rule always having said that. Use "
            "your own agent name."
        ),
    )
    async def update_merchant_rule(
        rule_id: str,
        actor: str,
        category_id: str | None = None,
        entity_id: str | None = None,
        clear: list[str] | None = None,
    ) -> dict:
        # `and not clear` matters: this is the same trap impl-3 hit in the
        # API model. "Neither is set" is exactly what clearing looks like,
        # so without it clear=["category_id"] is refused here as an empty
        # update -- and it reads as the clear feature not working rather
        # than as this guard misfiring.
        if category_id is None and entity_id is None and not clear:
            raise ToolError(
                "at least one of category_id or entity_id is required — an "
                "update that sets neither would report success while changing "
                "nothing. To unset one, name it in clear."
            )
        body: dict = {"actor": actor}
        # Omitted rather than sent as null, matching create_merchant_rule:
        # the recorded body then pins only what the caller actually set.
        if category_id is not None:
            body["category_id"] = category_id
        if entity_id is not None:
            body["entity_id"] = entity_id
        apply_clear(body, clear, "update_merchant_rule")
        return await client.request("PATCH", f"/merchant_rules/{rule_id}", json=body)

    @server.tool(
        annotations=ADDITIVE,
        description=(
            "Learn a categorization rule from one transaction you have "
            "already corrected, so matching transactions are categorized "
            "automatically on future imports. Call this AFTER "
            "update_transaction has set the right category or entity on "
            "that transaction — applying an uncorrected one is refused, "
            "since a rule that classifies nothing is worse than none.\n\n"
            "It learns that transaction's EXACT description as the "
            f"pattern, and matching is {MATCHER_CONTRACT}. So it covers "
            "future "
            "transactions whose description contains that whole string — "
            "not the merchant in general. A rule learned from "
            "'ACME FUEL 41' will not match 'ACME FUEL 99' at a different "
            "store number or location. Tell the user what was actually "
            "learned (the specific description), not that the merchant is "
            "now handled, and expect other branches of the same chain to "
            "come back unmatched and need their own rule.\n\n"
            "It creates a new rule rather than editing an existing one. "
            "The longer pattern takes precedence by itself, and nothing "
            "already in place is modified.\n\n"
            "If a rule for that exact description already exists, this "
            "reports a conflict and changes nothing — which usually means "
            "the merchant is already handled and no action is needed. "
            "If that existing rule is WRONG rather than redundant, use "
            "update_merchant_rule to correct its category or entity; do "
            "not retry this call."
            "\n\nPass actor: who is making this change, recorded in "
            "the rule's audit trail. A merchant rule decides how spending "
            "is classified, so an unrecorded correction is "
            "indistinguishable from the rule always having said that. Use "
            "your own agent name."
        ),
    )
    async def apply_transaction_as_rule(transaction_id: str, actor: str) -> dict:
        return await client.request(
            "POST", f"/transactions/{transaction_id}/apply_as_rule",
            json={"actor": actor},
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Check whether an account's imported statements cover a "
            "continuous stretch of time, or whether one is missing. Run "
            "it after every import.\n\n"
            "A GAP means a statement period nobody has imported — money "
            "moved in that window and q-core has no record of it, so "
            "every total silently understates. Treat a gap as a STOP: "
            "report it and ask, do not carry on as though the import "
            "were complete.\n\n"
            "An OVERLAP means two statements cover the same days. That "
            "is a re-export and is expected for sources whose export "
            "windows overlap; it is reported, not a problem, because "
            "de-duplication already keeps the shared rows from being "
            "counted twice.\n\n"
            "THIS CANNOT DETECT A MISSING PAGE, and no check on the "
            "import side can. A statement that imported without its "
            "second page looks whole: its rows are there, its period is "
            "intact, and it sits flush against its neighbours. Storing "
            "opening and closing balances would not help — a balance "
            "chain reconciles within the pages you have, so the run "
            "that lost the page read the closing balance off a page it "
            "did have, and the check would pass on exactly the import "
            "it was built to catch. Do not propose it; page-level "
            "assurance has to come from the document, not from here.\n\n"
            "account_id scopes it to one account; omit it for all. "
            'Returns {"statements", "accounts", "gaps", "overlaps"} — '
            "gaps and overlaps, EACH IN ITS OWN SHAPE, so neither can be "
            "read as the other. ONE CONVENTION ON BOTH LISTS: the named "
            "range IS what days counts, so gap_start..gap_end inclusive "
            "is exactly days, and overlap_start..overlap_end inclusive is "
            "exactly days. A gap's bounds are the first and last MISSING "
            "day and days = days missing; an overlap's are the first and "
            "last day covered TWICE and days = days doubled. Both counts "
            "are positive; there are no gap_* fields on an overlap. "
            "Report a gap as missing coverage and an overlap as a "
            "re-export, and never describe an overlap as a gap — a gap is "
            "the one that needs action."
        ),
    )
    async def statement_coverage(account_id: str | None = None) -> dict:
        params: dict = {}
        if account_id is not None:
            params["account_id"] = account_id
        return await client.request("GET", "/statements/coverage", params=params)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Spend by calendar month or Monday–Sunday week for an inclusive "
            "date range of at most 366 days. This is the same report that "
            "drives the Spend Cadence page. Returns integer cents, top-level "
            "category groups, coverage warnings, transfers in/out, income, "
            "and a merchant breakdown for each group. Spending is negative "
            "active transactions; transfers and income are excluded, while "
            "uncategorized debits count as spending. Merchant labels are "
            "trimmed statement descriptors, not a merchant registry."
        ),
    )
    async def spending_periods(
        granularity: str,
        date_from: str,
        date_to: str,
        account_id: str | None = None,
    ) -> dict:
        params: dict = {
            "granularity": granularity,
            "date_from": date_from,
            "date_to": date_to,
        }
        if account_id is not None:
            params["account_id"] = account_id
        return await client.request("GET", "/spending/periods", params=params)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            f"{PAGING_NOTE} "
            "Total spending for a period, grouped by category or by entity "
            "— the 'where did it all go' report. period is 'YYYY' or "
            "'YYYY-MM'; omit it for all time. group_by is 'category' or "
            "'entity'.\n\n"
            "Totals are SIGNED sums, not debit-only: money in and money "
            "out are added together. Report them as net totals per group "
            "rather than as raw amounts spent. All totals are in integer "
            "CENTS — divide by 100 before showing dollars.\n\n"
            f"MONEY MOVED BETWEEN THE OWNER'S OWN ACCOUNTS: "
            f"{TRANSFERS_CONTRACT} (exclude_transfers). A transfer is not "
            "spending, and it appears twice in a cross-account read — "
            "once leaving, once arriving — so including it reports a "
            "purchase that never happened. The response carries "
            "transfers_excluded_cents and transfers_excluded_count: the "
            "CENTS are often 0 because two legs cancel, so use the COUNT "
            "to tell 'no transfers' from 'transfers removed'. Pass "
            "exclude_transfers=false only when asked about the movements "
            "themselves.\n\n"
            "ALSO CHECK THE NULL FIGURE BEFORE CALLING ANY TOTAL "
            "COMPLETE. Excluding transfers does not mean the rest is "
            "spending: many transfers are not yet categorized as "
            "transfers and are sitting in the uncategorized bucket "
            "looking like purchases. If uncategorized_cents is large "
            "relative to the total, say so rather than reporting the "
            "total as the answer.\n\n"
            "Rows with no value for whatever is being grouped are "
            "returned under a key of null. That is NOT a category (or "
            "entity) named 'None' — it means NOT GROUPED, and what that "
            "means depends on group_by: no category, or no entity. Never "
            "drop it; the totals stop adding up if you do, and a large "
            "null group usually means an import still has unmatched "
            "transactions.\n\n"
            "The response carries the same figure by name, and WHICH name "
            "tells you which null you are looking at: uncategorized_cents "
            "with group_by=category, unattributed_cents with "
            f"group_by=entity: {NULL_BUCKET_CONTRACT}. Use it rather than "
            "hunting for the null row.\n\n"
            "For one category or one thing over time, use trend. For "
            "everything one asset has cost, use cost_of_ownership."
        ),
    )
    async def spending_summary(
        period: str | None = None,
        group_by: str = "category",
        exclude_transfers: bool = True,
        limit: int | None = None,
        offset: int = 0,
        all: bool = False,
        allow_truncated: bool = False,
    ) -> dict:
        # exclude_transfers is a FILTER and belongs in params, so it is sent
        # on every page of an all=true fetch. limit and offset are not
        # filters -- they are how the page is chosen, which is
        # list_request's job now, and leaving them here would restate the
        # API's default in the one place this change removes it from.
        params: dict = {"group_by": group_by, "exclude_transfers": exclude_transfers}
        if period is not None:
            params["period"] = period
        return await list_request(
            client, "/spending_summary", params=params,
            limit=limit, offset=offset, fetch_all=all,
            allow_truncated=allow_truncated,
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            f"{PAGING_NOTE} "
            "Month-by-month totals for ONE category or ONE entity over the "
            "last `months` months (1-60, default 6) — the 'is this going "
            "up or down' report. Totals are in integer CENTS.\n\n"
            "You get exactly `months` buckets, on calendar month "
            "boundaries. THE LATEST BUCKET IS MONTH-TO-DATE — the current "
            "month is only as complete as today, so it is normally lower "
            "than the ones before it and that is not a downward trend. "
            "Every earlier bucket is a whole month.\n\n"
            "Pass EXACTLY ONE of category_id or entity_id. Passing both, "
            "or neither, is an error rather than a default.\n\n"
            "Transfers between the owner's own accounts are excluded by "
            "default (exclude_transfers, default true), for the same "
            "reason as in spending_summary. ONE EXCEPTION, applied "
            "automatically: asking for a transfers category BY ID returns "
            "that series anyway, because an empty series would be "
            "indistinguishable from 'you made no payments' — you do not "
            "need to pass exclude_transfers=false to ask about credit "
            "card payments. The response carries "
            "transfers_excluded_cents and transfers_excluded_count.\n\n"
            "For a single period broken down across everything, use "
            "spending_summary instead."
        ),
    )
    async def trend(
        category_id: str | None = None,
        entity_id: str | None = None,
        months: int = 6,
        exclude_transfers: bool = True,
        limit: int | None = None,
        offset: int = 0,
        all: bool = False,
        allow_truncated: bool = False,
    ) -> dict:
        # See spending_summary: the filter travels, the page selection does not.
        params: dict = {"months": months, "exclude_transfers": exclude_transfers}
        if category_id is not None:
            params["category_id"] = category_id
        if entity_id is not None:
            params["entity_id"] = entity_id
        return await list_request(
            client, "/trend", params=params,
            limit=limit, offset=offset, fetch_all=all,
            allow_truncated=allow_truncated,
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "What one thing has cost: every transaction booked against "
            "this entity, plus those booked against any account that "
            "finances or insures it. period is 'YYYY' or 'YYYY-MM'; omit "
            "for all time. This is the tool for 'how much has the Toyota "
            "cost me this year'. Totals are in integer CENTS.\n\n"
            "Attribution is whole-cost, not pro-rata: a single policy "
            "covering two cars contributes its FULL premium to each of "
            "them. Each number is correct on its own, but they must NOT be "
            "added together across entities — that would double-count "
            "every shared policy or loan.\n\n"
            "CHECK shared_cost_entities BEFORE ADDING TWO OF THESE. It is "
            "the number of OTHER entities whose costs overlap this one, "
            "and shared_with names them, which account they are shared "
            "through, and whether that link has ended. Zero means the "
            "figure is safe to add to another zero. Anything above zero "
            "means the two responses contain the same money and summing "
            "them reports a charge that never happened. An ended link "
            "still counts: the total does not exclude a lapsed policy's "
            "premiums, so they still overlap.\n\n"
            "To total across several things, "
            "use spending_summary grouped by entity instead."
        ),
    )
    async def cost_of_ownership(entity_id: str, period: str | None = None) -> dict:
        params: dict = {}
        if period is not None:
            params["period"] = period
        return await client.request(
            "GET", f"/entities/{entity_id}/cost_of_ownership", params=params
        )


    @server.tool(
        annotations=DESTRUCTIVE,
        description=(
            "Archive a whole imported statement and every transaction in "
            "it, for a mis-imported or duplicated statement. "
            + 'Archived rows leave every total -- spending_summary, trend and cost_of_ownership all stop counting them -- and stop blocking de-duplication, so the corrected file can be imported straight after. Nothing is deleted: the rows stay on disk and stay auditable. Reversible with ' + "unarchive_statement.\n\n"
            "Returns hand_edited and hand_edited_ids: how many of those "
            "rows -- and WHICH -- had their category or entity link "
            "changed BY HAND after import, as opposed to by a merchant "
            "rule during it. Re-importing the corrected file does NOT "
            "carry that work over and does not warn you again: the rows "
            "simply come back uncategorized. If the count is not zero, "
            "say so before re-importing, and list the rows so the person "
            "can decide -- a count alone tells them something was lost "
            "without telling them what."
        ),
    )
    async def archive_statement(statement_id: str) -> dict:
        return await client.request(
            "POST", f"/statements/{statement_id}/archive"
        )

    @server.tool(
        annotations=ADDITIVE_IDEMPOTENT,
        description=(
            "Put an archived statement and its transactions back into "
            "every total. The counterpart to archive_statement, for an "
            "archive made in error."
        ),
    )
    async def unarchive_statement(statement_id: str) -> dict:
        return await client.request(
            "POST", f"/statements/{statement_id}/unarchive"
        )

    @server.tool(
        annotations=DESTRUCTIVE,
        description=(
            "Archive ONE transaction, for a duplicate or a mis-parsed "
            "line, leaving the rest of its statement alone. "
            + 'Archived rows leave every total -- spending_summary, trend and cost_of_ownership all stop counting them -- and stop blocking de-duplication, so the corrected file can be imported straight after. Nothing is deleted: the rows stay on disk and stay auditable. Reversible with ' + "unarchive_transaction."
        ),
    )
    async def archive_transaction(transaction_id: str) -> dict:
        return await client.request(
            "POST", f"/transactions/{transaction_id}/archive"
        )

    @server.tool(
        annotations=ADDITIVE_IDEMPOTENT,
        description=(
            "Put one archived transaction back into every total."
        ),
    )
    async def unarchive_transaction(transaction_id: str) -> dict:
        return await client.request(
            "POST", f"/transactions/{transaction_id}/unarchive"
        )
