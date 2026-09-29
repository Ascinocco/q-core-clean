"""Every skill's copy of the conventions must match the canonical text.

`runbooks/skill-conventions.md` is the source of truth; each SKILL.md
copies it verbatim, because a skill must be self-contained when loaded.
Copying means N copies that can drift, and these tests are what stop it.

TWO KINDS OF TEST HERE, AND BOTH ARE NECESSARY:

  * PINS prove the skills AGREE with the runbook.
  * A BEHAVIOUR test proves the text they agree ON works.

Pins alone are not enough and the failure is total: if the canonical
fence lost its empty-page guard, every pin would pass and every skill
would hang. Unanimous and wrong, with every signal green.

THE MUTATION THAT DEMONSTRATES THIS HAS TO BE THE PROPAGATED ONE.
Dropping the guard from the canonical copy *alone* turns the pins red
too -- they compare each skill against canon, so a one-sided edit is a
disagreement and they catch it. That looks reassuring and proves the
wrong thing. The dangerous edit is the CONSISTENT one: canon and every
skill changed together, which is exactly what normal propagation does.
Measured, with the guard removed from canon and both skills:

    test_the_runbook_defines_a_canonical_fence                PASSED
    test_discovery_finds_every_skill_directory                PASSED
    ...carries_the_canonical_fence[document-intake]           PASSED
    ...carries_the_canonical_fence[statement-intake]          PASSED
    test_the_canonical_fence_actually_drains                  PASSED
    test_the_canonical_fence_terminates_on_an_empty_page      FAILED

There is deliberately NO floor test ("at least N skills carry a fence")
beside the per-skill pin. A floor stops discriminating once the
population exceeds it: with three skills, one can drop its fence and
">= 2" still passes while the identity check compares the two survivors
and agrees -- every guard green, one skill silently unprotected
(impl-3 measured exactly that, 8 passed and blind). The requirement here
is per-skill, which makes a floor a weaker restatement of the same rule
and the weaker statement is the one that goes blind.

Every pin green, the behaviour test the only thing red. That divergence
is the whole argument for having both.
"""

import re
from pathlib import Path

import pytest

from api.config import REPO_ROOT

CANON = REPO_ROOT / "runbooks/skill-conventions.md"
SKILLS_DIR = REPO_ROOT / "plugin/skills"


def _fence(markdown: str, defines: str) -> str | None:
    blocks = re.findall(r"```python\n(.*?)```", markdown, re.S)
    hits = [b for b in blocks if defines in b]
    if not hits:
        return None
    assert len(hits) == 1, f"expected at most 1 fence defining {defines!r}, got {len(hits)}"
    return hits[0]


def _skill_files() -> list[Path]:
    """DISCOVERED, never enumerated.

    A hand-written list has the same defect one level up: a new skill
    escapes by not being added to it, and the diff that introduces the
    skill is the diff that would have carried the entry -- so the reviewer
    who would catch the omission is reading the file where it is absent.
    That is not hypothetical: document-intake shipped calling
    `list_entities()` unpaged, with no fence and no mention of pagination,
    and nothing flagged it (ticket T-79).
    """
    return sorted(SKILLS_DIR.glob("*/SKILL.md"))


CANONICAL_FENCE = _fence(CANON.read_text(), "def fetch_all")


def test_the_runbook_defines_a_canonical_fence():
    """Guard on the guard: every pin below compares against this."""
    assert CANONICAL_FENCE, "runbooks/skill-conventions.md defines no fetch_all fence"


def test_discovery_finds_every_skill_directory():
    """Guards the DISCOVERY itself, against an independent traversal.

    If `_skill_files()` returned [], every parametrized pin below would
    vanish and the suite would stay green while enforcing nothing --
    impl-4 measured that exact shape, where one broken helper made three
    guards vacuous at once and still reported the rule enforced.

    Cross-checked against a different traversal rather than a count.
    impl-3's caution is that a floor (">= N") absorbs the regression once
    the population grows past it; a literal count is the usual answer but
    needs editing whenever a skill lands, and then fails for the wrong
    reason when one does. Comparing two ways of finding the same thing has
    neither problem.
    """
    walked = {d.name for d in SKILLS_DIR.iterdir()
              if d.is_dir() and (d / "SKILL.md").is_file()}
    globbed = {p.parent.name for p in _skill_files()}
    assert walked, f"no skills found under {SKILLS_DIR}"
    assert globbed == walked, (
        f"discovery disagrees with the directory: glob found {sorted(globbed)}, "
        f"walk found {sorted(walked)}"
    )


