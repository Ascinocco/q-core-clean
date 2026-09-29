"""Privacy guards for text that q-core persists and may return to a model.

Document extraction has a scrubber because the source is allowed to contain
financial identifiers. Ordinary API writes are different: silently editing a
ticket, transaction description or entity name would corrupt the user's data,
so those writes are refused when they contain an account-shaped digit run.

The error is deliberately constant. Pydantic validation failures are returned
through the API and MCP, so quoting the rejected value would recreate the leak
the guard prevented.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import AfterValidator

from api.redaction import UnscrubbedDigitsError, assert_no_account_numbers


STORED_TEXT_REFUSAL = (
    "text that may be stored or returned must not contain a full account, "
    "routing, card or SIN-shaped number; use a masked last four instead"
)


# Generated entity/board/ticket IDs may contain seven-digit numeric fragments.
# Recognize only complete RFC-variant UUIDv4 tokens, never arbitrary hex hashes
# or partial IDs. Word/hyphen boundaries prevent exemptions inside a larger
# identifier. This is a stored-text utility exception, not a document-redaction
# rule; the scanner sees placeholders while the caller's text stays unchanged.
_STORED_UUID4 = re.compile(
    r"(?<![\w-])[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![\w-])",
    re.IGNORECASE,
)


def refuse_sensitive_numbers(value: str) -> str:
    """Return safe text unchanged; refuse unsafe text without echoing it."""
    try:
        scan = _STORED_UUID4.sub(lambda match: "\0" * len(match.group()), value)
        assert_no_account_numbers(scan)
    except UnscrubbedDigitsError:
        raise ValueError(STORED_TEXT_REFUSAL) from None
    return value


StoredText = Annotated[str, AfterValidator(refuse_sensitive_numbers)]


def refuse_sensitive_numbers_in(value: Any) -> Any:
    """Recursively guard free-form JSON blobs before they are persisted."""
    if isinstance(value, str):
        return refuse_sensitive_numbers(value)
    if isinstance(value, dict):
        for key, item in value.items():
            refuse_sensitive_numbers(str(key))
            refuse_sensitive_numbers_in(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            refuse_sensitive_numbers_in(item)
    return value
