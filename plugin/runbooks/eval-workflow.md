# Invoking financial intake and evaluations

The skills ship in the q-core plugin (`plugin/skills/`, runbooks/plugin.md),
installed on every machine and kept current by its own update hook. A remote MCP
connection exposes tools, not skills: the plugin is what teaches another machine
the procedure. Restarting the API does not.

## Plain-language requests

- “I added `intake/example_batch/`; process it through our financial audit system.”
  Uses `statement-intake`: list only filenames in that subtree, scrub each file
  server-side, extract source-bound rows, preview, resolve overlaps, commit and
  verify. No separate upload is needed for files already on this machine.
  Stop on a scrubber refusal, unclear account, source conflict or unresolved overlap.
- “Audit/preview this folder without importing.” Same extraction and preview,
  but no commit, account creation, rule change or other live mutation.
- “Run the financial evals.” Uses `financial-evals`: offline classification,
  ledger replay and synthetic source-identity checks. No new statement is needed.
- “Run fresh extraction trials” requires approved packets and isolated sessions.
  “Compare these two eval runs” uses the existing dataset-bound comparator.

## Routine regression run

Use the app's `.venv/bin/python` from the checkout being evaluated. Choose a new
private run directory under `data/evals/`; never overwrite previous results.
Use the accepted baseline in the app checkout's `data/evals/`. It has reviewed
classifications; unknown labels remain unknown. Do not silently switch baselines.
Worktrees should use absolute paths to the app's private artifacts.

```sh
python -m financial_evals classify --reference data/evals/intake-flow-v1/ledger.json --output data/evals/RUN/classification.json
python -m financial_evals replay --reference data/evals/intake-flow-v1/ledger.json --output data/evals/RUN/replay.json
python -m financial_evals source-dedupe --output data/evals/RUN/source-dedupe.json
python -m financial_evals publish --report data/evals/RUN/classification.json --corpus development --summary-dir data/evals/published
python -m financial_evals publish --report data/evals/RUN/replay.json --corpus development --summary-dir data/evals/published
python -m financial_evals publish --report data/evals/RUN/source-dedupe.json --corpus synthetic --summary-dir data/evals/published
```

`RUN` is a new unique name, not a literal reusable directory. Classification uses
the frozen rules by default; test candidate rules via `--rules candidate-rules.json`.
For current live rules, capture GET-only while edits are idle, select its rules
locally without printing descriptions, and keep the accepted reference labels fixed.
Never replace the accepted baseline just because today's DB changed.

For optional **saved-submission** end-to-end replay, use the paired
`intake-flow-v2/suite.json` and `intake-flow-v2/submissions.json` with `intake-eval`.
This tests current scoring/import behavior against old extraction outputs; it
does not test fresh extraction under changed instructions. Follow
[intake-flow evaluations](intake-flow-evaluations.md) to prepare fresh packets,
freeze a suite and run actual isolated trials.

```sh
python -m financial_evals compare --before data/evals/BEFORE/classification.json --after data/evals/RUN/classification.json --output data/evals/RUN/comparison.json
```

Different kinds or dataset hashes refuse comparison. Retain the comparator output
privately; the page displays metrics and hashes, not an inferred regression verdict.
Exit zero from a scoring command means it ran, not that it passed. Classification
is displayed as **measured**, because no binary acceptance threshold is defined.
Scenario `passed` is a count, whereas end-to-end `passed` is a 0/1 verdict.

## Read-only page and privacy

Open `http://127.0.0.1:8420/ui/evals`. It lists explicitly published summaries,
filterable by corpus and test type. Expand a run for metrics, failure counts,
report/dataset/implementation hashes and commit. No upload, run, mutation or
private-file download endpoint exists. Full transaction-level failure traces
stay in their original private reports; the eval skill can investigate approved
evidence when requested. No raw statements or unreviewed text are made visible.

The CLI whitelist publishes only numeric/boolean metrics and validated hashes,
timestamps and fixed labels, not source text, names, paths, IDs, prompts or free
text errors. Publication is explicit and immutable, 0600 on disk. The API reads
only validated, bounded, regular summary files, not nested eval folders. It
revalidates the whitelist and withholds malformed summaries. It does not recompute
or cryptographically attest their scores; report hashes associate the originals.

Default directory: `data/evals/published`, configurable with
`Q_CORE_EVAL_SUMMARIES_DIR`. Like spending, these two GET routes are intentionally
unauthenticated behind loopback: `/ui/evals`, `/evals/runs`. Do not expose the server
externally. Private artifacts must never be placed in static assets or committed.

Corpus labels are operator declarations. Use development for known/tuned data,
synthetic for authored scenarios, and holdout only for genuinely unseen,
independently reviewed data withheld from tuning.
