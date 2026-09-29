"""The MCP server's tool registry.

No entry point of its own any more: the server is mounted on the API's
FastAPI app at /mcp and runs in that process. `api.main` builds it.
"""

from mcp.server import MCPServer
from mcp.types import CallToolResult
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from q_core_mcp.client import QCoreClient
from q_core_mcp.tools import register_tools

INSTRUCTIONS = (
    "q-core is the owner's personal inventory: people, property, "
    "vehicles, pets and accounts, plus the relationships between them "
    "(who owns what, what insures what). Use these tools to answer "
    "questions about what the owner has and to record changes."
)


_tracer = trace.get_tracer("q-core")


class TracedServer(MCPServer):
    """One `mcp.tool <name>` span per tool call (see runbooks/observability.md).

    Attributes are the tool's name and `ok`/`error` only; never its
    arguments or result. Without telemetry configured this is the no-op
    tracer. api/telemetry.py's allowlist still filters what is exported.
    """

    async def call_tool(self, name, arguments, context=None):
        # The name comes from the client: record it only if it's a real tool,
        # so span names stay a closed, low-cardinality set.
        known = name if self._tool_manager.get_tool(name) is not None else "unknown"
        with _tracer.start_as_current_span(
            f"mcp.tool {known}", attributes={"mcp.tool.name": known},
            record_exception=False, set_status_on_exception=False,
        ) as span:
            try:
                result = await super().call_tool(name, arguments, context)
            except Exception as exc:
                span.set_attributes({"mcp.outcome": "error", "error.type": type(exc).__name__})
                span.set_status(StatusCode.ERROR)
                raise
            failed = isinstance(result, CallToolResult) and result.is_error
            span.set_attribute("mcp.outcome", "error" if failed else "ok")
            if failed:
                span.set_status(StatusCode.ERROR)
            return result


def build_server(client: QCoreClient) -> MCPServer:
    server = TracedServer("q-core", instructions=INSTRUCTIONS)
    register_tools(server, client)
    return server
