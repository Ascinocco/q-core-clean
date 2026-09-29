"""`_error_detail` must drop Pydantic's `input` for EVERY error type (ticket T-37).

This is the only thing standing between a caller-invented field name and
its value in a response, and until now nothing pinned it.

HOW THAT BECAME TRUE. `_redact_sensitive_inputs` masks a validation
error's `input` by KEY NAME, against a set of names known to be sensitive
-- so an unrecognised key is by construction not on that list, and
`{"ssn": "123-45-6789"}` produced an error object holding the number.
That never reached a response, because `_error_detail` drops `input`
wholesale when building the envelope. Two independent layers, and only
the outer one is load-bearing for the guarantee.

I found the inner gap and reported it as a live leak without reading the
outer layer (D119, corrected). The lasting lesson is the one that applies
here: **checking a layer tells you nothing about the layers downstream of
it, including whether they already handle what you found.** The
corollary is what this file is: when a guarantee rests on one layer, pin
THAT layer, because the redundancy that saved us is not redundancy if
nothing checks it.

Derived from `pydantic_core.list_all_errors()` -- every error type the
library can produce, not a list someone maintains here. A new Pydantic
version adding an error type is covered without anyone noticing it
happened.
"""

from __future__ import annotations

import pytest
from pydantic_core._pydantic_core import list_all_errors

from api.main import _error_detail

#: Every error type pydantic-core can emit, straight from the library.
ERROR_TYPES = sorted(info["type"] for info in list_all_errors())

#: A value no rendering should ever echo. Distinctive enough that a
#: substring search cannot match it by accident.
SECRET = "123-45-6789-SECRET"  # gitleaks:allow (invented fixture, not a credential)


def test_the_error_type_set_is_not_empty():
    """Guards the guard: an empty registry would make every parametrized
    case below vanish, and pytest turns an empty parametrize into a SKIP,
    which reads as deliberate."""
    assert len(ERROR_TYPES) > 50, f"only {len(ERROR_TYPES)} error types found"
    assert "extra_forbidden" in ERROR_TYPES
    assert "json_invalid" in ERROR_TYPES


@pytest.mark.parametrize("error_type", ERROR_TYPES)
def test_error_detail_drops_input_and_url(error_type):
    """No error type may carry the rejected value into the rendered detail.

    The `input` is the caller's own value. For a field name the caller
    invented, no redaction keyed on names can know it is sensitive, so
    the only safe rendering is not to include it at all.
    """
    rendered = _error_detail(
        {
            "type": error_type,
            "loc": ("body", "attributes", "ssn"),
            "msg": "Something was wrong",
            "input": SECRET,
            "url": "https://errors.pydantic.dev/2.13/v/" + error_type,
        }
    )

    assert "input" not in rendered, (error_type, rendered)
    assert "url" not in rendered, (error_type, rendered)
    assert SECRET not in str(rendered), (error_type, rendered)


@pytest.mark.parametrize("error_type", ERROR_TYPES)
def test_error_detail_still_says_what_and_where(error_type):
    """Dropping the value must not drop the diagnosis.

    A detail with no field and no message is safe and useless, and would
    pass the test above. The caller needs to know which field and what
    was wrong with it -- neither of which is the value.
    """
    rendered = _error_detail(
        {
            "type": error_type,
            "loc": ("body", "attributes", "ssn"),
            "msg": "Something was wrong",
            "input": SECRET,
        }
    )

    assert rendered.get("field"), (error_type, rendered)
    assert rendered.get("message"), (error_type, rendered)


def test_a_secret_in_the_message_itself_is_not_covered_here():
    """Stating the limit rather than implying coverage it does not have.

    `_error_detail` keeps `msg`. Pydantic's own messages do not quote the
    input, but a message this repo writes could -- `_name_permitted_keys`
    puts the KEY name in `msg` deliberately, and a future message that
    interpolated a VALUE would pass every test above.

    So this asserts the one thing that makes that safe today: the
    rendered message is Pydantic's `msg`, passed through unchanged, and
    nothing here constructs it from `input`.
    """
    rendered = _error_detail(
        {
            "type": "extra_forbidden",
            "loc": ("body", "attributes", "ssn"),
            "msg": "Extra inputs are not permitted",
            "input": SECRET,
        }
    )
    assert rendered["message"] == "Extra inputs are not permitted"
