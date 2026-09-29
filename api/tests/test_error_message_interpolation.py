"""What an error MESSAGE is allowed to quote back to the caller.

`_error_detail` (api/main.py) drops pydantic's `input` key, and its
docstring says why: the rejected value "can be a mistyped account number
or date of birth", and an error body is an easy way to leak one into
logs and MCP transcripts. That decision covers exactly one channel.
A hand-built message that interpolates the same value into its own text
reaches the caller through `msg`, which nothing was checking -- and
three sites did, measured against the running API:

    period_start "123-45-6789"
      -> "expected a calendar date (YYYY-MM-DD), got '123-45-6789' ..."
    recurrence_rule "FREQ=WEEKLY;BYDAY=123-45-6789"
      -> "... not a usable RRULE: invalid 'BYDAY': 123-45-6789"
    a merchant-rule collision
      -> quoted the transaction's DESCRIPTION, which is bank free text
         and is what the scrubber redacts

IDS AND ENUM VALUES STAY. An id is not a secret and is the whole
diagnostic -- "No entity with id 'abc'" is useless without the id. An
enum value the caller sent back is bounded by the enum. The line is
free text a caller controls, where the permitted content is anything.

This test does not judge; it makes the judgement EXPLICIT. Every
interpolation is classified below, so adding one is a decision someone
records rather than a diff nobody reads.
"""

from __future__ import annotations

from tests.source_import_support import seed_source_batch

import ast
import pathlib

API = pathlib.Path(__file__).resolve().parent.parent

#: NO LIST OF ERROR CLASSES. An earlier version of this file curated one
#: -- ConflictError, NotFoundError, ValueError and the rest -- and it
#: missed FIVE classes that are actually raised with an f-string, among
#: them UnscrubbedDigitsError, which guards the single most sensitive
#: message in the repo. A curated list of names is the same shape this
#: guard exists to replace, one level up: it decides what to look at by
#: recognising a name, so anything named something else is invisible.
#: Every `raise <anything>(f"...")` is in scope instead, and the cost of
#: that is a handful more rows to classify, which is the point.


