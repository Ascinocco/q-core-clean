"""Google Calendar connection controls; reminder writes remain in reminders."""

from mcp.server import MCPServer

from q_core_mcp.annotations import ADDITIVE, READ_ONLY
from q_core_mcp.client import QCoreClient


def register_google_calendar_tools(server: MCPServer, client: QCoreClient) -> None:
    @server.tool(annotations=READ_ONLY, description="Report whether the one-way Google Calendar reminder projection is connected and its last sync health.")
    async def google_calendar_status() -> dict:
        return await client.request("GET", "/integrations/google/status")

    @server.tool(annotations=ADDITIVE, description="Start the one-time Google Calendar OAuth connection. Open the returned authorization_url in a browser and approve it; q-core creates its dedicated q-core Reminders calendar after the callback.")
    async def connect_google_calendar() -> dict:
        return await client.request("POST", "/integrations/google/connect")

    @server.tool(annotations=ADDITIVE, description="Synchronize every active q-core reminder to its dedicated Google Calendar. Google edits are overwritten and deleted events are recreated, because q-core is authoritative.")
    async def sync_google_calendar() -> dict:
        return await client.request("POST", "/integrations/google/sync")
