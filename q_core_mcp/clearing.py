"""One way to unset a field across every `update_*` tool (ticket T-17).

The API distinguishes an absent key from an explicit null: absent leaves
a field alone, null clears it. A PYTHON SIGNATURE CANNOT EXPRESS THAT.
`update_entity(id)` and `update_entity(id, name=None)` are the same call
and both arrive as `None`, measured against the SDK:

    plain    (a: str|None = None)   omitted -> None    explicit null -> None
    sentinel (a: str|None = UNSET)  omitted -> UNSET   explicit null -> None

A sentinel default would work, and is rejected: the generated schema then
advertises `"default": "__unset__"` on the argument, and a caller sending
that literal string as a real value would have it silently read as "not
supplied". That is a correctness hole on any free-text field, bought for
nothing.

So clearing gets its own argument. `clear=["category_id"]` becomes
`{"category_id": null}` in the JSON body, which is the layer that CAN say
it. The tools keep omitting null arguments, because a null argument
carries no intent to forward.

Chosen jointly with impl-3 (#124 shipped this shape first for
`update_relationship`): one convention across the surface matters more
than which convention it is.
"""

from mcp.server.mcpserver.exceptions import ToolError

#: Interpolated into every `update_*` tool description, so the surface
#: explains itself the same way everywhere.
CLEAR_NOTE = (
    "To UNSET a field rather than change it, name it in clear, e.g. "
    'clear=["entity_id"] — passing null as the value does nothing, because '
    "this tool cannot tell a null you sent from an argument you left out. "
    "Only nullable fields can be cleared; clearing a required one is "
    "refused and the error names what is clearable."
)


def apply_clear(body: dict, clear: list[str] | None, tool: str) -> dict:
    """Add an explicit null to `body` for each field named in `clear`.

    Refuses a field that is also being set. Guessing which was meant
    would be a silent wrong write, and both guesses are defensible --
    "the explicit value wins" and "the explicit clear wins" are equally
    arguable, which is the sign it should not be guessed at all.

    Does NOT check whether a field is clearable: the API owns that, and
    refuses by name against the database's own nullability. Duplicating
    the list here would give two sources that can disagree.
    """
    for field in clear or []:
        if field in body:
            raise ToolError(
                f"{tool} was asked both to set and to clear {field!r}. Send "
                f"one or the other -- guessing which was meant would be a "
                f"silent wrong write."
            )
        body[field] = None
    return body
