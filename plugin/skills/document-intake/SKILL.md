---
name: document-intake
description: File a receipt, tax document, policy, lease, warranty or piece of correspondence into q-core — extract it through the server-side scrubber, classify it, link it to the entity it concerns, and register it into the documents store. Use when asked to file, log, save, store or process a document, receipt or bill, or to sort out what is sitting in inbox/.
---

# Document intake

## Never — read this before anything else

These are not cautions. They are the reason this skill exists, and every
one of them will feel reasonable to break at the moment it matters.

This list is ordered by reversibility — the first item is the one that
cannot be undone.

**Never open the document yourself.** Not with Read, not with `cat`, not
with a Python script, not "just to see the total". The only way a
document's text enters context is `extract_document_text`'s return value.

The temptation here is specific and worth naming, because the general
rule will not survive it: **a receipt is small.** It is one page, often
one column, visibly not a bank statement, and frequently has no account
number on it at all. Reading it looks like nothing. But a receipt can
carry a full card number — many do — and **you cannot know whether this
one does until you have already read it.** The rule therefore has to hold
*before* the information that would justify breaking it exists. That is
the whole argument, and it does not depend on any property of the
document in your hand.

And it fails silently. There is no client-side redaction anywhere in this
system: `scrub_text` and `assert_no_account_numbers` run in the API
process and nowhere else. A skill that opens the file has stepped around
the entire mechanism — no error, no warning, no redaction count, nothing
downstream that says it happened. If you are ever tempted by "I'd notice
if it were a problem": you would not. There is no mechanism by which you
could.

**Never file a document you have not extracted.** `doc_type`, the title
and the entity link all come from the scrubbed text. Filing on a filename
guess — "`utility-march.pdf`, that's correspondence for the house" —
produces a plausible `doc_type`, a confident title, and a wrong entity
link, and *nothing about the output looks like an error*. This is the
easier mistake to make and the harder one to notice, but it ranks second
here on purpose: a wrong row is repairable. Notice it, refile it, and the
system is correct again. **Better find, lesser hazard.** A card number
read into context cannot be unread.

**Never process anything from `intake/`.** That directory belongs to the
statement-intake skill, which imports bank and card statements. A receipt
that arrives there should be named explicitly by the user, or moved to
`inbox/` first — not swept up because it looked like a receipt. Two
skills claiming one file is how the same document gets filed twice, once
as a statement and once as a receipt. This skill's territory is `inbox/`
plus paths the user names.

**When `extract_document_text` refuses or errors, stop.** Report the
tool's error verbatim, except for masking any digits it echoes, and go no
further on that file. Specifically, on `redaction_incomplete`, on
`partial_extraction`, on a scanned or empty document, on a path outside
the permitted roots, or on any other refusal:

- Do **not** open the file to see what went wrong.
- Do **not** ask the user to paste the contents, or a photo, or "just the
  total line".
- Do **not** fall back to reading a `.txt` or `.csv` version directly.
- Do **not** try a different path, a copy, or a converted version of the
  same document.

The only remediation you may suggest is filing a ticket against the
scrubber. A refusal means the safety property could not be established;
working around it is working around the safety property.

**`partial_extraction` is a STOP and a ticket. Never pass
`allow_partial` on a bank or card statement.** This refusal means some
pages yielded text and some yielded none, and the tool offers
`allow_partial=true` to accept that — which is exactly why it needs a
rule. The parameter exists for a blank cover or notice page. On a
statement a missing page means **missing transactions**, and they do not
arrive as an error: they import as though the statement were whole, so
every total is wrong and every total looks right. There is nothing
downstream that can notice.

Reaching for `allow_partial` will feel reasonable, because the refusal
arrives when you are most of the way through a document that mostly
worked. That is the moment the rule is for.

**And `partial_extraction` not firing is not a guarantee of
completeness.** It catches a page that extracted *nothing*; it does not
catch a page that extracted too little. A continuation stub page can
legitimately come back with only a few dozen characters and pass. If completeness matters for what you are about to do,
read `extracted_chars` yourself rather than treating a clean return as an
assurance somebody else already checked.

**Registering from `inbox/` MOVES the file.** It is not a copy, it is not
repeatable, and there is no undo. Confirm with the user before
registering, and if a batch fails part way through, **never re-run it
blindly** — the sources for the successful half are already gone, and a
second pass will report "file not found" for work that actually
succeeded. Re-run only the files you can see are still there.

(A document registered from anywhere *other* than `inbox/` is copied, and
its original is left alone. `intake/` in particular is never modified.
You should not be reading from `intake/` anyway — see above — but if the
user names a path there explicitly, registering it leaves it in place.)

