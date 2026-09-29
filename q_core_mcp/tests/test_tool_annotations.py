"""Every registered tool carries an explicit annotation (ticket T-61).

A client reads these to decide what to confirm with a human and what is
safe to retry. **An unannotated write is not neutral**: the MCP spec
treats a missing `destructive_hint` as true, so a harmless `create_ticket`
reads as dangerous. A client that prompts on destructive calls then
prompts on everything, and the annotations stop being signal — a human
clicking through every prompt is not reviewing any of them.

Walked over the REGISTERED tool set rather than a list in this file. A
literal would have to be updated by the same commit that adds a tool,
which is the commit that forgets.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import anyio
import httpx
import pytest

from api.config import REPO_ROOT, Settings
from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server


def _tools():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        settings = Settings(
            _env_file=None, api_token="t", db_path=str(tmp / "d.db"),
            documents_dir=str(tmp / "doc"), jyra_dir=str(tmp / "j"),
            logs_dir=str(tmp / "l"), intake_dir=str(tmp / "i"),
            inbox_dir=str(tmp / "in"),
            schema_path=str(REPO_ROOT / "db" / "schema.sql"),
            seed_categories_path=str(REPO_ROOT / "db" / "seed_categories.sql"),
            migrations_dir=str(REPO_ROOT / "db" / "migrations"),
        )
        client = QCoreClient(
            settings,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        )
        return anyio.run(build_server(client).list_tools)


def test_every_tool_is_annotated():
    """The guard the ticket asks for: a new tool cannot arrive unannotated.

    Before this, 20 of 52 tools carried no annotation at all -- every
    create, every update, the whole reminder write surface, and
    `claim_ticket`, which an agent loop calls in a loop.
    """
    tools = _tools()
    assert tools, "the server registered no tools -- the build broke"

    unannotated = sorted(
        tool.name
        for tool in tools
        if tool.annotations is None
        or (
            not tool.annotations.read_only_hint
            and tool.annotations.destructive_hint is None
        )
    )
    assert not unannotated, (
        f"tools with neither read_only_hint nor an explicit destructive_hint: "
        f"{unannotated}. A missing destructive_hint reads as TRUE to a "
        f"client, so an additive create is presented as dangerous. Pick one "
        f"from q_core_mcp/annotations.py and say why in the PR."
    )


def test_read_only_tools_do_not_also_claim_destructive():
    """The two are contradictory, and a client reading either first would
    get a different answer."""
    for tool in _tools():
        annotations = tool.annotations
        if annotations and annotations.read_only_hint:
            assert annotations.destructive_hint in (None, False), tool.name


def test_the_annotation_split_is_not_degenerate():
    """Guards the guard.

    If everything were annotated the same way the test above would pass
    while the annotations carried no information. This is a floor on the
    split, not a pin on the counts -- a count would need editing every
    time a tool lands and then fail for the wrong reason.
    """
    tools = _tools()
    read_only = [t for t in tools if t.annotations and t.annotations.read_only_hint]
    destructive = [
        t for t in tools if t.annotations and t.annotations.destructive_hint is True
    ]
    additive = [
        t for t in tools if t.annotations and t.annotations.destructive_hint is False
    ]

    assert len(read_only) >= 10, "suspiciously few read-only tools"
    assert len(destructive) >= 5, "suspiciously few destructive tools"
    assert len(additive) >= 5, "suspiciously few additive tools"
    assert len(read_only) + len(destructive) + len(additive) == len(tools)


@pytest.mark.parametrize(
    "name",
    ["delete_entity", "delete_ticket", "delete_document", "delete_attachment",
     "delete_relationship", "delete_reminder", "archive_statement",
     "archive_transaction", "update_entity", "update_ticket",
     "update_transaction", "update_merchant_rule"],
)
def test_overwrites_and_deletes_are_destructive(name):
    """Named explicitly, because this is the half where a wrong answer
    costs data rather than costing a prompt."""
    tools = {tool.name: tool for tool in _tools()}
    assert tools[name].annotations.destructive_hint is True, name


@pytest.mark.parametrize(
    "name", ["snooze_reminder", "unarchive_statement", "unarchive_transaction"]
)
def test_only_checked_tools_claim_idempotence(name):
    """Idempotence was claimed only where it was verified in the source.

    `complete_reminder` is the instructive exclusion: it looks like the
    obvious idempotent case -- an upsert keyed on (reminder_id, due_date)
    -- and it is not, because it writes `datetime.now()` as
    `completed_at`, so a repeat moves the timestamp. Every `update_*`
    fails the same way on `updated_at`/`edited_at`. Telling a client a
    call is safe to retry when it is not is worse than saying nothing.
    """
    tools = {tool.name: tool for tool in _tools()}
    assert tools[name].annotations.idempotent_hint is True, name


def test_complete_reminder_does_not_claim_idempotence():
    """The measured exclusion, pinned so it is not 'tidied up' later."""
    tools = {tool.name: tool for tool in _tools()}
    assert tools["complete_reminder"].annotations.idempotent_hint is not True