@pytest.mark.parametrize("skill", _skill_files(), ids=lambda p: p.parent.name)
def test_any_fence_a_skill_carries_matches_the_canonical_one(skill):
    """UNCONDITIONAL, and it covers what the rule below cannot.

    Two different rules, and conflating them left a hole:
      * a skill that READS a list must HAVE a fence   (below, conditional)
      * a skill that HAS a fence must MATCH canon     (here, unconditional)

    reminder-digest is why. It carries the fence but names its tools in
    prose -- `list_due_items` without a trailing paren -- so the call
    detection below finds nothing and skips it. Its fence was therefore
    never compared to anything. Byte-identical today,
    and nothing would have said otherwise if it drifted.

    Found by reading a skip rather than a failure: the suite reported
    "1 skipped" for reminder-digest and that skip was the whole of its
    coverage. A skip you did not write is an unread test result.
    """
    fence = _fence(skill.read_text(), "def fetch_all")
    if fence is None:
        pytest.skip(f"{skill.parent.name} carries no fence")
    assert fence == CANONICAL_FENCE, (
        f"{skill.parent.name}'s fence has drifted from "
        "runbooks/skill-conventions.md"
    )


@pytest.mark.parametrize("skill", _skill_files(), ids=lambda p: p.parent.name)
def test_every_skill_that_reads_a_list_carries_the_canonical_fence(skill):
    """The heuristic, stated in the runbook rather than implied here.

    "Mentions a `list_` tool" over-fires on prose and under-fires on a
    skill that pages another way. It catches the common shape; it does not
    prove the general case.
    """
    text = skill.read_text()
    # TOKEN-BOUNDED NAME, NOT A CALL SHAPE. Requiring a trailing paren --
    # `list_entities(` -- missed the callable form `fetch_all(list_entities,
    # ...)`, which is exactly what the drain-loop convention produces. So
    # the fence's own recommended usage defeated the detection that
    # requires the fence, and email-triage and reminder-digest both skipped
    # while genuinely reading lists (ticket T-77).
    #
    # Measured before the widening: deleting reminder-digest's fence
    # outright gave "9 passed, 3 skipped" -- no failure at all. The skip
    # count went UP and nothing asked for the fence back.
    #
    # Detection reads the WHOLE file, prose included, deliberately. The
    # obvious narrowing -- look only inside ```python fences -- was
    # measured and is worse: no skill calls a list tool inside a fence, so
    # it would cover nothing while reporting green.
    #
    # It over-fires: a skill naming a tool in prose, or in a NEGATIVE
    # example like document-intake's "never a bare `list_entities()`",
    # is required to carry a fence. That cost is one line of justification
    # and it runs in the safe direction, which D25 already priced. A fence
    # required for a slightly wrong reason still holds; a fence not
    # required at all does not.
    calls = sorted(set(re.findall(r"\blist_[a-z_]+\b", text)) - {"list_tool"})
    if not calls:
        pytest.skip(f"{skill.parent.name} names no list tool")

    # A skill whose every list call passes `all=true` does not need the
    # fence: the tool pages for it and refuses rather than truncating
    # (D27). Without this, the rule would demand a fence that is no longer
    # used -- and the false positive would GROW as D27 succeeds, so the
    # guard would penalise the migration the project is making.
    #
    # impl-3 asks whether a guard depends on something the project is
    # trying to eliminate. This is that question inverted: does the guard
    # REQUIRE something the project is trying to move away from.
    # A tool counts as migrated only if EVERY mention of it passes
    # all=True. A bare mention -- including the callable form handed to
    # the fence -- is not migrated, so the fence is still required.
    unpaged = [c for c in calls
               if re.search(rf"\b{c}\b(?!\s*\([^)]*\ball\s*=\s*True)", text)]
    if not unpaged:
        # MIGRATED, NOT EXEMPT. An earlier version skipped here, which
        # turned "must carry a drain loop" into "must carry nothing" --
        # closing one unchecked class by opening another (impl-3).
        #
        # The requirement does not disappear on migration, it CHANGES.
        # `all=true` is complete-or-refuse: above the cap it is a 422 that
        # withholds the rows. So a migrated skill has an obligation the
        # fence never had -- say what it does when the call refuses --
        # which is the same shape `partial_extraction` gets a NEVER
        # section for. Without this a skill could migrate, be skipped, and
        # leave an unattended run stopping on an unhandled tool error with
        # no procedure for it.
        #
        # Checked against the CONCEPT, not the identifier: #90 has not
        # merged and the field name is not shipped. Naming it here would
        # pin against a key that can still move. The canonical text
        # carries the name once it lands.
        assert re.search(r"refus(e|es|ed|al)", text, re.I), (
            f"{skill.parent.name} reads lists only via all=true but never "
            "mentions a refusal -- all=true is complete-or-refuse, so the "
            "skill must say what it does when the call refuses"
        )
        return
    fence = _fence(text, "def fetch_all")
    assert fence is not None, (
        f"{skill.parent.name} calls {unpaged} without all=true but carries "
        "no fetch_all fence -- "
        "any skill that reads a list drains it"
    )
    assert fence == CANONICAL_FENCE, (
        f"{skill.parent.name}'s fence has drifted from "
        "runbooks/skill-conventions.md"
    )


