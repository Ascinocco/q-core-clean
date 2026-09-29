"""Canvas diagram editing. Thin client: every rule lives in api/canvas_diagram_ops.py."""
from mcp.server import MCPServer

from q_core_mcp.annotations import DESTRUCTIVE
from q_core_mcp.client import QCoreClient

#: The op vocabulary as the tool description states it. Pinned equal to the
#: API's discriminated union by q_core_mcp/tests/test_tools_canvas.py.
GRAPH_OPERATIONS = ('add_node, update_node, remove_node(dependents), add_edge, update_edge, remove_edge, add_group, '
                    'update_group, move_to_group, remove_group(members), add_note, remove_note, set_layout, pin, unpin')
SEQUENCE_OPERATIONS = ('add_participant, update_participant, move_participant, remove_participant(dependents), '
                       'insert_item, move_item, remove_item(contents), update_message, update_fragment, add_section, '
                       'update_section, remove_section(contents)')
COMMON_OPERATIONS = 'set_meta, update_note'


def register_canvas_tools(server: MCPServer, client: QCoreClient):
    @server.tool(annotations=DESTRUCTIVE, description=(
        'Apply an all-or-nothing batch of diagram operations as one new revision. payload: expected_revision, '
        'operations (1-200), actor, note. Read get_artifact(view=\'summary\') first. '
        f'Graph ops: {GRAPH_OPERATIONS}. Sequence ops: {SEQUENCE_OPERATIONS}. Both: {COMMON_OPERATIONS}. '
        'Removals need an explicit policy (dependents / members / contents). Stale expected_revision refuses; '
        'an exact retry returns the same revision. Errors name operations[i].'))
    async def edit_diagram(artifact_id: str, payload: dict) -> dict:
        return await client.request('PATCH', f'/artifacts/{artifact_id}/diagram', json=payload)
