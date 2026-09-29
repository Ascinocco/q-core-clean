# Cash runway and property costs

Open `/ui/forecast` on the loopback API. GET `/forecast/projection` is a
read-only scenario; the page never saves changes. `get_cash_forecast` provides
the same report through MCP. No bank feed, automatic matching, financial ledger
write, payment execution, interest-rate advice or model service is involved.

## Managing assumptions

Use `get_forecast_plan`, then `replace_forecast_plan` with `payload` containing
the full `plan`, `expected_revision`, `actor`, and a meaningful `note`. Initial
creation uses a null revision. Only persist operator-authorized changes; a
scenario request is not permission to replace a plan. A stale revision refuses
with 409; reread rather than blindly retry. HTTP equivalents are authenticated
GET/PUT `/forecast/plan`. Previous revisions remain under private ignored
`data/forecast/`, with a process-safe file lock and atomic current-pointer
replacement. Files are 0600. `Q_CORE_FORECAST_DIR` overrides the directory.
The plan is separate from the financial ledger; labels are local plan keys,
not database foreign keys. No actual transactions are invented or reclassified.

The bounded schema is `api/forecast_model.py`:

- `accounts`: key, label, kind (`cash` or `debt`), balance_cents (null for unknown
  debt), as_of (end-of-day date), evidence. Cash snapshots must share a date
  and have known balances. Positive debt balances mean amounts owed, not cash.
- `rules`: key, label, kind (`income`, `expense`, `transfer`, `debt_payment`),
  account, optional destination, nonnegative integer amount_cents or null,
  cadence (`monthly`, `interval`, `once`, `daily_budget`), days (1..31;
  31 means month end), interval_days, start, optional end, weekend (`none`,
  `previous`, `next`), confidence (`confirmed`, `estimated`, `unknown`),
  property_label, evidence. Null amounts must be unknown and remain listed as
  missing obligations, never silently represented as known zero payments.
- `runway_account`, `runway_growth_target_cents`, optional `snowball_rule`
  naming a debt payment, and explicit `gaps` for missing coverage/assumptions.

For daily_budget, amount_cents is the monthly envelope, spread across calendar
days using exact integer allocation. Partial months receive only their share.
Interval schedules use their start as an anchor, never reset each month.
Weekend shifts do not implement bank holidays. Month-end days clamp to the
month's last day. End dates constrain nominal due dates before shifting.

Never read raw statements or the redaction profile to refresh balances. Follow
the statement-intake privacy rules and use approved server-scrubbed evidence.
Preserve each source date and evidence note. A new snapshot must be reconciled
with its source; never label a calculated projection as a verified balance.
Later imported transactions are NOT blended into the forecast automatically:
doing so without resolving expected/actual matches could double-count them.

## Accounting and interpretation

Transfers reduce one cash account and increase another, with no income/cost.
Card purchases increase debt and count as running costs, not immediate bank
cash outflow. Debt payments reduce cash and debt, not running costs again.
Unfunded card spending can leave cash apparently high while debt grows. Unknown
debt balances stay unknown; there is no claimed combined net-worth total.
Balances on different dates are anchored independently: events on/before an
account's end-of-day snapshot never change that balance a second time.

This is a household cash-cost view, not accounting profit. Shared costs
remain unallocated until confirmed. Monthly tables mark partial months. The runway
account's growth target is a comparison, not a withdrawal, savings reservation or guaranteed
result. The low-water mark processes obligations before same-day income as a
conservative intraday scenario; actual posting order can differ.

Optional `snowball_monthly_cents` supplies a temporary monthly payment
allowance. `extra_payment_cents` with `extra_payment_date` models a single
additional payment. Neither persists nor automatically allocates apparent
surplus. Horizon is 1..365 days beyond local today; anchor-to-end is capped at
730 days so obsolete snapshots eventually require refresh.

## Initial operator setup

Record the operator's own framing when setting up a plan: which account is
the runway (and whether it is a reserve or a working account), how payday
transfers fund bills, which properties have distinct cash costs, payment
cadences, and which debt a snowball targets. Anything not confirmed goes into
`gaps`. Actual personal values/snapshots are configured privately, never
seeded in source or tests. The forecast is incomplete until those gaps are
resolved.