#: Each interpolation, keyed "<module>:<expression>", with WHY it is safe
#: to echo. The value is the classification, and it is the point of the
#: mapping -- a bare set would let someone add a name without saying what
#: it is.
#:
#: KEYED BY MODULE, not by the expression alone, and that is load-bearing.
#: One name can be an id in one file and free text in another: `value` is
#: `_require_reference`'s id argument in financial.py, and was the
#: rejected raw string in models.py's date validator. With a flat set,
#: classifying the safe one silently permitted the unsafe one -- verified
#: by mutation, which the flat version did not catch.
ALLOWED: dict[str, str] = {
    "run.py:directory": "operator configuration (serve_socket's parent) at startup, never caller input",
    "review_inbox.py:item_id": "parsed UUID query reference, never free text",
    # -- ids: not secrets, and the whole diagnostic value of the message
    "documents.py:document_id": "id",
    "documents.py:body.entity_id": "id",
    "entities.py:entity_id": "id",
    "entities.py:relationship_id": "id",
    "entities.py:body.to_entity_id": "id",
    "entities.py:existing['id']": "id",
    "financial.py:account_id": "id",
    "financial.py:body.account_id": "id",
    "financial.py:category_id": "id",
    "financial.py:entity_id": "id",
    "financial.py:existing['id']": "id",
    "financial.py:parent_id": "id",
    "financial.py:rule_id": "id",
    "financial.py:statement_id": "id",
    "financial.py:transaction_id": "id",
    # `require_reference` moved from financial.py to db.py in #151, and
    # the module key correctly re-opened the question rather than
    # carrying the old answer across. Same expressions, same answers,
    # asked again where they now live.
    "db.py:value": "id -- require_reference's id argument",
    "db.py:label": "our own literal, never caller-supplied",
    "jyra.py:attachment_id": "id",
    "jyra.py:board_id": "id",
    "jyra.py:body.board_id": "id",
    "jyra.py:entity_id": "id",
    "jyra.py:prefix": "id",
    "jyra.py:row['id']": "id",
    "jyra.py:ticket_id": "id",
    "notes.py:archived": "fixed literal chosen from TARGET_TABLES, no caller text",
    "notes.py:link_id": "id",
    "notes.py:note_id": "id",
    "notes.py:target_id": "id",
    "notes.py:target_type": "enum member (NoteTargetType Literal)",
    "canvas_diagram_ops.py:problem": "A7 validation text: a JSON path and a path-only message that names no id values (ticket T-04, pinned by test_canvas_diagram_id_echo), dumped with include_input=False",
    "artifact_links.py:target_type": "enum member (ArtifactLinkCreate.target_type Literal)",
    "artifact_links.py:target_id": "id",
    "artifact_links.py:artifact_id": "id (UUID-validated path parameter)",
    "artifact_links.py:link_id": "id",
    "artifacts.py:kind": "enum member (ArtifactCreate.kind Literal, or the stored kind the schema CHECK bounds)",
    "reminders.py:entity_id": "id",
    "reminders.py:reminder_id": "id",
    # -- a caller-controlled KEY, not a value. The one judgement in this
    #    table rather than an obvious id or enum, so it is written out.
    #    `_name_permitted_keys` rewrites "Extra inputs are not permitted"
    #    into "'x' is not an attribute of a vehicle", echoing the
    #    unrecognised attribute NAME the caller sent. It sets
    #    "input": "[omitted]" in the same breath, so the VALUE under that
    #    key never travels. A key is what the caller must correct and the
    #    message is useless without it; the realistic content of one is an
    #    identifier they guessed. A pathological caller could put digits
    #    in the key itself -- accepted as the cost of a usable error, and
    #    recorded here so it is a decision rather than an oversight.
    "models.py:key": "caller-controlled key name; its value is omitted",
    # -- enum members and type names: bounded by the enum itself
    "models.py:entity_type": "enum",
    "financial.py:row['type']": "enum",
    "jyra.py:row['status']": "enum",
    "models.py:child_type": "enum",
    "models.py:parent_type": "enum",
    "models.py:status": "enum",
    "models.py:ticket_type": "enum",
    # -- dates, already parsed to date objects, so they render ISO and
    #    cannot carry arbitrary text
    "due.py:from_date": "parsed date",
    # resolve_occurrence's refusals (#136). `requested` is ReminderInstance
    # Create.due_date, which pydantic has already parsed to a `date`, so
    # it renders ISO and cannot carry arbitrary text -- and it is the
    # diagnostic: a refusal that will not say which date it refused is
    # not usable. `listed` and `detail` are built from the same parsed
    # dates plus this module's own literals.
    "due.py:requested.isoformat()": "parsed date",
    "due.py:listed": "parsed dates, joined",
    "due.py:detail": "assembled from parsed dates and our own literals",
    "due.py:len(candidates)": "a count",
    "due.py:to_date": "parsed date",
    "financial.py:body.period_end": "parsed date",
    "financial.py:body.period_start": "parsed date",
    # Declared `date | None` on the route, so FastAPI has parsed them
    # before the reversed-range refusal can fire -- checked, not assumed:
    # a string that is not a date never reaches this message, it is
    # refused earlier by the type.
    "financial.py:date_from": "parsed date",
    "financial.py:date_to": "parsed date",
    "models.py:period_end": "parsed date",
    "models.py:period_start": "parsed date",
    "models.py:self.due_date": "parsed date",
    "models.py:self.end_date": "parsed date",
    "models.py:self.snoozed_to": "parsed date",
    "models.py:self.start_date": "parsed date",
    # -- reached only once the curated class list was dropped. All of
    #    them are this process describing itself, not the caller.
    "db.py:_test_writable_root()": "our own configured path",
    "db.py:current_test": "the running test's name",
    "db.py:data_dir": "our own configured path",
    # db_file and db_path are the same value under two names, and both
    # are quoted by messages raised at STARTUP -- the operator's terminal
    # and data/logs/api.log, never a model's context. That audience is
    # why they may carry a path at all, and it is the same reason the
    # attachment refusals elsewhere quote neither a path nor the digits
    # they found: those travel to an LLM, these do not.
    #
    # TWO things keep it that way, and the second is the durable one.
    # `_transactions_amount_problem` is reached only from
    # `apply_startup_migrations`, which only the lifespan calls -- its
    # firing is the condition under which no request is served at all.
    # AND `_envelope_response` already handles DatabaseNotUsableError by
    # substituting the fixed DATABASE_UNUSABLE_MESSAGE rather than
    # echoing `str(exc)`, so even reached from a request path the message
    # itself would not travel.
    #
    # REVISIT IF EVER RENDERED INTO A RESPONSE. The trigger is not
    # "someone adds a handler" -- one exists. It is someone making that
    # handler echo the exception, or routing this probe somewhere a
    # different handler does. Meet the condition; do not inherit the
    # verdict.
    "db.py:db_file": "our own configured path; startup-only, revisit if ever rendered into a response",
    "db.py:db_path": "our own configured path",
    "db.py:what": "our own literal describing the operation",
    "db.py:AMOUNT_CENTS_CONVERSION": "constant",
    "jyra.py:MAX_ATTACHMENT_BYTES": "constant",
    "jyra.py:KEY_PREFIX_CHANGE_CONTRACT": "constant",
    "jyra.py:len(payload)": "a byte count",
    "main.py:name": "an attribute name on our own client object",
    "main.py:type(client).__name__": "a class name",
    "main.py:type(self).__name__": "a class name",
    # -- THE one message whose subject IS a secret. It names the LOCATION
    #    of the digits it refuses and never the digits, which
    #    test_the_unscrubbed_digits_refusal_names_the_offset_not_the_digits
    #    pins directly -- the classification alone would not be enough
    #    here, because "offset" being the only safe thing to splice is a
    #    property of that message, not of the name.
    "redaction.py:offset": "a character offset, never the digits",
    # -- counts and limits computed here, never caller text
    "db.py:MAX_LIMIT": "constant",
    "db.py:limit": "number",
    "db.py:offset": "number",
    "jyra.py:children": "count",
    "jyra.py:remaining": "count",
    # -- names of FIELDS and other literals from this codebase, not values
    "due.py:', '.join(SOURCES)": "our own vocabulary",
    "documents.py:', '.join(DOC_TYPES)": "our own vocabulary (the doc_type enum)",
    "entities.py:', '.join(differing)": "field names",
    "entities.py:ARCHIVE_HINT": "our own constant",
    "entities.py:message": "assembled from table names and ids, above",
    "models.py:', '.join(unclearable)": "field names",
    "models.py:'an' if ticket_type[0] in 'aeiou' else 'a'": "an article",
    "models.py:clearable": "field names",
    "models.py:listed": "field names",
    "models.py:permitted": "field names of the attribute model",
    # -- canvas_diagram.py: refusals name a JSON path, never the value (A7)
    "canvas_diagram.py:path": "a JSON path built from model field names and list indices",
    "canvas_diagram.py:message": "one of the module's fixed refusal constants",
    "canvas_diagram.py:key": "a model field name: the document was validated with extra='forbid' first",
}


