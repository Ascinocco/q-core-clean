"""Canvas links: artifacts to tickets and entities. Thin client of the API."""
from mcp.server import MCPServer

from api.artifact_links import LIST_ARTIFACT_LINKS_ORDER
from api.jyra import TICKET_REF_CONTRACT
from q_core_mcp.annotations import ADDITIVE, DESTRUCTIVE, READ_ONLY
from q_core_mcp.client import QCoreClient
from q_core_mcp.paging import PAGING_NOTE, list_request


def register_artifact_link_tools(server: MCPServer, client: QCoreClient):
    @server.tool(annotations=ADDITIVE, description='Link an artifact to a Jyra ticket or an entity. target_type is ticket or entity; the target must exist. Linking the same target again returns the existing link with created=false. Ticket delete removes its links but keeps the artifact; entity delete is refused while linked. '
                 f'For a ticket target, {TICKET_REF_CONTRACT}; the link stores the UUID.')
    async def link_artifact(artifact_id: str, target_type: str, target_id: str, actor: str) -> dict:
        return await client.request('POST', f'/artifacts/{artifact_id}/links',
                                    json={'target_type': target_type, 'target_id': target_id, 'actor': actor})

    @server.tool(annotations=DESTRUCTIVE, description='Remove one link from an artifact by link_id (from list_artifact_links). The artifact and its other links are unchanged.')
    async def unlink_artifact(artifact_id: str, link_id: str) -> dict:
        return await client.request('DELETE', f'/artifacts/{artifact_id}/links/{link_id}')

    @server.tool(annotations=READ_ONLY, description=f'{PAGING_NOTE} List an artifact\'s links, {LIST_ARTIFACT_LINKS_ORDER}. Each has link id, target_type, target_id, actor and created_at, plus the ticket key (e.g. KA-12) and title, or the entity name.')
    async def list_artifact_links(artifact_id: str, limit: int | None = None, offset: int = 0, all: bool = False, allow_truncated: bool = False) -> dict:
        return await list_request(client, f'/artifacts/{artifact_id}/links', limit=limit, offset=offset, fetch_all=all, allow_truncated=allow_truncated)
