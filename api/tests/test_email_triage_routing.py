"""The email-triage routing rules, executed rather than read.

The classifier is EXTRACTED from `plugin/skills/email-triage/SKILL.md`,
not retyped here, so this tests the text a future session will actually
follow. Retyping it would produce a test that passes while the skill
says something else — which is the failure the extraction pattern in
`api/tests/test_skill_preview_parity.py` exists to prevent.

No teammate session has the Gmail connector, so the deliverable is the
routing rules plus this dry run. That makes the fixture load-bearing:
the adversarial cases are the reason the file exists, not decoration.
"""

import json
import re
from datetime import date
from pathlib import Path

import pytest

from api.config import REPO_ROOT

SKILL = REPO_ROOT / "plugin" / "skills" / "email-triage" / "SKILL.md"
FIXTURES = REPO_ROOT / "plugin" / "skills" / "email-triage" / "fixtures.json"


def _route_source() -> str:
    blocks = re.findall(r"```python\n(.*?)```", SKILL.read_text(), re.S)
    matching = [block for block in blocks if "def route(" in block]
    assert len(matching) == 1, (
        f"expected exactly one routing fence in {SKILL.name}, found "
        f"{len(matching)}"
    )
    return matching[0]


def _route_function():
    blocks = re.findall(r"```python\n(.*?)```", SKILL.read_text(), re.S)
    matching = [block for block in blocks if "def route(" in block]
    assert len(matching) == 1, (
        f"expected exactly one routing fence in {SKILL.name}, found "
        f"{len(matching)} — the test executes the fence, so two would mean "
        f"it silently checks only the first"
    )
    namespace: dict = {}
    exec(matching[0], namespace)
    return namespace["route"]


#: Routes that do NOT write. Everything else does, and that direction is
#: deliberate: a route added to the fence is treated as a write until
#: someone exempts it here, so the unknown case is guarded rather than
#: ignored. `ask` reports and `ignore` counts; neither touches anything.
NON_WRITE_ROUTES = frozenset({"ask", "ignore"})


def _declared_routes() -> set[str]:
    """The routes the fence itself declares, read out of its docstring.

    The fence opens `route()` with a one-line docstring reading
    `One of: statement | document | renewal | ask | ignore.` and that
    line is the source here.

    Extracted rather than retyped. The previous version of the test
    below carried `writes = {...}` as a literal beside a docstring
    claiming it was derived — the exact defect it was written to fix,
    reproduced one paragraph later (review-1 on #97). Knowing the
    failure mode by name did not prevent repeating it, which is why
    this is mechanical rather than a matter of care.
    """
    source = _route_source()
    match = re.search(r'"""One of:\s*([^."]+)\.', source)
    assert match, (
        "the fence no longer declares its routes as "
        '`"""One of: a | b | c."""` — fix the anchor rather than '
        "replacing this with a literal, which is what it exists to avoid"
    )
    routes = {part.strip() for part in match.group(1).split("|")}
    assert len(routes) >= 3, f"suspiciously few routes parsed: {routes}"
    return routes


def _fixtures() -> dict:
    return json.loads(FIXTURES.read_text())


def _today(data) -> date:
    return date.fromisoformat(data["today"])


def _known(data) -> set:
    return {sender.lower() for sender in data["known_senders"]}


def _case_ids(data) -> list[str]:
    return [case["id"] for case in data["cases"]]


@pytest.mark.parametrize("case_id", _case_ids(_fixtures()))
def test_each_fixture_routes_as_documented(case_id):
    data = _fixtures()
    case = next(c for c in data["cases"] if c["id"] == case_id)
    route = _route_function()

    actual = route(case["email"], _known(data), _today(data))

    assert actual == case["expected"], (
        f"{case_id}: expected {case['expected']}, got {actual}. {case['why']}"
    )


def test_the_fixture_exercises_every_route_the_skill_documents():
    """A fixture that never produces `ask` would let the whole
    unknown-sender branch rot untested while every case still passed."""
    data = _fixtures()
    route = _route_function()
    produced = {
        route(case["email"], _known(data), _today(data)) for case in data["cases"]
    }

    assert produced == {"statement", "document", "renewal", "ask", "ignore"}


