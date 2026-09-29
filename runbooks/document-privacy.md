# Document privacy profile

Names and addresses are scrubbed
server-side before `/documents/extract` returns text to API/MCP callers.
The existing account-number scrubber and its post-condition remain in place.

## Setup — locally, never in a model conversation

Run in the application checkout using its Python environment:

```sh
python -m api.personal_redaction init
```

This creates `data/privacy/redaction.json` (ignored, mode 0600), refuses to
overwrite an existing file, and prints no personal values. Open that file in
your local editor. Add:

- `names`: your full name, initials/aliases and other household names as they
  occur in documents. Case, punctuation, spacing and line-wrap differences are
  handled; genuinely different spellings need their own entries.
- `addresses`: complete address variants and their individual lines, including
  unit and city/province/postal lines. Include old addresses still on documents.
  Different abbreviations (Road vs Rd, for example) need separate entries.
- Set `reviewed` to `true` only after you have checked these against your
  documents locally. Leave `version` at `1`.

Keep both arrays nonempty. Use complete identifying phrases, not common words
such as a standalone street name: a configured literal is removed everywhere,
including transaction descriptions, and overly broad entries damage utility.
Do not add actual account/routing/card numbers to this file; the numeric
scrubber already handles them. Do not store the profile under intake, inbox,
documents or attachment roots where another tool can read/register it.

```sh
chmod 600 data/privacy/redaction.json
python -m api.personal_redaction check
```

The check validates structure, permissions and the review flag, **not** whether
you listed every identity. Never ask a model to read this file or raw sources.
`Q_CORE_PRIVACY_PROFILE_PATH` overrides the default; set an absolute path if
the live app and development checkout differ. Profile edits are read on each
extraction and need no restart. A changed environment path needs an API restart.
No new MCP parameters or database migration are required.

## Behavior and boundaries

- Missing, malformed, symlinked, unreviewed or group/world-readable profile:
  HTTP 422 `personal_redaction_required`, no document text. `allow_partial`
  cannot bypass this guard. There is no model-facing profile-writing endpoint.
- Known literals are replaced deterministically with `[REDACTED]`. Explicitly
  labelled names are removed throughout the document, including repeated page
  headers. Recognizable Canadian/US postal blocks and addressees are removed.
  Ambiguous address sections, detached postal lines, or transaction values
  inside an apparent address block cause a refusal for local review.
- Email addresses are removed. The prior numeric guard still masks phone/card
  numbers. Safe alphanumeric transaction references remain; ambiguous numeric
  references continue to be masked. This change does not relax that policy.
- Already-masked cards that expose a numeric prefix and last four are reduced
  to last-four-only (stars, Xs and bullet masks are recognized). URLs containing
  a query or fragment are removed entirely, including account-key parameters
  from printed browser headers. Ordinary merchant names are unchanged.
- Complete calendar dates (ISO, numeric slash dates with four-digit years,
  and English month/day/year dates including line wraps) and masked last-four
  tokens bound numeric runs. This prevents a continuation-header year or a
  following transaction date from being swallowed into an account number.
  Both the scrubber and survivor check use these boundaries across the whole
  document; wrapped accounts still scrub. Ambiguous numeric references keep
  the existing conservative thresholds, even when that reduces dedupe utility.
- Address detection also handles bounded word-per-line PDF blocks and split
  postal codes. Complete blocks are recognized before literal masking so a
  profile entry for just the street cannot detach its remaining city/postal
  lines. Suspicious blocks containing transaction amounts are refused.
- Recognized Bank A print-production header/footer blocks are removed, including
  short unexplained codes. Matching is format-specific, not a blanket removal
  of short numbers: transaction references, merchant numbers, dates, amounts,
  page counts and labelled account last-four remain. An unfamiliar block with
  the same print-code marker is refused for local review rather than broadly
  deleting nearby financial content. CSV cells do not use this layout rule.
- Bank C CSV `Note` is always suppressed. `Payee` stays the merchant field;
  date/type/amount survive. Generic date/description/amount CSV and recognized
  personal columns are supported. Unknown/duplicate/misaligned headers or an
  ambiguous `Payee` column are refused rather than guessed. Quotes and embedded
  newlines are handled by the CSV parser.
- Extraction logs contain counts and request IDs, not source text or filenames.
  Error messages never quote personal values. Original registered documents
  remain local plaintext by existing design.

Model-visible persistence also refuses account-, routing-, card- and SIN-shaped
runs in transaction descriptions, entity text/attributes, reminder text,
merchant rules, forecast notes, document titles and Jyra text. It refuses rather
than silently masks a user-authored value. Attachment readable text and
filenames are likewise refused when unsafe.

Registered document metadata never returns the raw stored path. Use
`extract_registered_document_text(document_id)` to read the stored copy through
the same server-side scrubber. The path remains inside the database for server
file management; it is not a client capability.

This is not an anonymity system. Merchant names, amounts, dates and the last
four digits intentionally remain useful, and a filesystem-capable local agent
could ignore project instructions and open `intake/` directly. The supported
agent workflow is part of the boundary: use the project skills and extraction
tools, stop on refusals, and never use shell/file reads as a fallback.

This is **not universal name detection**. A previously unknown, unlabelled
person in free text may look exactly like a merchant. The reviewed profile is
load-bearing; unsupported layouts or newly arriving identities require local
review. It is not honest to claim arbitrary names can be caught with regex.
Financial evaluation packets still require local privacy review. Do not mark
that attestation automatically because the API returned 200.

Redaction/profile changes can change canonical transaction descriptions and
therefore exact dedupe keys. Do not bulk reimport historical documents solely
to test this change. Use the offline evaluator and source-verified fixtures
first; deployment does not rewrite existing ledger rows or private baselines.

## Verification

`api/tests/test_personal_redaction.py` uses invented identities to cover
multi-page PDFs, repeated headers, postal blocks, CSV Note, amount/date/merchant
preservation, deterministic output, malformed/private profiles and fail-closed
API behavior. The MCP round trip verifies the same boundary through the real
tool. These tests do not certify the operator's real profile or every possible
future document layout.