**Never record a receipt as a transaction.** Filing the document is what
"log this receipt" means here. The spending itself is captured when the
matching statement is imported, and creating a transaction from the
receipt as well would double-count it: transaction de-duplication matches
on exact `(txn_date, description, amount_cents)`, and a receipt's
description differs from the statement descriptor for the same purchase,
so both rows would survive and every total would be wrong with nothing
reporting a problem. Say plainly in your report that the spending will be
captured at statement import. (Decision D18. The single exception is an
explicit instruction naming a source that will never be imported — "record
this as a cash transaction" — and even then, never offer it.)

**Never send an SSN, or a full account, routing or card number, to any
model.** That is the standing invariant of this system, and this skill's
entire shape follows from it. If you ever find yourself holding one, you
have already gone wrong — say so plainly rather than continuing.

## What the scrubbed text looks like

Redaction happens server-side before you see anything, so the text you
get already contains placeholders:

- `****1234` — a long digit run with its last four preserved.
- `[REDACTED]` — an SSN, or a run with nothing safe to keep.

Neither is document content. A `****1234` is not a title, not a merchant
name, and not a document number to file under. Over-redaction is expected:
a phone number on a letterhead comes back as `****1234`, and so does a
confirmation number. Amounts and dates are preserved.

If a document is unusable because redaction removed what identified it,
say so and ask — do not reconstruct it.

## Every list call is paginated

`list_entities` defaults to `limit=50` and returns
`{items, total, limit, offset}`. One call is **not** the whole set.
Drain it:

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

Always compare against `total`. Raising `limit` is not a fix — it moves
the cliff rather than removing it, and `MAX_LIMIT` is 200.

The empty-page guard is load-bearing, not defensive habit: `total` is
counted in a separate query from the page, so a row deleted between the
two leaves `total` permanently above what any offset can return. Without
the guard the loop advances by zero and spins forever — an unattended run
hanging silently rather than failing.

This fence is copied verbatim from statement-intake, which is where the
canonical version lives. It is not adapted, because two versions of one
loop diverge exactly the way two copies of one default do.

What it costs here is a wrong link rather than a wrong total. An entity
past the first page reads as **absent**, and both outcomes look like
success: the document is filed unlinked, or a duplicate entity is
created for something that already exists. Neither raises anything. A
short first page looks exactly like a small dataset, so nothing in the
result says it was truncated — and step 5's rule, *under-link rather
than guess*, gives a truncated list the shape of a correct answer,
because "no matching entity" is a legitimate outcome this skill is told
to expect.

## The flow

### 1. Choose the files

List `inbox/` by name only (`ls`). Filenames are not document content.

If the user named specific files, use those. Otherwise confirm the list
with them before touching anything — registering moves files out of
`inbox/`, so "process everything in there" should be a decision the user
actually made rather than one inferred from an open-ended request.

Count what you find rather than repeating a number you were told.

### 2. Extract

Call `extract_document_text(path)`. **Pass an absolute path for anything
in `inbox/`.** A bare name resolves against `intake/`, not `inbox/` — that
is statement-intake's base and deliberately unchanged, because resolving a
bare name against whichever directory happens to hold a matching file
would make its meaning depend on the filesystem. Absolute paths inside
`inbox/`, `intake/` or `data/documents/` all work.

Check the result before classifying: it reports the page count, the
characters extracted per page, and the redaction count.

Two refusals to expect, and they mean different things. A document whose
pages *all* extract zero characters is `no_text_extracted` — that usually
means a scanned image, which this system cannot OCR. A document where
only *some* pages extract is `partial_extraction`, which carries the page
numbers that came back empty. The first needs OCR; the second needs
somebody to decide whether the missing page mattered, and for a statement
the answer is always yes. Stop and report that file; carry on with the
others.

### 3. Classify

From the scrubbed text, propose a `doc_type` from exactly this list:
`receipt`, `tax`, `insurance_policy`, `lease`, `title_deed`, `warranty`,
`correspondence`, `statement`, `other`.

`statement` exists in the vocabulary but is not this skill's business — if
a document is a bank or card statement, say so and hand it to
statement-intake rather than filing it here.

If two types fit, ask. `other` is a real answer and better than a
confident wrong one; a document filed as `other` is findable, a document
filed as `warranty` when it is a lease is not.

### 4. Propose a title

Short, human, and recognisable in a directory listing: "Bank card April 2026
statement", "Toyota winter tires receipt", "tax slip". The title becomes
part of the stored filename, so it is what the owner will read when scanning
`data/documents/` later.

### 5. Propose an entity link — under-link rather than guess

Call `fetch_all(list_entities)` — never a bare `list_entities()`, for
the reason given above — and link the document to an entity **only when
that entity's name actually appears in the scrubbed text.** A receipt
naming "Toyota" links to the Toyota vehicle entity; a receipt from a tire
shop that names no vehicle does not link to one just because it is
plausible.

Ask when it is ambiguous. A wrong link is not neutral: documents linked
to an entity feed that entity's picture of itself, so filing a receipt
against the wrong vehicle quietly distorts what that vehicle appears to
have cost. No link is a smaller error than a wrong one, and an unlinked
document is still findable by type and title.

### 6. Confirm, then register

Show the user what you are about to file for each document: source path,
`doc_type`, title, entity link or none. Say plainly that files in
`inbox/` will be **moved**.

Then call `register_document(path, title, doc_type, entity_id)`.

If the document's content is already stored, the tool returns the
existing record and moves nothing. That is not an error — say so ("already
filed, nothing moved") and move on.

### 7. Report

For each document: the title, the `doc_type`, the entity link, and the
document id the tool returned. Raw stored paths are deliberately not exposed:
a path in model context invites the model to bypass the scrubber. Use the id
with `extract_registered_document_text` for any later read.

Say explicitly which sources were moved and which were copied, and for
receipts, that the spending will be captured when the statement is
imported.

## After registering, the original path is dead

A file registered from `inbox/` has moved. The path you extracted from no
longer exists.

If you need the text again — to answer a question about the document, or
because the user asks a follow-up — call
`extract_registered_document_text(document_id)`. Re-extracting the original
path will fail, and the failure will look like the file was never there.

## What this skill does not do

- It does not read documents. Everything goes through
  `extract_document_text`.
- It does not create transactions. See the NEVER list.
- It does not import statements. That is statement-intake, and `intake/`
  is its territory.
- It does not delete anything. `delete_document` removes the record *and*
  the file permanently; if a document was filed wrongly, say so and let
  the user decide.