def _interpolations() -> dict[str, list[str]]:
    """expression source -> where it appears. Discovered, never listed."""
    found: dict[str, list[str]] = {}

    def record(expr_node, src, path, lineno):
        text = ast.get_source_segment(src, expr_node) or "<unparsed>"
        found.setdefault(f"{path.name}:{text}", []).append(f"{path}:{lineno}")

    for path in sorted(API.rglob("*.py")):
        if "tests" in path.parts:
            continue
        src = path.read_text()
        tree = ast.parse(src)
        rel = path.relative_to(API.parent)
        for node in ast.walk(tree):
            # raise SomeError(f"... {x} ...")
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                for inner in ast.walk(node.exc):
                    if isinstance(inner, ast.JoinedStr):
                        for piece in inner.values:
                            if isinstance(piece, ast.FormattedValue):
                                record(piece.value, src, rel, node.lineno)
            # {"msg": f"... {x} ..."} -- RequestValidationError's details
            if isinstance(node, ast.Dict):
                for key, val in zip(node.keys, node.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "msg"
                        and isinstance(val, ast.JoinedStr)
                    ):
                        for piece in val.values:
                            if isinstance(piece, ast.FormattedValue):
                                record(piece.value, src, rel, key.lineno)
    return found


def test_the_discovery_finds_both_shapes_it_is_looking_for():
    """The vacuity guard, and it is not a count.

    A floor absorbs a regression once the population grows past it --
    which has happened in this repo. What this asserts instead is that
    each of the TWO syntactic forms the walk handles is actually being
    found: a walk that silently stopped matching `raise` calls, or
    stopped matching the `"msg"` dicts, would still return plenty of
    the other and satisfy any threshold.
    """
    found = _interpolations()
    assert found, "the walk found no interpolations at all -- it is broken"

    # A raise-site canary and a "msg"-dict canary, each from a different
    # module, so one file being skipped does not hide the other form.
    assert "entities.py:entity_id" in found, (
        "no `raise SomeError(f'... {entity_id} ...')` was found -- the "
        "raise-site half of the walk has stopped matching"
    )
    assert "db.py:MAX_LIMIT" in found, (
        "no {'msg': f'...'} interpolation was found -- the "
        "RequestValidationError half of the walk has stopped matching"
    )


