# Audited financial metadata corrections

Use MCP `correct_statement_period` for a source-verified statement boundary repair,
or `reassign_transaction_statement` for source-table membership repairs. Neither
tool changes transaction IDs, dates, descriptions, amounts or classifications.
Reassignment requires active source/destination statements in the same account;
transaction dates need not fall inside the destination period.

Read the current records first. Supply the exact expected old values, a UUID
`correction_id`, an actor label, and a reason identifying the reviewed evidence.
Persist the request before sending it. Mutation and audit insertion are atomic.
Concurrent/stale requests, cross-account moves, no-ops and period collisions are
refused. The actor is caller-reported, not a separately authenticated identity.

Retry an uncertain request with the same ID and identical arguments. It returns
the original audit receipt without applying the mutation again; it does not prove
the record still has those values. Reusing an ID for different arguments fails.
Read back the live records and use `get_financial_correction` to verify the audit.
To undo an approved correction, make a new correction with a new ID and the
current expected values. There is no audit editing/deletion endpoint.
