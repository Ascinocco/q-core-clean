"""The server as `api/main.py` actually builds it (ticket T-21).

Every other test in this suite wires `build_server(real QCoreClient)`.
Production wires `build_server(_LazyClient())`, and `_LazyClient` proxied
ONE method. So `attach_file` -- which reads `client.settings` -- raised
AttributeError against the deployed server for its whole shipped life,
the model saw "Error executing tool attach_file", and every refusal in
that tool (roots, DB, .env) was dead code in production.

Nothing caught it because the thing that differed was the WIRING, and no
test exercised the wiring. That is the gap this file closes: it builds
the client the way main.py does and drives real tools through it.

Only the socket is substituted -- `QCoreClient` gets an ASGI transport,
exactly as every round-trip test does -- because the loopback HTTP call
is not what is under test. The proxying is.
"""

import httpx
import pytest

from api.config import get_settings
from api.main import _LazyClient
from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server


@pytest.fixture()
def production_server(test_settings, monkeypatch):
    """`build_server(_LazyClient())` -- the expression from api/main.py."""
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    monkeypatch.setattr("api.main.get_settings", lambda: test_settings)
    monkeypatch.setattr(
        "api.main.QCoreClient",
        lambda settings: QCoreClient(
            settings, transport=httpx.ASGITransport(app=app)
        ),
    )
    try:
        yield build_server(_LazyClient())
    finally:
        app.dependency_overrides.clear()


def _ticket(server, call_tool):
    entity = call_tool(
        server, "create_entity", {"entity_type": "project", "name": "proj"}
    )
    board = call_tool(
        server, "create_board", {"entity_id": entity["id"], "title": "D"}
    )
    return call_tool(
        server,
        "create_ticket",
        {
            "board_id": board["id"],
            "ticket_type": "task",
            "title": "t",
            "actor": "owner",
        },
    )


def test_attach_file_works_through_the_production_wiring(
    production_server, call_tool, tmp_path
):
    """The headline: this raised AttributeError against the real server.

    An ordinary successful attach, which is the case that was broken --
    not an edge case, the main path.
    """
    source = tmp_path / "note.txt"
    source.write_text("hello")

    ticket = _ticket(production_server, call_tool)
    attachment = call_tool(
        production_server,
        "attach_file",
        {"ticket_id": ticket["id"], "path": str(source)},
    )

    assert attachment["filename"] == "note.txt"


def test_read_attachment_works_through_the_production_wiring(
    production_server, call_tool, tmp_path
):
    """The second tool with the same bug, and the reason for the fix's shape.

    `read_attachment` calls `client.fetch_bytes`. The one-method proxy had
    no such attribute either, so it would have raised AttributeError over
    the deployed server exactly as `attach_file` did -- a second instance
    of one bug, arriving because the minimal fix for the first would have
    been to add one more name to a list.

    Driven end to end (attach, then read back by id) rather than asserted
    on the client, because what was broken was the tool, not the method.
    """
    source = tmp_path / "note.txt"
    source.write_text("content through the real wiring")

    ticket = _ticket(production_server, call_tool)
    attachment = call_tool(
        production_server,
        "attach_file",
        {"ticket_id": ticket["id"], "path": str(source)},
    )
    result = call_tool(
        production_server, "read_attachment", {"attachment_id": attachment["id"]}
    )

    assert result["text"] == "content through the real wiring"


def test_the_refusals_in_attach_file_are_not_dead_code_in_production(
    production_server, call_tool, test_settings
):
    """The second half of the same bug, and the worse half.

    A refusal that raises AttributeError still "fails", so the tool looked
    like it was refusing when it was merely broken. This asserts the
    refusal is REACHED -- it names the reason -- rather than that the call
    failed somehow.
    """
    ticket = _ticket(production_server, call_tool)

    with pytest.raises(Exception) as raised:
        call_tool(
            production_server,
            "attach_file",
            {"ticket_id": ticket["id"], "path": str(test_settings.db_path)},
        )

    message = str(raised.value)
    assert "AttributeError" not in message, message
    assert "database" in message.lower() or "db" in message.lower(), message


# --- the guard, enumerated by property ---------------------------------


def _attributes_read_off_the_client() -> set[str]:
    """Every `client.<name>` the tool modules actually use.

    Discovered from the source rather than listed here. A hand-written
    list is precisely what failed: `_LazyClient` named one method, and
    nothing noticed when `attach_file` reached for a second or
    `read_attachment` for a third. A list that must be updated by hand
    falls behind silently; a property cannot.
    """
    import ast
    import pathlib

    names: set[str] = set()
    directory = pathlib.Path(__file__).resolve().parent.parent / "tools"
    for module in sorted(directory.glob("*.py")):
        tree = ast.parse(module.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "client"
            ):
                names.add(node.attr)
    return names