def test_every_interpolated_value_is_one_someone_classified():
    """The guard itself.

    A new interpolation fails here until it is written into ALLOWED with
    a reason. That is the whole mechanism: the question "could this field
    hold something that must not be echoed?" gets asked once, by the
    person adding it, instead of never.
    """
    found = _interpolations()
    unclassified = sorted(set(found) - set(ALLOWED))
    assert not unclassified, (
        "error messages interpolate values nobody has classified:\n"
        + "\n".join(f"  {expr}  at {', '.join(found[expr])}" for expr in unclassified)
        + "\n\nIs the value free text the caller controls? Then name the "
        "field instead of echoing it. Is it an id, an enum member, a "
        "parsed date or a count? Add it to ALLOWED with that reason."
    )


def test_the_allowed_list_has_not_gone_stale():
    """The other direction, so the classification stays a description.

    Without this, ALLOWED only ever grows: a site that is deleted or
    de-interpolated leaves its entry behind, and the next reader takes a
    list of things the code no longer does as a description of what it
    does.
    """
    found = _interpolations()
    stale = sorted(set(ALLOWED) - set(found))
    assert not stale, (
        f"ALLOWED lists interpolations that no longer exist: {stale}. "
        "Remove them -- an allow-list that outlives its code stops "
        "describing anything."
    )


# --- the three sites this swept, pinned by behaviour ----------------------

SECRET_SHAPED = "123-45-6789"


def test_a_misaimed_value_in_a_date_field_is_not_echoed_back(
    client, test_settings
):
    """A date field is where a misaimed paste lands.

    Measured before the fix: period_start "123-45-6789" came back inside
    the message in full. `_error_detail` drops pydantic's `input` for
    precisely this reason and the hand-built message put it back.
    """
    from api.tests.test_financial import _account, _auth

    headers = _auth(test_settings)
    account = _account(client, headers)
    response = client.post(
        "/statements",
        json={
            "account_id": account["id"],
            "period_start": SECRET_SHAPED,
            "period_end": "2026-09-30",
        },
        headers=headers,
    )

    assert response.status_code == 422
    body = response.text
    assert SECRET_SHAPED not in body, (
        "the rejected value was echoed back into the error body: " + body
    )
    # Still diagnostic: the caller is told which field and what shape.
    detail = response.json()["error"]["details"][0]
    assert detail["field"] == "period_start"
    assert "YYYY-MM-DD" in detail["message"]


def test_a_bad_recurrence_rule_does_not_echo_the_rule_back(
    client, test_settings
):
    """dateutil quotes the caller's own string in its exception.

    Measured: "FREQ=WEEKLY;BYDAY=123-45-6789" produced "invalid 'BYDAY':
    123-45-6789", and forwarding that message forwarded the value with
    it. recurrence_rule is free text, so the parser's message stays in
    the log and the response names the field.
    """
    from api.tests.test_financial import _auth

    response = client.post(
        "/reminders",
        json={
            "title": "probe",
            "start_date": "2026-10-01",
            "recurrence_rule": f"FREQ=WEEKLY;BYDAY={SECRET_SHAPED}",
        },
        headers=_auth(test_settings),
    )

    assert response.status_code == 422
    body = response.text
    assert SECRET_SHAPED not in body, (
        "the caller's recurrence_rule was echoed back through dateutil's "
        "message: " + body
    )
    assert "RRULE" in body, "the caller still needs to know what was wrong"


