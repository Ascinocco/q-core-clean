"""A dependency whose output reaches stored data is pinned, not floored.

Audited 2026-09-19 (ticket T-25), from the pypdf story (ticket T-24):
`pypdf>=5.1` had already allowed an in-place jump to 6.19.0, and pypdf
feeds `description`, which is part of the de-duplication key. An upgrade
changing whitespace handling would change the key for every previously
imported row -- silently, with re-imports simply starting to create
duplicates.

A FLOOR DOES NOT DISCRIMINATE. That is the whole finding, and it is the
same shape as a `>= N` floor in a test: it stops distinguishing the
moment the population moves past it.

THE LITERAL LISTS AND THE FILE CHECK EACH OTHER. Naming the pinned
dependencies alone would not notice a NEW dependency nobody classified --
the diff that adds it is the diff that would have carried the entry.
Reading the file alone would pass over an empty parse. Asserting the two
agree has neither failure.
"""

from __future__ import annotations

import importlib.metadata as metadata
import re

import pytest

from api.config import REPO_ROOT

REQUIREMENTS = REPO_ROOT / "api/requirements.txt"

#: Output reaches a stored value, a key, or scrubbed text -> pinned exactly.
PINNED: dict[str, str] = {
    # A LENIENT coercion change is the hazard, not a strict one: stricter
    # rejects and shouts, more lenient accepts what it used to reject and
    # writes a different key with a 200. "Most likely loud" is not
    # "guaranteed loud", and only the silent half matters here.
    "pydantic": "coerces txn_date and amount_cents, both part of the dedup key",
    "python-multipart": "parses the upload whose filename and content_hash are stored",
    "pypdf": "extracts statement text, which becomes `description` -- a key field",
}

#: Output does not reach stored data -> a floor is fine.
FLOORED: dict[str, str] = {
    "fastapi": "routing and response serialization; nothing it produces is written",
    "uvicorn": "transport only",
    "pydantic-settings": "chooses which database is opened, not what goes in it",
    "python-dateutil": "expands recurrences at request time; api/due.py writes nothing",
    "opentelemetry-api": "telemetry: exports allowlisted span fields to the collector, stores nothing",
    "opentelemetry-sdk": "telemetry: exports allowlisted span fields to the collector, stores nothing",
    "opentelemetry-exporter-otlp-proto-http": "telemetry transport to the collector; stores nothing",
    "opentelemetry-instrumentation-fastapi": "telemetry: records spans per request; stores nothing",
    "opentelemetry-instrumentation-httpx": "telemetry: records spans per outgoing call; stores nothing",
    "opentelemetry-instrumentation-logging": "telemetry: forwards allowlisted log fields; stores nothing",
}

#: Distribution name -> the name `importlib.metadata` knows it by, where
#: they differ.
INSTALLED_AS = {"uvicorn[standard]": "uvicorn"}

LINE = re.compile(r"^(?P<name>[A-Za-z0-9._\[\]-]+)\s*(?P<op>==|>=)\s*(?P<version>\S+)")


def _declared() -> dict[str, tuple[str, str]]:
    """name -> (operator, version), ignoring comments and -r includes."""
    out: dict[str, tuple[str, str]] = {}
    for raw in REQUIREMENTS.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-r"):
            continue
        match = LINE.match(line)
        assert match, f"unparsed requirement line: {line!r}"
        name = match.group("name")
        out[INSTALLED_AS.get(name, name)] = (match.group("op"), match.group("version"))
    return out


def test_every_dependency_is_classified():
    """A new dependency must be classified, not silently floored.

    Also guards the parse: if `_declared()` returned nothing, every check
    below would pass over an empty set and report green.
    """
    declared = _declared()
    assert declared, "parsed no requirements at all"
    classified = set(PINNED) | set(FLOORED)
    unclassified = set(declared) - classified
    stale = classified - set(declared)
    assert not unclassified, (
        f"dependency added without a classification: {sorted(unclassified)}. "
        "Decide where its output lands -- stored value, key, scrubbed text, "
        "or none -- and add it to PINNED or FLOORED."
    )
    assert not stale, f"classified but no longer declared: {sorted(stale)}"


@pytest.mark.parametrize("name", sorted(PINNED), ids=lambda n: n)
def test_a_key_bearing_dependency_is_pinned_exactly(name):
    operator, version = _declared()[name]
    assert operator == "==", (
        f"{name} carries `{operator}` but {PINNED[name]}. A floor does not "
        "discriminate: it permits the upgrade that changes stored data."
    )


@pytest.mark.parametrize("name", sorted(PINNED), ids=lambda n: n)
def test_the_pin_records_the_version_actually_tested(name):
    """A pin naming a version nobody runs is a number, not a fact."""
    _, pinned = _declared()[name]
    assert pinned == metadata.version(name), (
        f"{name} is pinned at {pinned} but {metadata.version(name)} is "
        "installed -- the suite is not exercising the pinned version"
    )


@pytest.mark.parametrize("name", sorted(FLOORED), ids=lambda n: n)
def test_a_non_key_bearing_dependency_keeps_its_floor(name):
    """Pinning everything is its own failure: it stops security updates.

    The classification has to cut both ways or it is not a classification.
    """
    operator, _ = _declared()[name]
    assert operator == ">=", (
        f"{name} is pinned, but {FLOORED[name]} -- pinning it buys nothing "
        "and costs upgrades"
    )
