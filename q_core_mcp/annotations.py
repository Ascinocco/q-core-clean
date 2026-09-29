"""Tool annotations, defined once (ticket T-61).

Every tool carries one. A client uses these to decide what to confirm
with a human and what to retry, so an UNANNOTATED write is not neutral:
`destructive_hint` defaults to true in the MCP spec when it is absent, so
a harmless `create_ticket` reads as dangerous, and a client that prompts
on destructive calls prompts on everything. The annotations stop being
signal and start being noise, which is worse than having none — a human
clicking through every prompt is not reviewing any of them.

`q_core_mcp/tests/test_tool_annotations.py` walks the REGISTERED tool
set and fails on a tool carrying neither `read_only_hint` nor an explicit
`destructive_hint`, so the next tool cannot arrive unannotated.

These were five copies of two constants, one per tools module. Defined
here for the same reason `clearing.py` exists: a constant duplicated per
module is a constant that can disagree with itself.
"""

from mcp.types import ToolAnnotations

#: Reads nothing but the database and changes nothing.
READ_ONLY = ToolAnnotations(read_only_hint=True)

#: Creates or appends. Nothing that existed before is overwritten or
#: removed, so a client has no reason to confirm it the way it would a
#: delete. NOT marked idempotent: a second call generally creates a
#: second row.
ADDITIVE = ToolAnnotations(destructive_hint=False)

#: Additive, and repeating it with the same arguments leaves the same
#: state -- an upsert keyed on something the caller supplies, or a field
#: set to a fixed value.
#:
#: Claimed only where it was CHECKED, not where it felt true. Measured:
#: `complete_reminder` looks like the obvious idempotent case and is not,
#: because it writes `datetime.now()` as `completed_at`, so a repeat moves
#: the timestamp. Every `update_*` fails the same way on
#: `updated_at`/`edited_at`. Over-claiming here tells a client it is safe
#: to retry a call that is not.
ADDITIVE_IDEMPOTENT = ToolAnnotations(destructive_hint=False, idempotent_hint=True)

#: Overwrites or removes. Deletes, archives, and updates that replace a
#: value a caller may not have meant to lose.
DESTRUCTIVE = ToolAnnotations(destructive_hint=True)
