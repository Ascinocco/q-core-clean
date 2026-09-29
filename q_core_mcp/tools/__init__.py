"""Tool registration, split by domain.

`register_tools` keeps its name and signature across the split, so
`server.py` is unchanged by it.
"""

from mcp.server import MCPServer

from q_core_mcp.client import QCoreClient
from q_core_mcp.tools.canvas import register_canvas_tools
from q_core_mcp.tools.artifact_links import register_artifact_link_tools
from q_core_mcp.tools.briefings import register_briefing_tools
from q_core_mcp.tools.documents import register_document_tools
from q_core_mcp.tools.due import register_due_tools
from q_core_mcp.tools.entities import register_entity_tools
from q_core_mcp.tools.financial import register_financial_tools
from q_core_mcp.tools.google_calendar import register_google_calendar_tools
from q_core_mcp.tools.jyra import register_jyra_tools
from q_core_mcp.tools.notes import register_note_tools
from q_core_mcp.tools.reminders import register_reminder_tools
from q_core_mcp.tools.source_imports import register_source_import_tools
from q_core_mcp.tools.forecast import register_forecast_tools


def register_tools(server: MCPServer, client: QCoreClient) -> None:
    register_briefing_tools(server, client)
    register_canvas_tools(server, client)
    register_artifact_link_tools(server, client)
    register_document_tools(server, client)
    register_due_tools(server, client)
    register_entity_tools(server, client)
    register_financial_tools(server, client)
    register_source_import_tools(server, client)
    register_forecast_tools(server, client)
    register_jyra_tools(server, client)
    register_reminder_tools(server, client)
    register_note_tools(server, client)
    register_google_calendar_tools(server, client)
