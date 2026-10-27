"""Explicit, additive notification migration; no startup execution or backfill."""

def migration_statements(dialect='sqlite'):
    if dialect not in ('sqlite', 'postgres'):
        raise ValueError('Unsupported dialect')
    statements = [
        '''CREATE TABLE IF NOT EXISTS stage6_notifications(notification_id TEXT PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES users(id), event_kind TEXT NOT NULL,
            dedupe_key TEXT NOT NULL, source_json TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
            path TEXT NOT NULL, created_at BIGINT NOT NULL, read_at BIGINT,
            test_only INTEGER NOT NULL CHECK(test_only=1), UNIQUE(customer_id,dedupe_key))''',
        '''CREATE TABLE IF NOT EXISTS stage6_notification_preferences(customer_id INTEGER NOT NULL REFERENCES users(id),
            event_kind TEXT NOT NULL, channel TEXT NOT NULL CHECK(channel IN ('email','push','line')),
            enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), updated_at BIGINT NOT NULL,
            PRIMARY KEY(customer_id,event_kind,channel))''',
        '''CREATE TABLE IF NOT EXISTS stage6_destinations(destination_id TEXT PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES users(id), channel TEXT NOT NULL CHECK(channel IN ('email','push','line')),
            address TEXT NOT NULL, address_hash TEXT NOT NULL, installation TEXT, binding_hash TEXT,
            platform TEXT, verified_at BIGINT NOT NULL, revoked_at BIGINT)''',
        '''CREATE UNIQUE INDEX IF NOT EXISTS stage6_active_destination ON stage6_destinations(channel,address_hash) WHERE revoked_at IS NULL''',
        '''CREATE UNIQUE INDEX IF NOT EXISTS stage6_active_installation ON stage6_destinations(installation) WHERE channel='push' AND revoked_at IS NULL''',
        '''CREATE TABLE IF NOT EXISTS stage6_destination_proofs(proof_id TEXT PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES users(id), channel TEXT NOT NULL CHECK(channel IN ('email','line')),
            address TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, expires_at BIGINT NOT NULL,
            created_at BIGINT NOT NULL, consumed_at BIGINT)''',
        '''CREATE TABLE IF NOT EXISTS stage6_line_invitations(invitation_id TEXT PRIMARY KEY,
            webhook_event_id TEXT NOT NULL UNIQUE, line_user_id TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE, expires_at BIGINT NOT NULL, consumed_at BIGINT)''',
        '''CREATE TABLE IF NOT EXISTS stage6_line_webhooks(webhook_event_id TEXT PRIMARY KEY,
            request_hash TEXT NOT NULL, recorded_at BIGINT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS stage6_line_link_tokens(invitation_id TEXT PRIMARY KEY
            REFERENCES stage6_line_invitations(invitation_id), link_token_hash TEXT NOT NULL UNIQUE,
            created_at BIGINT NOT NULL, expires_at BIGINT NOT NULL,
            delivery_state TEXT NOT NULL CHECK(delivery_state IN ('prepared','accepted','failed','unknown')))''',
        '''CREATE TABLE IF NOT EXISTS stage6_notification_outbox(delivery_id TEXT PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES users(id), notification_id TEXT REFERENCES stage6_notifications(notification_id),
            destination_id TEXT REFERENCES stage6_destinations(destination_id), proof_id TEXT REFERENCES stage6_destination_proofs(proof_id),
            channel TEXT NOT NULL CHECK(channel IN ('email','push','line')), private_payload_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('queued','claimed','sending','accepted','provider_accepted','failed','unknown','suppressed')),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0), due_at BIGINT NOT NULL,
            claim_hash TEXT, claimed_at BIGINT, provider_reference TEXT, outcome_code TEXT,
            created_at BIGINT NOT NULL, test_only INTEGER NOT NULL CHECK(test_only=1),
            UNIQUE(notification_id,destination_id), UNIQUE(proof_id))''',
        '''CREATE TABLE IF NOT EXISTS stage6_delivery_audit(audit_id TEXT PRIMARY KEY,
            delivery_id TEXT NOT NULL REFERENCES stage6_notification_outbox(delivery_id),
            state TEXT NOT NULL, outcome_code TEXT, recorded_at BIGINT NOT NULL)''',
        'CREATE INDEX IF NOT EXISTS stage6_delivery_due ON stage6_notification_outbox(state,due_at)',
    ]
    if dialect == 'sqlite':
        statements += [f"CREATE TRIGGER IF NOT EXISTS stage6_delivery_audit_no_{op.lower()} BEFORE {op} ON stage6_delivery_audit BEGIN SELECT RAISE(ABORT,'append_only'); END" for op in ('UPDATE','DELETE')]
    else:
        statements += ["CREATE OR REPLACE FUNCTION stage6_audit_reject_change() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'append_only'; END; $$ LANGUAGE plpgsql",
            'DROP TRIGGER IF EXISTS stage6_delivery_audit_immutable ON stage6_delivery_audit',
            'CREATE TRIGGER stage6_delivery_audit_immutable BEFORE UPDATE OR DELETE ON stage6_delivery_audit FOR EACH ROW EXECUTE FUNCTION stage6_audit_reject_change()']
    return statements