def test_the_canonical_fence_actually_drains():
    """The behaviour half. Pins cannot see this."""
    namespace: dict = {}
    exec(compile(CANONICAL_FENCE, "<canonical>", "exec"), namespace)
    fetch_all = namespace["fetch_all"]

    rows = [{"id": i} for i in range(137)]

    def pager(limit, offset):
        window = rows[offset : offset + limit]
        return {"items": window, "total": len(rows), "limit": limit, "offset": offset}

    got = fetch_all(pager)
    assert len(got) == len(rows)
    assert [r["id"] for r in got] == list(range(137)), "dupes or gaps"


def test_the_canonical_fence_terminates_on_an_empty_page():
    """A stale `total` must not spin the loop forever.

    `total` is counted in a separate query from the page, so a row deleted
    between the two leaves `total` above what any offset can return.

    The call budget is not decoration. Unbounded, a missing guard HANGS
    the suite rather than failing it -- no report, blocked CI, reads as
    infrastructure rather than as the bug. A test whose failure mode is a
    hang is barely a test.
    """
    calls = {"n": 0}

    def hostile(limit, offset):
        calls["n"] += 1
        if calls["n"] > 20:
            raise AssertionError(
                "fetch_all did not terminate on an empty page -- the canonical "
                'fence is missing `if not page["items"]: return items`'
            )
        return {"items": [], "total": 7, "limit": limit, "offset": offset}

    namespace: dict = {}
    exec(compile(CANONICAL_FENCE, "<canonical>", "exec"), namespace)
    assert namespace["fetch_all"](hostile) == []


# --------------------------------------------------------------------------
# runbooks/INDEX.md is a hand-maintained list of a directory's contents,
# which makes it the same hazard as any other value typed from memory: it
# drifts, silently, in the direction of being incomplete. It was in fact
# missing `skill-conventions.md` -- the file this module's own tests treat
# as the source of truth -- so a runbook whose entire purpose is to be
# findable was not findable by the document that exists to find it.
#
# The index's stated job is "check here before assuming something isn't
# written down." A missing entry defeats exactly that, and defeats it
# invisibly: you look, you don't find it, and you conclude it isn't there.
# --------------------------------------------------------------------------

RUNBOOKS = REPO_ROOT / "runbooks"
INDEX = RUNBOOKS / "INDEX.md"


def _linked_targets(page):
    return set(re.findall(r"\]\(([^)]+\.md)\)", page.read_text()))


def _indexed_targets():
    return _linked_targets(INDEX)


def _runbook_files():
    return {p.name for p in RUNBOOKS.glob("*.md") if p.name != "INDEX.md"}


def test_every_runbook_is_listed_in_the_index():
    """Derived from the directory, not from a literal list of filenames.

    A literal list here would reproduce the defect: it would need updating
    in the same commit that adds the runbook, which is the step that gets
    missed.
    """
    files = _runbook_files()
    assert files, "found no runbooks -- the glob broke, and this test would pass vacuously"
    missing = sorted(files - _indexed_targets())
    assert not missing, (
        f"runbooks not listed in INDEX.md: {missing} -- add one line each. "
        f"The index is what people check before concluding something isn't "
        f"written down, so an unlisted runbook reads as a nonexistent one."
    )


def test_the_index_does_not_point_at_missing_runbooks():
    """The reverse: a renamed or deleted runbook still linked from the index.

    A dead link is worse than a missing entry -- it says the thing exists
    and sends you somewhere empty.
    """
    indexed = _indexed_targets()
    assert indexed, "parsed no links out of INDEX.md -- the regex broke"
    dangling = sorted(t for t in indexed if not (RUNBOOKS / t).exists())
    assert not dangling, f"INDEX.md links to runbooks that don't exist: {dangling}"


