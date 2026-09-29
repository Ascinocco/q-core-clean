"""Source-tracked import tools; all persistence remains in the API."""
from mcp.server import MCPServer
from api.models import AMOUNT_CENTS_CONTRACT
from q_core_mcp.client import QCoreClient
from q_core_mcp.annotations import READ_ONLY, ADDITIVE


def register_source_import_tools(server: MCPServer, client: QCoreClient):
    @server.tool(annotations=READ_ONLY, description=(
        'Preview a source-tracked statement import without writes. payload has account_id, period_start, period_end, '
        'source_id (SHA-256 of original file bytes), actor, and transactions containing txn_date, description, '
        f'amount_cents ({AMOUNT_CENTS_CONTRACT}; negative for money out, positive for money in), '
        'source_row (stable physical locator such as csv:v1:record:17 or pdf:2:row:4). '
        'An exact scrubbed-text span can instead use text-v1:<text_sha256>:<start>:<end>, binding offsets to the extraction returned by the server. '
        'Use only approved server-scrubbed text; never open raw source content to construct locators. '
        'Never invent bank IDs or use extraction output order as source identity. If reliable locators are unavailable, stop. '
        'Returns a review_token and all candidate rows: same account and cents within 3 days of the row date, '
        'exact dates first (each row has match exact|near_date, each candidate days_apart). Neither a same or '
        'nearby date nor an exact description proves cross-source identity. needs_review requires operator '
        'evidence for new/link; source_conflict is a stop.'))
    async def preview_source_import(payload: dict) -> dict:
        return await client.request('POST', '/source_imports/preview', json=payload)

    @server.tool(annotations=ADDITIVE, description=(
        'Commit the exact source payload inspected by preview_source_import, with its review_token. '
        'For each ambiguous row, add decision="new" or decision="link", a review reason, and '
        'transaction_id only for link. Do not guess resolutions or infer them from matching money/merchant alone. '
        'New means a genuinely separate purchase; link attaches source evidence to an existing row without changing it. '
        'Whole batch is atomic; stale tokens and unresolved collisions refuse all writes. '
        'After timeout or retry, preview again: recorded source occurrences are recognized without reinsertion. '
        'Caller-supplied source hash/locators must be trustworthy: these are not bank transaction IDs. '
        'Read back returned transaction IDs with list_transactions (drain all pages); category_id=null means unmatched. '
        'Resolve unmatched rows with approved update_transaction decisions; propose merchant rules, '
        'using apply_transaction_as_rule only when an exact-description rule is intended.'))
    async def commit_source_import(payload: dict) -> dict:
        return await client.request('POST', '/source_imports/commit', json=payload)
