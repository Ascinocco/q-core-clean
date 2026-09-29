import pytest

from fastapi.exceptions import RequestValidationError

from api.errors import InvalidReferenceError
from api.models import (
    STATUSES_BY_TYPE,
    TYPE_RANK,
    validate_parent_rank,
    validate_ticket_status,
)


@pytest.mark.parametrize("ticket_type", ["story", "bug", "task"])
@pytest.mark.parametrize(
    "status",
    [
        "backlog",
        "in_progress",
        "agent_ready",
        "agent_coding",
        "review",
        "blocked",
        "done",
    ],
)
def test_delivery_types_accept_every_status(ticket_type, status):
    assert validate_ticket_status(ticket_type, status) is None


@pytest.mark.parametrize("ticket_type", ["solution", "epic"])
@pytest.mark.parametrize("status", ["backlog", "in_progress", "blocked", "done"])
def test_planning_types_accept_planning_statuses(ticket_type, status):
    assert validate_ticket_status(ticket_type, status) is None


@pytest.mark.parametrize("ticket_type", ["solution", "epic"])
@pytest.mark.parametrize("status", ["agent_ready", "agent_coding", "review"])
def test_planning_types_reject_agent_statuses(ticket_type, status):
    # 422, not InvalidReferenceError's 400: a value outside its permitted
    # range, not a reference to something missing. Same mechanism as
    # validate_entity_attributes, and Phase 2's handler gives it the same
    # error envelope as everything else.
    with pytest.raises(RequestValidationError):
        validate_ticket_status(ticket_type, status)


def test_blocked_is_available_to_every_type():
    for ticket_type in TYPE_RANK:
        assert "blocked" in STATUSES_BY_TYPE[ticket_type]


def test_parent_must_outrank_child():
    assert validate_parent_rank("epic", "story") is None
    assert validate_parent_rank("solution", "task") is None


def test_equal_rank_parent_is_rejected():
    with pytest.raises(InvalidReferenceError):
        validate_parent_rank("task", "bug")


def test_lower_rank_parent_is_rejected():
    with pytest.raises(InvalidReferenceError):
        validate_parent_rank("bug", "epic")


def test_every_ticket_type_has_a_rank_and_a_status_set():
    """The two tables are keyed by the same vocabulary and are written by
    hand, so a type added to one and not the other would surface as a KeyError
    at request time rather than here."""
    from api.models import TicketType

    declared = set(TicketType.__args__)
    assert set(TYPE_RANK) == declared
    assert set(STATUSES_BY_TYPE) == declared


def test_status_sets_only_contain_declared_statuses():
    """Guards a typo in a status set, which would otherwise silently make a
    legal status unreachable for one ticket type."""
    from api.models import TicketStatus

    declared = set(TicketStatus.__args__)
    for ticket_type, statuses in STATUSES_BY_TYPE.items():
        assert statuses <= declared, f"{ticket_type} allows unknown: {statuses - declared}"


def test_agent_statuses_are_exactly_what_planning_types_exclude():
    """Pins the design's rule — planning types are excluded from the three
    agent-facing statuses and nothing else — so a future status added to one
    set and not the other is caught here rather than by an agent picking up an
    epic."""
    from api.models import TicketStatus

    declared = set(TicketStatus.__args__)
    excluded = declared - STATUSES_BY_TYPE["epic"]
    assert excluded == {"agent_ready", "agent_coding", "review"}
    assert STATUSES_BY_TYPE["story"] == declared


def test_rejected_status_error_points_at_the_offending_field():
    """The `loc` has to name the field the caller actually sent, or a client
    highlights the wrong input. Creation sends `status`; the transition
    endpoint sends `to_status`."""
    with pytest.raises(RequestValidationError) as excinfo:
        validate_ticket_status("epic", "agent_ready")
    assert excinfo.value.errors()[0]["loc"] == ("body", "status")

    with pytest.raises(RequestValidationError) as excinfo:
        validate_ticket_status("epic", "agent_ready", field="to_status")
    assert excinfo.value.errors()[0]["loc"] == ("body", "to_status")


def test_parent_rank_still_raises_invalid_reference():
    """Deliberately NOT moved to 422 alongside the status check: rank is a
    cross-row consistency question about another ticket, which is what
    invalid_reference means, rather than a value out of range."""
    with pytest.raises(InvalidReferenceError):
        validate_parent_rank("bug", "epic")


def test_board_columns_match_the_status_vocabulary():
    """ALL_STATUS_ORDER and TicketStatus are written separately and must stay
    equal.

    A status added to TicketStatus alone becomes unreachable on the board —
    and before PR #20's fix, a ticket carrying it made the whole board 500
    with a bare KeyError. A column added to ALL_STATUS_ORDER alone renders an
    empty column no ticket can ever occupy.
    """
    from typing import get_args

    from api.jyra import ALL_STATUS_ORDER
    from api.models import TicketStatus

    assert set(ALL_STATUS_ORDER) == set(get_args(TicketStatus))
    assert len(ALL_STATUS_ORDER) == len(set(ALL_STATUS_ORDER)), "duplicate column"


def test_unknown_ticket_type_raises_keyerror_not_a_confusing_error():
    """Both validators index a dict by ticket type, so an unknown type is a
    KeyError by design — the documented precondition, matching
    validate_entity_attributes. Pinned so it is a deliberate contract rather
    than something that looks like an oversight."""
    with pytest.raises(KeyError):
        validate_ticket_status("spike", "backlog")
    with pytest.raises(KeyError):
        validate_parent_rank("spike", "task")
