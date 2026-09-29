"""A skill's prose vocabulary must match the API's.

A skill is instructions for a model to follow, so a stale list in it does
not fail — it produces confident, wrong calls. `document-intake` tells the
model to choose a `doc_type` "from exactly this list", and if that list
drifts from `api.models.DocType` the model picks a value the API rejects,
or never offers one the API accepts.

This repo has watched exactly that happen twice: the MCP tool descriptions
went stale against `EntityType` when `project` was added (nothing failed —
the tools kept working for every value they did list), and the same
descriptions then contradicted themselves when the constant was fixed and
the English sentence above it was not. Prose is the part that goes stale
silently, because nothing executes it.
"""

import re
from pathlib import Path
from typing import get_args

import pytest

from api.models import AccountAttributes, DocType

SKILLS = Path(__file__).resolve().parent.parent / "plugin/skills"
SKILL = SKILLS / "document-intake/SKILL.md"
STATEMENT_SKILL = SKILLS / "statement-intake/SKILL.md"

#: The sentence each list belongs to. Anchored on the phrase rather than a
#: line number so re-wrapping the paragraph does not silently disable this.
ANCHOR = "from exactly this list"
SUBTYPE_ANCHOR = "`account_subtype` is one of"

#: Every phrase that introduces a vocabulary in a skill, and the anchor
#: that pins it. `test_every_prose_vocabulary_is_pinned` fails if a skill
#: grows a list this file does not know about — enumeration alone would
#: let the next one arrive unpinned, which is how both of the failures in
#: the docstring happened.
VOCABULARY_PHRASES = ("is one of", "from exactly this list", "must be one of")
PINNED_ANCHORS = (ANCHOR, SUBTYPE_ANCHOR)


def _paragraph_after(path: Path, anchor: str) -> str:
    text = path.read_text()
    assert anchor in text, (
        f"{path.name} no longer contains {anchor!r}, so this test can no "
        "longer find the list it is meant to check — fix the anchor rather "
        "than deleting the test"
    )
    start = text.index(anchor)
    # The list runs to the end of that paragraph.
    return text[start : text.index("\n\n", start)]


def _backticked(paragraph: str, drop: set[str]) -> set[str]:
    return set(re.findall(r"`([a-z_]+)`", paragraph)) - drop


def _listed_doc_types() -> set[str]:
    return _backticked(_paragraph_after(SKILL, ANCHOR), {"doc_type"})


def _listed_account_subtypes() -> set[str]:
    return _backticked(
        _paragraph_after(STATEMENT_SKILL, SUBTYPE_ANCHOR), {"account_subtype"}
    )


def _api_account_subtypes() -> set[str]:
    # account_subtype is `Literal[...] | None`; the Literal is the first arg.
    annotation = AccountAttributes.model_fields["account_subtype"].annotation
    return set(get_args(get_args(annotation)[0]))


def test_the_skill_lists_exactly_the_api_doc_types():
    assert _listed_doc_types() == set(get_args(DocType))


def test_statement_intake_lists_exactly_the_api_account_subtypes():
    """statement-intake tells the model which `account_subtype` to set
    when it creates an account entity for a statement.

    Set equality, both directions, not "the prose mentions them all". A
    value the API accepts but the skill omits is an account silently
    typed wrong; a value the skill offers but the API rejects sends the
    model into a 422 it cannot reason its way out of. The second is the
    one an "includes everything" assertion misses.
    """
    assert _listed_account_subtypes() == _api_account_subtypes()


def test_the_subtype_list_is_not_matching_by_accident():
    """Guards the guard: if the anchor moved and the paragraph slice came
    back empty, set equality would fail — but if the regex silently
    matched nothing while the API vocabulary were also empty, both sides
    would be empty and the test would pass over nothing."""
    assert len(_listed_account_subtypes()) >= 5
    assert "checking" in _listed_account_subtypes()




def _skill_files() -> list[Path]:
    return sorted(SKILLS.glob("*/SKILL.md"))


def test_the_discovery_finds_every_skill():
    """Cross-check the glob against an independent traversal.

    Not a count. A literal needs editing whenever a skill lands and then
    fails for the wrong reason at the moment someone is adding one; a
    floor (`>= N`) stops discriminating as soon as the population passes
    it. Two different ways of finding the same set have neither problem
    and need no maintenance — and unlike a count, which is a claim
    *about* the discovery and can only be wrong in the same direction as
    it, a second traversal is an independent *way* of doing it.
    (impl-1's framing, from the same shape in test_skill_conventions.py.)

    This is load-bearing rather than tidy. Measured: replacing the glob
    below with `[]` leaves this file at **4 passed, 1 skipped** — pytest
    turns an empty parametrize into a skip, so the vocabulary discovery
    silently covers nothing while the suite stays green. A skip is
    quieter than a failing assertion would be, because it reads as
    deliberate.
    """
    globbed = {path.parent.name for path in _skill_files()}
    walked = {
        directory.name
        for directory in SKILLS.iterdir()
        if directory.is_dir()
        and not directory.name.startswith((".", "_"))
        and (directory / "SKILL.md").is_file()
    }

    assert walked, (
        f"no skill found under {SKILLS} at all — the discovery below would "
        f"pass over an empty set"
    )
    assert globbed == walked


@pytest.mark.parametrize(
    "path", _skill_files(), ids=lambda p: p.parent.name
)
def test_every_prose_vocabulary_is_pinned(path):
    """A skill that grows a second list must not do so unpinned.

    The two failures in this file's docstring were both a vocabulary
    drifting with nothing executing it. Enumerating the ones we know
    about cannot catch the next one — the diff that adds a list is the
    diff that would have added its pin, and the reviewer is reading the
    file where the pin is absent. So the phrases are discovered and each
    must resolve to a registered anchor.

    Over-fires on prose that happens to use one of these phrases without
    introducing a vocabulary. That costs one line here; under-firing
    costs a model a 422 it cannot fix.
    """
    text = path.read_text()
    for phrase in VOCABULARY_PHRASES:
        for match in re.finditer(re.escape(phrase), text):
            window = text[max(0, match.start() - 40) : match.end()]
            assert any(anchor in window for anchor in PINNED_ANCHORS), (
                f"{path.parent.name} introduces a vocabulary with "
                f"{phrase!r} that no anchor in this file pins: "
                f"...{window.strip()!r}. Add an anchor and a set-equality "
                f"test, or reword the prose if it is not a vocabulary."
            )


def test_the_skill_file_exists_where_claude_code_looks_for_it():
    """`plugin/skills/<name>/SKILL.md` is where the q-core plugin's standard
    layout puts skills (split, part 3); installed on every machine, that is
    where Claude Code discovers them. A skill anywhere else is a document
    nobody reads."""
    assert SKILL.is_file()
    assert SKILL.read_text().startswith("---\nname: document-intake\n")
