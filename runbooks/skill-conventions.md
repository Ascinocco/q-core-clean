# Skill conventions — the canonical text

Every `plugin/skills/*/SKILL.md` **copies** the text below verbatim.
Copied, not imported: a skill must be self-contained when loaded, because
Claude reads `SKILL.md` and a "see the runbook" is a read step that a
model under pressure skips. The cost is that each convention exists N
times and can drift, which is what `tests/test_skill_conventions.py`
exists to prevent.

**This file is the source of truth. A skill that disagrees with it is
wrong, and the pin test fails naming the skill.**

> **A pin proves agreement, not correctness.** If the fence below lost its
> empty-page guard, every pin in the repo would pass and every skill would
> hang. That is why the canonical text has its own *behaviour* test, not
> only pins pointing at it. Changing this file means re-running both.

## The drain loop

**Fourteen tools are paginated, and three are not named `list_*`:**
`get_ticket_history`, `spending_summary` and `trend`. "The list tools" is
narrower than the behaviour — checked against main, not assumed. Each
defaults to `limit=50`, and one call is **not** the whole set.

```python
def fetch_all(list_tool, **kwargs):
    items, offset = [], 0
    while True:
        page = list_tool(limit=200, offset=offset, **kwargs)
        if not page["items"]:
            return items          # empty page: stop, never spin
        items += page["items"]
        if len(items) >= page["total"]:
            return items
        offset += len(page["items"])
```

**The empty-page guard is load-bearing, not defensive habit.** `total` is
counted in a *separate query* from the page, so a row deleted between the
two leaves `total` permanently above what any offset can return. Without
the guard the loop advances by zero and spins forever — an unattended run
hanging silently rather than failing.

**`total` means the count of all matching rows, never the count
returned.** The fence terminates on that meaning. Removing `total` raises
a loud `KeyError`; *redefining* it under the same name would make
`len(items) >= page["total"]` true after the first page, always — the
original truncation bug restored inside the helper written to prevent it,
with no error anywhere. Pinned at `paginate` by
`api/tests/test_list_total_contract.py`.