def test_an_unknown_sender_never_produces_a_write_route():
    """The property the whole skill is built around, stated as a
    property rather than as the three cases that happen to cover it.

    `statement`, `document` and `renewal` all end in something being
    written or proposed. None of them may be reachable from a sender
    that matches no account entity — otherwise a stranger's message
    decides what goes into the owner's records.

    `document` was missing from this set while the sentence above
    already named it (review-1 on #97), and the fall-through in the
    fence had no sender check to match — so a stranger emailing any
    `.pdf` got it saved into `inbox/`.

    The set is now computed from the fence's own declared routes minus
    the two that write nothing, so a route added to the fence is
    covered here the moment it exists. Adding one and not exempting it
    makes this stricter, never blinder.
    """
    data = _fixtures()
    route = _route_function()
    writes = _declared_routes() - NON_WRITE_ROUTES

    for case in data["cases"]:
        email = dict(case["email"], sender="stranger@nowhere.test")
        assert route(email, _known(data), _today(data)) not in writes, case["id"]


def test_no_date_written_is_never_a_renewal():
    """An inferred date lands in an entity attribute that /due later
    reports as fact. Stripping the date from every renewal case must
    leave none of them proposing one."""
    data = _fixtures()
    route = _route_function()
    iso = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")

    for case in data["cases"]:
        email = dict(
            case["email"],
            subject=iso.sub("", case["email"]["subject"]),
            snippet=iso.sub("", case["email"].get("snippet", "")),
        )
        assert route(email, _known(data), _today(data)) != "renewal", case["id"]


def test_a_past_date_is_never_a_renewal():
    """`/due` is the authority on overdue and it reads the entity, which
    is the record the owner controls. Mail must not decide what looks
    overdue."""
    data = _fixtures()
    route = _route_function()
    far_future = date(2099, 1, 1)

    for case in data["cases"]:
        # Every fixture date is now in the past relative to `today`.
        assert route(case["email"], _known(data), far_future) != "renewal", case["id"]


def test_an_attachment_is_never_routed_from_the_subject_alone():
    """Routing on the subject would hand off a file that does not
    exist. Removing the attachments must remove every hand-off."""
    data = _fixtures()
    route = _route_function()

    for case in data["cases"]:
        email = dict(case["email"], attachments=[])
        assert route(email, _known(data), _today(data)) not in {
            "statement",
            "document",
        }, case["id"]


def test_the_fixture_contains_no_real_looking_identifiers():
    """The fixture is fabricated and must stay that way. A real address
    or a real account number in a committed file is a leak that no
    later redaction undoes.
    """
    raw = FIXTURES.read_text()
    data = _fixtures()

    for case in data["cases"]:
        assert case["email"]["sender"].endswith(".test") or case["email"][
            "sender"
        ] == "[REDACTED]", case["id"]
    for sender in data["known_senders"]:
        assert sender.endswith(".test"), sender
    # Any run of 7+ digits would be account-number shaped.
    assert not re.search(r"\d{7,}", raw), "fixture contains a long digit run"


def test_every_fixture_case_says_why_it_is_there():
    """A fixture case with no rationale becomes a case nobody dares
    change, and then a case nobody can evaluate."""
    for case in _fixtures()["cases"]:
        assert case.get("why", "").strip(), case["id"]


def test_the_write_set_is_read_from_the_fence_not_retyped():
    """Pins the derivation itself.

    A literal here would pass every other test in this file while
    silently omitting a route — which is precisely what happened, and
    what the docstring above wrongly claimed had been fixed.
    """
    assert _declared_routes() == {
        "statement",
        "document",
        "renewal",
        "ask",
        "ignore",
    }
    assert _declared_routes() - NON_WRITE_ROUTES == {
        "statement",
        "document",
        "renewal",
    }


def test_a_route_added_to_the_fence_counts_as_a_write_until_exempted():
    """The direction that matters: unknown means guarded.

    Simulated on the extracted text rather than by editing the skill,
    so the assertion is about the derivation's behaviour and not about
    today's route list.
    """
    declared = _declared_routes() | {"forward"}

    assert "forward" in declared - NON_WRITE_ROUTES
