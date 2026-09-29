"""Tools over api/reminders.py.

One tool per endpoint, as elsewhere. The descriptions carry two things
a model cannot infer from the shapes: that `GET /due` — not a reminders
endpoint — is where you ask what is coming up, and that `due_date`
identifies WHICH occurrence of a recurring reminder is being completed
or snoozed rather than being optional metadata.
"""

from api.reminders import LIST_REMINDERS_ORDER
from q_core_mcp.paging import MAX_LIMIT, PAGING_NOTE, list_request
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from q_core_mcp.annotations import (
    ADDITIVE,
    ADDITIVE_IDEMPOTENT,
    DESTRUCTIVE,
    READ_ONLY,
)
from q_core_mcp.client import QCoreClient
from q_core_mcp.clearing import CLEAR_NOTE, apply_clear


def register_reminder_tools(server: MCPServer, client: QCoreClient) -> None:
    @server.tool(
        annotations=ADDITIVE,
        description=(
            "Create a reminder. title and start_date (ISO date) are "
            "required.\n\n"
            "Leave recurrence_rule unset for a one-off: the reminder is "
            "its start_date and nothing else. For a repeating one, pass "
            "an RRULE string such as 'FREQ=WEEKLY', "
            "'FREQ=MONTHLY;BYMONTHDAY=1' or 'FREQ=YEARLY'. The rule is "
            "validated here and an unusable one is rejected with validation_error — "
            "do not retry the same string, fix it.\n\n"
            "end_date ends a repeating series and is only allowed "
            "alongside a recurrence_rule. entity_id links the reminder to "
            "an entity so it is reported against that thing.\n\n"
            "Do NOT create a reminder for something the entity already "
            "records as an attribute — a vehicle's registration_expiry or "
            "an account's renewal_date are already reported by "
            "list_due_items. A reminder for the same date is a second "
            "copy that can drift from the first. Birthdays are different: saving a person or pet date_of_birth automatically maintains a linked annual birthday reminder at 09:00 local time with week/day/hour alerts; do not create a second birthday reminder manually."
        ),
    )
    async def create_reminder(
        title: str,
        start_date: str,
        notes: str | None = None,
        due_time: str | None = None,
        location: str | None = None,
        notification_offsets_minutes: list[int] | None = None,
        entity_id: str | None = None,
        recurrence_rule: str | None = None,
        end_date: str | None = None,
    ) -> dict:
        body: dict = {"title": title, "start_date": start_date}
        # Omitted rather than sent as null: end_date without a
        # recurrence_rule is a 422, and an explicit null for the others
        # is indistinguishable from a value the caller meant to set.
        if notes is not None:
            body["notes"] = notes
        if due_time is not None:
            body["due_time"] = due_time
        if location is not None:
            body["location"] = location
        if notification_offsets_minutes is not None:
            body["notification_offsets_minutes"] = notification_offsets_minutes
        if entity_id is not None:
            body["entity_id"] = entity_id
        if recurrence_rule is not None:
            body["recurrence_rule"] = recurrence_rule
        if end_date is not None:
            body["end_date"] = end_date
        return await client.request("POST", "/reminders", json=body)

    @server.tool(
        annotations=DESTRUCTIVE,
        description=(
            "Edit a reminder series, retaining its id. Schedule changes with existing completion/snooze history require reset_occurrences=true, which clears that history. "
            "For managed birthdays, change the entity birthday/name; reminder notes, location, time and notification overrides can be edited here. "
            + CLEAR_NOTE
        ),
    )
    async def update_reminder(
        reminder_id: str,
        title: str | None = None,
        notes: str | None = None,
        entity_id: str | None = None,
        start_date: str | None = None,
        recurrence_rule: str | None = None,
        end_date: str | None = None,
        due_time: str | None = None,
        location: str | None = None,
        notification_offsets_minutes: list[int] | None = None,
        reset_occurrences: bool = False,
        clear: list[str] | None = None,
    ) -> dict:
        body = {
            key: value
            for key, value in locals().items()
            if key not in {"reminder_id", "clear", "client"} and value is not None
        }
        return await client.request(
            "PATCH",
            f"/reminders/{reminder_id}",
            json=apply_clear(body, clear, "update_reminder"),
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Get one reminder by id: its title, notes, linked entity, "
            "recurrence rule and dates."
        ),
    )
    async def get_reminder(reminder_id: str) -> dict:
        return await client.request("GET", f"/reminders/{reminder_id}")

    @server.tool(
        annotations=READ_ONLY,
        description=(
            f"{PAGING_NOTE} "
            f"List reminders as they were DEFINED, ordered {LIST_REMINDERS_ORDER}. "
            f"entity_id filters to one entity. limit is 1-{MAX_LIMIT}.\n\n"
            "This is not the way to ask what is coming up. A repeating "
            "reminder is ONE row here however many times it fires, and "
            "renewals stored as entity attributes do not appear at all. "
            "Use list_due_items for 'what's due'; use this to find or "
            "check a reminder you are about to change or delete.\n\n"
            'Returns {"items", "total", "limit", "offset"}.'
        ),
    )
    async def list_reminders(
        entity_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        all: bool = False,
        allow_truncated: bool = False,
    ) -> dict:
        params: dict = {}
        if entity_id is not None:
            params["entity_id"] = entity_id
        return await list_request(
            client,
            "/reminders",
            params=params,
            limit=limit,
            offset=offset,
            fetch_all=all,
            allow_truncated=allow_truncated,
        )

    @server.tool(
        annotations=DESTRUCTIVE,
        description=(
            "Delete a reminder and every record of its occurrences. This "
            "cannot be undone and it removes the whole series, not one "
            "date. To clear a single occurrence use complete_reminder; to "
            "edit its schedule use update_reminder. Managed birthday reminders "
            "are removed by setting birthday_reminder_enabled=false on the entity."
        ),
    )
    async def delete_reminder(reminder_id: str) -> dict:
        return await client.request("DELETE", f"/reminders/{reminder_id}")

    @server.tool(
        annotations=ADDITIVE,
        description=(
            "Mark ONE occurrence of a reminder done, so it stops "
            "appearing in list_due_items.\n\n"
            "due_date identifies which occurrence and is required — a "
            "repeating reminder has many, and the rest of the series is "
            "unaffected. Pass the due_date exactly as list_due_items "
            "reported it, including for an occurrence that was snoozed: "
            "the date it is reported at identifies it. Completing the "
            "same occurrence twice is fine.\n\n"
            "A date that is not an occurrence is REFUSED, and the "
            "refusal names the dates that are. It is not recorded "
            "against a date the reminder is never due on, which is what "
            "a 200 used to mean here."
        ),
    )
    async def complete_reminder(reminder_id: str, due_date: str) -> dict:
        return await client.request(
            "POST",
            f"/reminders/{reminder_id}/complete",
            json={"due_date": due_date},
        )

    @server.tool(
        annotations=ADDITIVE_IDEMPOTENT,
        description=(
            "Move ONE occurrence of a reminder to a later date.\n\n"
            "due_date identifies which occurrence, exactly as "
            "list_due_items reported it — for an already-snoozed "
            "occurrence that is its snoozed date, which resolves back to "
            "the occurrence it belongs to. snoozed_to is the new date "
            "and must be after it. A date that is not an occurrence is "
            "REFUSED naming the ones that are. "
            "The rest of the series is unaffected. A "
            "snooze past the end of the window you are looking at will "
            "make the item disappear from list_due_items until then, "
            "which is the point."
        ),
    )
    async def snooze_reminder(reminder_id: str, due_date: str, snoozed_to: str) -> dict:
        return await client.request(
            "POST",
            f"/reminders/{reminder_id}/snooze",
            json={"due_date": due_date, "snoozed_to": snoozed_to},
        )
