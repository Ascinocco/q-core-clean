from api.errors import ConflictError, InvalidReferenceError, NotFoundError


def test_not_found_error_has_correct_status_and_envelope():
    exc = NotFoundError("No entity with id 'abc123'")

    assert exc.status_code == 404
    assert exc.detail == {
        "error": {"code": "not_found", "message": "No entity with id 'abc123'"}
    }


def test_invalid_reference_error_has_correct_status_and_envelope():
    exc = InvalidReferenceError("No entity with id 'xyz'")

    assert exc.status_code == 400
    assert exc.detail == {
        "error": {"code": "invalid_reference", "message": "No entity with id 'xyz'"}
    }


def test_conflict_error_has_correct_status_and_envelope():
    exc = ConflictError("Cannot delete entity 'abc123': still referenced")

    assert exc.status_code == 409
    assert exc.detail == {
        "error": {
            "code": "conflict",
            "message": "Cannot delete entity 'abc123': still referenced",
        }
    }


def test_the_three_rejection_codes_mean_three_different_things():
    """Stated together, because the distinction only holds side by side.

    Two of these were defined apart and 400 came to mean both "refers to
    something that does not exist" and "conflicts with something that
    does" — which is how a reader ends up opening the source to tell
    which a 400 is. Written as one assertion so the next person changing
    any of them sees all of them.

      400 InvalidReferenceError  — refers to something that DOES NOT EXIST
      409 ConflictError          — conflicts with something that DOES
      422 RequestValidationError — a value outside its permitted range

    422 is not constructed here: it is FastAPI's, raised by the models
    and by the explicit `RequestValidationError`s in the routes. It is
    named so the set is complete rather than so it can be asserted — a
    note listing two of three is what allowed the collision.
    """
    assert InvalidReferenceError("x").status_code == 400
    assert ConflictError("x").status_code == 409
    assert NotFoundError("x").status_code == 404

    codes = {
        InvalidReferenceError("x").status_code,
        ConflictError("x").status_code,
        NotFoundError("x").status_code,
    }
    assert len(codes) == 3, (
        "two rejection classes share a status code, so a caller cannot "
        "tell them apart without reading the message"
    )


# --- no conflict site may answer 400 (ticket T-33) ---------------------------

from pathlib import Path

from api.config import REPO_ROOT

#: Every module that raises ConflictError. A literal, cross-checked
#: against discovery below rather than trusted — an enumeration that
#: silently stops matching is the failure this whole ticket is about.
CONFLICT_MODULES = ("artifacts.py", "review_inbox.py", "birthdays.py", "reminders.py", "documents.py", "entities.py", "financial.py", "financial_corrections.py", "forecast.py", "jyra.py", "source_imports.py", "tokens.py")

#: Raise sites, counted. Was eight (the ticket estimated seven -- the
#: two in `apply_transaction_as_rule` read easily as one). Nine since
#: #120 added the note-links guard to delete_entity.
#:
#: This number failing on a legitimate addition is the SUCCESS case,
#: and it worked on the first new site after it was written: #120
#: landed, this went red, and the review that followed found its test
#: asserting `400 <= code < 500` rather than 409 -- a site that
#: existed, was exercised, and pinned nothing about what it answered.
# Transition history CAS is covered by test_transition_precondition.
# + api/jyra.py key prefix taken or retired, and a prefix change on a board
# that has had a ticket (test_jyra_keys).
# + api/artifacts.py edit_artifact: diagram refusal and stale revision (test_artifact_edit M7, M4)
# + api/artifacts.py edit_diagram: an operation's 409, not a diagram, a revision from the future, stale (test_canvas_diagram_edit_api)
# - 1: PUT, edit_artifact and edit_diagram share write_revision's three stale sites (ticket T-03, test_artifact_stale_first)
EXPECTED_CONFLICT_SITES = 46  # + api/tokens.py duplicate active name, and its insert race (test_api_tokens)


def _api_modules() -> list[Path]:
    return sorted((REPO_ROOT / "api").glob("*.py"))


def _conflict_sites() -> list[tuple[str, int]]:
    sites = []
    for path in _api_modules():
        for number, line in enumerate(path.read_text().split("\n"), 1):
            if "raise ConflictError" in line:
                sites.append((path.name, number))
    return sites


def test_the_conflict_site_discovery_finds_the_modules_it_should():
    """Guards the guard, the way the skill-conventions pin does.

    If the glob stopped matching, every assertion below would pass over
    an empty list and report the rule enforced across nothing. Checked
    against the named modules rather than a count, because two ways of
    naming the same set disagree loudly and a count does not.
    """
    discovered = {p.name for p in _api_modules()}

    assert discovered, "no api modules found at all"
    assert set(CONFLICT_MODULES) <= discovered, set(CONFLICT_MODULES) - discovered


def test_every_conflict_site_is_accounted_for():
    sites = _conflict_sites()

    assert len(sites) == EXPECTED_CONFLICT_SITES, (
        f"raise sites moved: {sites}. Update EXPECTED_CONFLICT_SITES and "
        f"check the new site asserts 409 in its own behavioural test — "
        f"the count is here so a new site cannot arrive unnoticed."
    )
    assert {name for name, _ in sites} == set(CONFLICT_MODULES)


def test_no_conflict_is_raised_as_a_bare_400():
    """The negative, read from source.

    `ConflictError` carries 409 now, so a conflict answered with 400
    would have to be a hand-rolled HTTPException standing in for one.
    That is how 400 came to mean two things the first time: not by
    anyone deciding it should, but by the nearest example being copied.
    """
    offenders = []
    for path in _api_modules():
        lines = path.read_text().split("\n")
        for number, line in enumerate(lines, 1):
            if "status_code=400" not in line:
                continue
            window = "\n".join(lines[max(0, number - 6) : number + 2]).lower()
            if "conflict" in window:
                offenders.append(f"{path.name}:{number}")

    assert offenders == [], (
        f"conflict-shaped rejections answering 400: {offenders}. A "
        f"conflict is 409 (see ConflictError); 400 means the request "
        f"referred to something that does not exist."
    )