def test_every_attribute_the_tools_use_survives_the_production_wiring(
    test_settings, monkeypatch
):
    """The regression guard for the whole class.

    Not "does _LazyClient have settings" -- that pins today's bug and
    would miss tomorrow's. This asserts the PROPERTY: whatever the tools
    reach for on the client is reachable through the proxy the server is
    actually built with.
    """
    from api.main import app

    monkeypatch.setattr("api.main.get_settings", lambda: test_settings)
    monkeypatch.setattr(
        "api.main.QCoreClient",
        lambda settings: QCoreClient(
            settings, transport=httpx.ASGITransport(app=app)
        ),
    )

    used = _attributes_read_off_the_client()
    # The discovery must have found something: an empty set makes the
    # assertion below vacuously true, and renaming the `client` parameter
    # is all it would take. The two named ones are the anti-vacuity
    # check that matters -- the count is a floor, not a pin, because the
    # whole point is that this set GROWS without this file being edited.
    # It already has: `fetch_bytes` arrived with read_attachment (#130)
    # and this guard picked it up with no edit here, which is the
    # behaviour a hand-written list would not have had.
    assert len(used) >= 2, used
    assert "request" in used, used
    assert "settings" in used, "attach_file reads it; this is the ticket T-21 bug"

    lazy = _LazyClient()
    missing = sorted(name for name in used if not hasattr(lazy, name))

    assert missing == [], f"not reachable through the production wiring: {missing}"


def test_the_proxy_refuses_private_names(test_settings, monkeypatch):
    """Underscore names are not forwarded.

    Asserted because the delegation is wholesale: without this the proxy
    would hand out the real client's privates, and `_client` could recurse
    into __getattr__ if __init__ had not run.
    """
    lazy = _LazyClient()

    with pytest.raises(AttributeError):
        lazy._not_a_real_attribute


# --- constructed exactly once ------------------------------------------


def _counting_factory(app, constructions, delay=0.0):
    import time

    def factory(settings):
        # The sleep widens the window between "decided to construct" and
        # "stored the result". Without it the race is real but too narrow
        # to observe reliably, and a test that only sometimes catches the
        # bug is a test that will be believed when it passes.
        constructions.append(settings)
        if delay:
            time.sleep(delay)
        return QCoreClient(settings, transport=httpx.ASGITransport(app=app))

    return factory


def test_the_client_is_constructed_once_when_first_calls_race(
    test_settings, monkeypatch
):
    """Threads, not tasks, and the distinction is the whole point.

    `__getattr__` is an ordinary synchronous attribute access, so any
    thread may reach it. Under asyncio alone there is no await between
    the check and the assignment, so concurrent TASKS cannot interleave
    there -- which means an async-only version of this test would pass
    with the lock removed. It would be measuring the wrong thing while
    reporting a clean number.

    Two clients means two httpx connection pools, one silently orphaned.
    """
    import concurrent.futures
    import threading

    from api.main import app

    constructions: list = []
    monkeypatch.setattr("api.main.get_settings", lambda: test_settings)
    monkeypatch.setattr(
        "api.main.QCoreClient", _counting_factory(app, constructions, delay=0.05)
    )

    lazy = _LazyClient()
    workers = 8
    ready = threading.Barrier(workers)

    def first_use():
        ready.wait()  # every thread arrives at the unbuilt client together
        return lazy.settings

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        results = [f.result() for f in [pool.submit(first_use) for _ in range(workers)]]

    assert len(constructions) == 1, f"built {len(constructions)} clients, not 1"
    # ...and every caller got the same one, not just a low count.
    assert all(r is results[0] for r in results)


def test_concurrent_tool_calls_share_one_client(test_settings, monkeypatch):
    """The same property in the shape production actually has it.

    Kept alongside the thread test rather than instead of it: this is the
    real usage, but it cannot fail if the lock is removed, so it is
    evidence about the wiring and not about the locking.
    """
    import functools

    import anyio

    from api.main import app

    constructions: list = []
    app.dependency_overrides[get_settings] = lambda: test_settings
    monkeypatch.setattr("api.main.get_settings", lambda: test_settings)
    monkeypatch.setattr("api.main.QCoreClient", _counting_factory(app, constructions))

    server = build_server(_LazyClient())

    async def drive():
        async with anyio.create_task_group() as group:
            for _ in range(8):
                group.start_soon(functools.partial(server.call_tool, "list_boards", {}))

    try:
        anyio.run(drive)
    finally:
        app.dependency_overrides.clear()

    assert len(constructions) == 1, f"built {len(constructions)} clients, not 1"


