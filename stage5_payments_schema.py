"""Additive test-only physical-card records. No migration executes on import."""


def migration_statements(dialect='sqlite'):
    from stage5_common import immutable_statements
    if dialect not in ('sqlite', 'postgres'):
        raise ValueError('Unsupported dialect')
    tables = {
        'stage5_physical_bills': '''bill_id TEXT PRIMARY KEY NOT NULL,
            invoice_id TEXT NOT NULL UNIQUE REFERENCES stage3_invoice_revisions(invoice_id),
            customer_id INTEGER NOT NULL REFERENCES users(id), period TEXT NOT NULL, timezone_name TEXT NOT NULL,
            due_date TEXT NOT NULL,
            source_hash TEXT NOT NULL, snapshot_json TEXT NOT NULL, amount_yen BIGINT NOT NULL CHECK(amount_yen>0),
            currency TEXT NOT NULL CHECK(currency='JPY'), review_reference TEXT NOT NULL,
            actor_user_id INTEGER NOT NULL REFERENCES users(id), recorded_at TEXT NOT NULL,
            execution_mode TEXT NOT NULL DEFAULT 'test' CHECK(execution_mode='test'),
            UNIQUE(customer_id,period,timezone_name)''',
        'stage5_card_consents': '''consent_id TEXT PRIMARY KEY NOT NULL,
            customer_id INTEGER NOT NULL REFERENCES users(id), terms_reference TEXT NOT NULL,
            provider_customer_reference TEXT NOT NULL, payment_method_reference TEXT NOT NULL,
            off_session INTEGER NOT NULL CHECK(off_session IN (0,1)), actor_user_id INTEGER NOT NULL REFERENCES users(id),
            recorded_at TEXT NOT NULL, execution_mode TEXT NOT NULL DEFAULT 'test' CHECK(execution_mode='test'),
            UNIQUE(customer_id,terms_reference)''',
        'stage5_card_consent_revocations': '''consent_id TEXT PRIMARY KEY NOT NULL REFERENCES stage5_card_consents(consent_id),
            reason TEXT NOT NULL, actor_user_id INTEGER NOT NULL REFERENCES users(id), recorded_at TEXT NOT NULL''',
        'stage5_payment_attempts': '''attempt_id TEXT PRIMARY KEY NOT NULL,
            bill_id TEXT NOT NULL REFERENCES stage5_physical_bills(bill_id),
            consent_id TEXT NOT NULL REFERENCES stage5_card_consents(consent_id),
            customer_id INTEGER NOT NULL REFERENCES users(id), amount_yen BIGINT NOT NULL CHECK(amount_yen>0),
            currency TEXT NOT NULL CHECK(currency='JPY'), off_session INTEGER NOT NULL CHECK(off_session IN (0,1)),
            provider_idempotency_key TEXT NOT NULL UNIQUE, actor_user_id INTEGER NOT NULL REFERENCES users(id),
            recorded_at TEXT NOT NULL, execution_mode TEXT NOT NULL DEFAULT 'test' CHECK(execution_mode='test')''',
        'stage5_payment_events': '''event_id TEXT PRIMARY KEY NOT NULL,
            attempt_id TEXT NOT NULL REFERENCES stage5_payment_attempts(attempt_id),
            sequence INTEGER NOT NULL CHECK(sequence>0), state TEXT NOT NULL,
            provider_revision INTEGER, snapshot_json TEXT NOT NULL, recorded_at TEXT NOT NULL,
            UNIQUE(attempt_id,sequence), UNIQUE(attempt_id,provider_revision)''',
        'stage5_payment_refunds': '''refund_id TEXT PRIMARY KEY NOT NULL,
            attempt_id TEXT NOT NULL REFERENCES stage5_payment_attempts(attempt_id),
            customer_id INTEGER NOT NULL REFERENCES users(id), amount_yen BIGINT NOT NULL CHECK(amount_yen>0),
            currency TEXT NOT NULL CHECK(currency='JPY'), reason TEXT NOT NULL,
            provider_idempotency_key TEXT NOT NULL UNIQUE, actor_user_id INTEGER NOT NULL REFERENCES users(id),
            recorded_at TEXT NOT NULL, execution_mode TEXT NOT NULL DEFAULT 'test' CHECK(execution_mode='test')''',
        'stage5_refund_events': '''event_id TEXT PRIMARY KEY NOT NULL,
            refund_id TEXT NOT NULL REFERENCES stage5_payment_refunds(refund_id),
            sequence INTEGER NOT NULL CHECK(sequence>0), state TEXT NOT NULL,
            provider_revision INTEGER, snapshot_json TEXT NOT NULL, recorded_at TEXT NOT NULL,
            UNIQUE(refund_id,sequence), UNIQUE(refund_id,provider_revision)''',
    }
    result = []
    for name, columns in tables.items():
        if dialect == 'sqlite':
            columns = columns.replace('CHECK(amount_yen>0)', "CHECK(amount_yen>0 AND typeof(amount_yen)='integer')")
        result.append('CREATE TABLE IF NOT EXISTS ' + name + ' (' + columns + ')')
        result.extend(immutable_statements(name, dialect))
    result.extend([
        'CREATE INDEX IF NOT EXISTS stage5_attempt_bill ON stage5_payment_attempts(bill_id)',
        'CREATE INDEX IF NOT EXISTS stage5_refund_payment ON stage5_payment_refunds(attempt_id)',
    ])
    return result