**Prefer `all=true` where the tool offers it** (#90, shipped). It returns
the complete set or refuses; it never returns a partial one. Above the
cap it refuses and returns nothing, and `allow_truncated=true` is the
only route to a partial set — which makes the flag your own recorded
acceptance rather than a warning you have to spot. There is deliberately
**no truncation marker** on an ordinary page: `total` was present in
every truncated (N-of-M) response and nobody compared it, so a second
field to not-check has a poor prior.

**A refusal is a STOP, not a retry** (D42). The fence cannot refuse — it
loops until it has everything — so a caller who has only ever used the
fence has never had to decide what a refusal means. `all=true` hands them
that case. Read as transient and retried, it fails identically and makes
the tool look flaky rather than the query look too broad. Say what you
will do when it refuses, and do not retry it.

**Compare what you are holding against `total` — but the two layers name
it differently, so check which one you are on.** An MCP tool response
carries `returned`; the API's `paginate` does not, so a raw-HTTP caller
compares `len(items)`. The fence below uses `len(items)` because it talks
to the API directly. Either way the reason is the same: **a page that
fills the limit looks exactly like a complete set.**

Why it matters: with more merchant rules than one default page, the
omitted rules silently leave their transactions uncategorized, with no
error; the only symptom is a slightly larger "uncategorized" figure.

## The NEVER list comes first

A skill opens with its NEVER list, before any procedure.

- **Order by reversibility, not by cleverness.** Silent-and-irreversible
  outranks silent-and-repairable. A misfiled document is a wrong row you
  can refile; a card number read into model context cannot be unread.
- **Name the specific temptation**, not the general rule. "It's only a
  CSV, I can parse it myself" and "the extraction failed but the filename
  says it's the utility bill" are the sentences that get rationalized past.
  A NEVER line that does not name the reasonable-sounding shortcut gets
  reasoned around at exactly the moment it matters.
- **Never justify a rule by "you'll notice if it goes wrong."** That is a
  rule someone can reason past — *this time I'd notice*. If it failed
  loudly you would not need the rule; the failure would be the rule.

## Stop and report

**A disagreement between two things that should agree is a stop.** Not a
discrepancy to reconcile in passing, and not something to resolve in
favour of whoever is writing. If a preview and an import disagree about a
row, one of them is wrong about money and you do not yet know which.

Corollary, learned the expensive way: when two *measurements* disagree,
fix the definition rather than adjudicate. **State the predicate next to
the number, every time** — not once, where it was derived. A figure that
outlives its definition gets compared against figures measuring something
else, and the comparison reads as a disagreement.

## Count what you find

Never repeat a count you were told. Derive it from what is in front of
you and say what you derived it from.

**A clean return is not an assurance somebody else already checked.** A
refusal that did not fire tells you only that its specific condition did
not hold — `partial_extraction` catches a page that extracted *nothing*,
not one that extracted *too little*. A continuation stub page can
legitimately return only a few dozen characters and pass.

**When you cannot decide how wide a rule should be, pick the failure mode
someone will notice.** Under-narrow fails loudly — it inflates a number
somebody questions. Over-generous fails silently, leaving the total
quietly smaller.

## Redaction tokens are not content

`****1234` and `[REDACTED]` are what the scrubber left behind. They are
never a merchant name, an account holder or a reference. Do not match on
them, and do not treat two of them as equal to each other.

**A rule keyed on a redacted field degrades as redaction works
properly.** The scrubber removes the counterparty from a
transfer description, which also removes the evidence that the row *is* a
transfer. The redaction is correct; the classification gap is its
consequence.

## Adding a vocabulary to this file propagates N times

Because every skill **copies** this text rather than importing it, a
vocabulary added here (an "is one of" list, a set of legal values) lands
in every skill at once — and `tests/test_skill_conventions.py` is not the
only thing watching. The prose-vocabulary pin discovers introducing
phrases across `plugin/skills/*/SKILL.md`, so one edit here can trip it
in N files the author never opened.

**Add the vocabulary and its anchor in the same commit.** Otherwise the
next person gets a red suite in files they did not touch, from an edit
that looked local.

**N red tests from one edit is the GOOD failure, and should be read that
way.** The alternative — the canonical text and the skills disagreeing
about a vocabulary — fails nowhere at all. Loud and repetitive beats
silent. Anyone hitting this should add the anchor, not weaken the check,
because "the test is oversensitive" is the reasonable-sounding move that
turns a working design back into a silent one.

## The limit of the pin

`tests/test_skill_conventions.py` discovers skills by globbing
`plugin/skills/*/SKILL.md` and requires the fence of any skill that
**names a `list_` tool at all** — a token-bounded name, not a call
shape. That is a **heuristic**, and it is written down here rather than
implied by a green suite:

> It used to require `list_entities(`, with the paren. That missed the
> **callable** form `fetch_all(list_entities, ...)` — which is precisely
> what this drain-loop convention produces, so the fence's own
> recommended usage defeated the detection that requires the fence. Two
> of four skills skipped while genuinely reading lists, and deleting one
> of their fences outright left the suite green with the skip count
> merely going up (ticket T-77).

- It **over-fires** on a skill that names a tool in prose without calling
  it — including on a *negative* example, since "never a bare
  `list_entities()`" reads as a mention. That costs one line of
  justification, or a copied fence nobody uses, and it runs in the safe
  direction. Detection reads prose on purpose: measured, **no skill calls
  a list tool inside a code fence** — all of them describe their calls in
  prose — so restricting detection to code would make the requirement
  cover nothing while still reporting green.
- It still **under-fires**: the pattern is `list_[a-z_]+`, and several
  paged tools are not named `list_*` — `get_ticket_history`,
  `spending_summary` and `trend` among them. (The count used to be
  written here as "fourteen" and went stale twice; the exact number
  lives in `PAGED_TOOL_COUNT`, which a test asserts, so it is not
  repeated in prose.) A skill that pages
  one of those and carries no fence is **not** caught. It also misses a
  skill that names its tools in prose without a trailing paren, which is
  how reminder-digest's fence went unchecked until a second, unconditional
  identity test was added.

The convention is **any skill that reads a list drains it**. The test
catches the common shape; it does not prove the general case. A green
suite here is evidence, not a guarantee.
