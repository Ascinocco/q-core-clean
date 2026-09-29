---
name: financial-evals
description: Run or compare q-core financial regression evals, assess extraction/classification/deduplication changes, and publish read-only results. Use for “run financial evals” or “did intake accuracy improve”; importing a new statement folder belongs to statement-intake, not this offline workflow.
---

# Financial evaluations

## Never — read first

- Never read raw intake documents or the private redaction profile into model context, even to diagnose an eval failure. New text must pass server-side extraction and explicit privacy approval before an extraction trial.
- Never give extraction trial agents the suite, reference answers, live tools, previous submissions, or reference parsers. Give only approved packets. Separate contexts are not OS sandboxes; disclose that limitation.
- Never repair a submission, adjust gold to make a trial pass, or call a replay of saved submissions a fresh extraction trial.
- Never import into the live ledger, create rules, or repair transactions as part of an eval request. Use the existing disposable runner; findings are proposals for a separately authorized change.
- Never call known development data a holdout, auto-approve a reference, or overwrite existing artifacts. `[REDACTED]` and `****1234` are privacy placeholders, not identity evidence or merchant names.

## Choose the workflow

Read `${CLAUDE_PLUGIN_ROOT}/runbooks/eval-workflow.md` for commands, current baseline locations, publication and interpretation. Read `${CLAUDE_PLUGIN_ROOT}/runbooks/intake-flow-evaluations.md` as well for fresh extraction trials or full intake scoring. Run from the project Python environment, not a new model API service.

- **“Run financial evals”**: run classification, reviewed ledger replay and current source-dedupe against the accepted frozen baseline; publish summaries and report their separate outcomes. Do not refresh labels from today's DB silently. Existing saved extraction submissions may be rescored, explicitly labeled as a saved-submission replay.
- **“Compare this change”**: preserve the reference, inputs and labels; use the current candidate rules/implementation. Compare compatible artifacts with the existing `compare` command. Mismatched datasets are a stop, not grounds to loosen the check.
- **“Run fresh extraction trials”**: use fresh isolated agent sessions only when authorized and available. Otherwise explain the missing capability; do not emulate a blind agent in a context that has seen gold. Rebuild packets for the current contract, preserve independently reviewed references, save unedited actual submissions, then score. Record actual tools/model (unknown if unverifiable) and isolation limitations.
- **“Process the new intake folder through the financial audit system”**: load `${CLAUDE_PLUGIN_ROOT}/skills/statement-intake/SKILL.md` and follow that production workflow. Evals neither upload nor import statements. A request just to audit or preview does not authorize a commit.

For new corpora, stop for missing privacy approval, source truth, account identity or category decisions. Agreement with the existing database or balancing arithmetic alone cannot establish source truth. Do not send raw content to the user via tool errors.

## Report and publish

Publish only through `python -m financial_evals publish`; never copy full reports into the published directory. The page is `/ui/evals`. Keep detailed evidence private under ignored `data/evals/`.

Report extraction exact rows and missing/extra rows separately from classification precision, coverage, abstentions and severe transfer/income errors. Report initial/repeat safety and unresolved-overlap refusal separately from resolved duplicate identity. CLI exit zero means execution completed, not that all measurements passed; inspect metrics. State the corpus, denominator and implementation with the result. Use `compare` for a regression verdict; stop and explain any incompatible or contradictory evidence.
