"""Narrow comparison identity, never a rewrite of stored merchant text."""

import re

_PLACEHOLDER = re.compile(r"(?<!\S)\[REDACTED\](?!\S)")


def comparison_description(description: str) -> str:
    """Ignore standalone redaction placeholders and whitespace only.

    Preserve case, punctuation, masked suffixes, and embedded lookalikes.
    If nothing meaningful survives, retain the whitespace-normalized original
    rather than making all uninformative descriptions equivalent to empty text.
    """
    cleaned = " ".join(_PLACEHOLDER.sub("", description).split())
    return cleaned or " ".join(description.split())