def test_a_real_missing_attribute_is_not_mistaken_for_an_unbuilt_proxy(
    test_settings, monkeypatch
):
    """A genuine gap in the underlying client must surface as itself.

    If `__getattr__` let that read as "not constructed yet", the fix
    would be attempted on this class rather than on the one actually
    missing the attribute -- and a retry loop could rebuild the client
    on every access.
    """
    from api.main import app

    constructions: list = []
    monkeypatch.setattr("api.main.get_settings", lambda: test_settings)
    monkeypatch.setattr("api.main.QCoreClient", _counting_factory(app, constructions))

    lazy = _LazyClient()

    with pytest.raises(AttributeError) as raised:
        lazy.no_such_method_on_the_real_client

    message = str(raised.value)
    assert "QCoreClient" in message, message
    assert "no_such_method_on_the_real_client" in message
    # Built once to answer the question, and NOT rebuilt by the failure.
    assert len(constructions) == 1, constructions


def test_api_error_codes_distinguish_missing_address_from_missing_reference(
    production_server, call_tool
):
    from mcp.server.mcpserver.exceptions import ToolError

    messages = []
    for name, arguments in (
        ('get_entity', {'entity_id': 'missing'}),
        ('create_board', {'entity_id': 'missing', 'title': 'Example'}),
    ):
        with pytest.raises(ToolError) as exc:
            call_tool(production_server, name, arguments)
        messages.append(str(exc.value))

    # The same explanatory message must retain two distinct API codes.
    assert messages == [
        "Error executing tool get_entity: not_found: No entity with id 'missing'",
        "Error executing tool create_board: invalid_reference: No entity with id 'missing'",
    ]


def test_api_conflict_code_survives_production_wiring(production_server, call_tool):
    from mcp.server.mcpserver.exceptions import ToolError

    ticket = _ticket(production_server, call_tool)
    with pytest.raises(ToolError, match=r'^Error executing tool delete_board: conflict: .*ticket'):
        call_tool(production_server, 'delete_board', {'board_id': ticket['board_id']})


def test_api_validation_code_and_details_survive_production_wiring(
    production_server, call_tool
):
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError) as exc:
        call_tool(production_server, 'create_reminder', {
            'title': 'Example', 'start_date': 'invalid-date',
        })
    message = str(exc.value)
    assert message.startswith('Error executing tool create_reminder: validation_error: Request validation failed\n')
    assert 'start_date:' in message


def test_reminder_lifecycle_through_production_mcp(production_server, call_tool):
    from datetime import date, timedelta
    today = date.today()
    original = (today - timedelta(days=40)).isoformat()
    moved = (today - timedelta(days=1)).isoformat()
    reminder = call_tool(production_server, 'create_reminder', {'title': 'Synthetic MCP reminder', 'start_date': original, 'notes': 'before'})
    edited = call_tool(production_server, 'update_reminder', {'reminder_id': reminder['id'], 'title': 'Edited MCP reminder', 'clear': ['notes']})
    assert edited['id'] == reminder['id'] and edited['notes'] is None
    call_tool(production_server, 'snooze_reminder', {'reminder_id': reminder['id'], 'due_date': original, 'snoozed_to': moved})
    items = call_tool(production_server, 'list_due_items', {'source': 'reminder', 'all': True})['items']
    assert len(items) == 1 and items[0]['reminder_id'] == reminder['id']
    assert items[0]['due_date'] == moved
    call_tool(production_server, 'complete_reminder', {'reminder_id': reminder['id'], 'due_date': moved})
    assert call_tool(production_server, 'list_due_items', {'source': 'reminder', 'all': True})['items'] == []
    entity = call_tool(production_server, 'create_entity', {'entity_type': 'pet', 'name': 'Synthetic Pet', 'attributes': {'date_of_birth': '2020-06-10'}})
    birthday = call_tool(production_server, 'list_reminders', {'entity_id': entity['id'], 'all': True})['items'][0]
    assert birthday['notification_offsets_minutes'] == [10080, 1440, 60]
    assert birthday['due_time'] == '09:00:00'