def test_a_duplicate_merchant_rule_does_not_echo_the_description(
    client, test_settings
):
    """The one site where the echoed value was not even the caller's input.

    They send a transaction_id; the pattern is that transaction's
    description, read from the database. A statement description is bank
    free text and is what the scrubber redacts, so repeating one here
    would undo the redaction for the rows where it mattered.
    """
    from api.tests.test_financial import _account, _auth

    headers = _auth(test_settings)
    account = _account(client, headers)
    description = f"MYSTERY MERCHANT {SECRET_SHAPED}"
    imported = seed_source_batch(client, json={
            "account_id": account["id"],
            "period_start": "2026-09-01",
            "period_end": "2026-09-30",
            "transactions": [
                {
                    "txn_date": "2026-09-02",
                    "description": "MYSTERY MERCHANT",
                    "amount_cents": -900,
                }
            ],
    }, headers=headers).json()
    txn_id = imported["unmatched"][0]["id"]
    # Simulate a row written before the model-visible store guard existed.
    # New API writes correctly refuse this value, but legacy rows must not
    # leak it through a later conflict response.
    import sqlite3
    connection = sqlite3.connect(test_settings.db_path)
    connection.execute(
        "UPDATE transactions SET description = ? WHERE id = ?",
        (description, txn_id),
    )
    connection.commit()
    connection.close()
    client.patch(
        f"/transactions/{txn_id}", json={"category_id": "food"}, headers=headers
    )
    first = client.post(
        f"/transactions/{txn_id}/apply_as_rule",
        json={"actor": "test"},
        headers=headers,
    )
    second = client.post(
        f"/transactions/{txn_id}/apply_as_rule",
        json={"actor": "test"},
        headers=headers,
    )

    assert first.status_code == 200
    assert second.status_code == 409
    message = second.json()["error"]["message"]
    assert description not in message, (
        "the transaction description was echoed into the conflict: " + message
    )
    assert SECRET_SHAPED not in message
    # The rule id is kept: it is what the caller needs in order to edit it.
    assert first.json()["id"] in message


def test_the_unscrubbed_digits_refusal_names_the_offset_not_the_digits():
    """The one message whose subject IS a secret.

    Written by impl-1 for #142 and taken into this PR on the lead's
    ruling, so one rule has one guard. Their sweep and mine crossed;
    this pin is the part of theirs that mine did not have, and my
    classification table alone would not replace it: "offset is the only
    safe thing to splice here" is a property of THIS message, not of the
    name `offset`.

    If it quoted the number it is refusing, the refusal would perform
    the leak it exists to prevent -- and it would do so in the one code
    path guaranteed to be handling a real account number.
    """
    source = (API / "redaction.py").read_text()
    tree = ast.parse(source)

    # The INTERPOLATED expressions only, never the literal text. The
    # message says "The value is deliberately not quoted here", so a
    # substring search over the whole f-string matches the word "value"
    # in the very sentence promising not to quote one -- which is how
    # impl-1's version of this test failed on its first run, against
    # correct code. It is the same failure as the surviving mutation in
    # #141 and the one in this file's own history: a check reading
    # through the distinction it exists to make.
    spliced = [
        ast.unparse(part.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise) and node.exc is not None
        and "UnscrubbedDigits" in ast.unparse(node.exc)
        for sub in ast.walk(node.exc)
        if isinstance(sub, ast.JoinedStr)
        for part in ast.walk(sub)
        if isinstance(part, ast.FormattedValue)
    ]
    assert spliced, "no UnscrubbedDigitsError interpolation found -- walk broke"
    assert set(spliced) == {"offset"}, (
        f"the unscrubbed-digits refusal splices {spliced}, and the only "
        f"safe thing to name there is the location: quoting the number "
        f"would perform the leak the refusal exists to prevent, in the "
        f"one path guaranteed to be handling a real account number."
    )
