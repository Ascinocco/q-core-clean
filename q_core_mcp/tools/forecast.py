from mcp.server import MCPServer
from q_core_mcp.client import QCoreClient
from q_core_mcp.annotations import READ_ONLY, DESTRUCTIVE


def register_forecast_tools(server: MCPServer, client: QCoreClient):
    @server.tool(annotations=READ_ONLY, description='Read the financial forecast plan and revision. Includes dated cash/debt snapshots, recurring assumptions and unresolved gaps. This is not live bank balance data. See runbooks/cash-forecast.md before changing assumptions.')
    async def get_forecast_plan() -> dict:
        return await client.request('GET','/forecast/plan')

    @server.tool(annotations=DESTRUCTIVE, description='Replace a forecast plan only with operator-authorized assumptions, not inferred certainty. payload contains plan, expected_revision from get_forecast_plan (null only for first creation), actor and note. Preserves private revision history; never changes ledger data. Unknown amounts must remain null with confidence unknown. Read runbooks/cash-forecast.md for the schema and accounting rules.')
    async def replace_forecast_plan(payload: dict) -> dict:
        return await client.request('PUT','/forecast/plan',json=payload)

    @server.tool(annotations=READ_ONLY, description='Calculate a read-only cash/debt scenario from dated snapshots, not actual current balances. horizon_days is 1..365. Optional extra_payment_cents and extra_payment_date model a one-off snowball payment; snowball_monthly_cents supplies an estimated monthly payment allowance without saving it. All amounts are integer cents. Inspect missing payments, stale snapshots and property allocation gaps before interpreting remaining cash; card purchases and debt payments are separate.')
    async def get_cash_forecast(horizon_days: int = 90, extra_payment_cents: int = 0,
                                extra_payment_date: str | None = None, snowball_monthly_cents: int | None = None) -> dict:
        return await client.request('GET','/forecast/projection',params={k:v for k,v in {
            'horizon_days':horizon_days,'extra_payment_cents':extra_payment_cents,
            'extra_payment_date':extra_payment_date,'snowball_monthly_cents':snowball_monthly_cents}.items() if v is not None})
