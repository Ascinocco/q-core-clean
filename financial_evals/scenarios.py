"""Synthetic sequences with explicit source evidence and known outcomes."""


def synthetic_scenarios():
    def row(description='CAFE', cents=-500, day='2026-06-15'):
        return {'txn_date': day, 'description': description, 'amount_cents': cents}

    def batch(rows, account='account-a', start='2026-06-01', end='2026-06-30'):
        return {'account_id': account, 'period_start': start, 'period_end': end, 'transactions': rows}

    def ledger(rows, account='account-a'):
        return [{'account_id': account, **r} for r in rows]

    one = row()
    other = row('SHELL', -3000)
    early = batch([one], end='2026-06-15')
    late = batch([one, other], start='2026-06-15')
    return [
        {'id': 'exact-reimport', 'imports': [batch([one]), batch([one])], 'expected': ledger([one])},
        {'id': 'identical-purchases-in-one-source', 'imports': [batch([one, one])], 'expected': ledger([one, one])},
        {'id': 'identical-purchases-reimport', 'imports': [batch([one, one]), batch([one, one])], 'expected': ledger([one, one])},
        {'id': 'overlap-forward', 'imports': [early, late], 'expected': ledger([one, other])},
        {'id': 'overlap-reversed', 'imports': [late, early], 'expected': ledger([one, other])},
        {'id': 'overlap-higher-multiplicity', 'imports': [early, batch([one, one], start='2026-06-15')], 'expected': ledger([one, one])},
        {'id': 'different-accounts', 'imports': [batch([one]), batch([one], account='account-b')],
         'expected': ledger([one]) + ledger([one], 'account-b')},
        {'id': 'different-amounts', 'imports': [batch([one, row(cents=-501)])], 'expected': ledger([one, row(cents=-501)])},
        {'id': 'different-dates', 'imports': [batch([one, row(day='2026-06-16')])], 'expected': ledger([one, row(day='2026-06-16')])},
        {'id': 'offsetting-errors-must-not-cancel', 'imports': [batch([row(cents=-500), row(cents=500)])],
         'expected': ledger([row(cents=-500), row(cents=500)])},
        # Same underlying purchase, known by the synthetic source author. The
        # production importer only sees descriptors, so this is a known miss.
        {'id': 'known-gap-descriptor-variation', 'imports': [batch([one]), batch([row('CAFE #12')])],
         'expected': ledger([one]), 'evidence': 'Synthetic fixture explicitly represents one purchase exported twice with different descriptors.'},
        # Two partial exports, each containing a different but identical-looking
        # purchase. This cannot be decided from the current payload alone.
        {'id': 'ambiguous-partial-exports', 'imports': [batch([one]), batch([one])],
         'expected': ledger([one, one]), 'evidence': 'Synthetic fixture explicitly represents two distinct purchases in separate partial exports.'},
    ]
