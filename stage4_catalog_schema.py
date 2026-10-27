"""Additive catalog and test-order SQL. No connection or automatic migration."""


def migration_statements(dialect='sqlite'):
    if dialect not in ('sqlite', 'postgres'):
        raise ValueError('Unsupported database dialect')
    statements = [
        '''CREATE TABLE IF NOT EXISTS stage4_company_items(
            physical_item_id TEXT PRIMARY KEY REFERENCES business_items(physical_item_id),
            initial_title TEXT NOT NULL, purchase_cost_yen BIGINT CHECK(purchase_cost_yen IS NULL OR (typeof(purchase_cost_yen)='integer' AND purchase_cost_yen BETWEEN 0 AND 9000000000000)),
            created_at TEXT NOT NULL, actor_user_id INTEGER NOT NULL REFERENCES users(id),
            evidence_reference TEXT NOT NULL, execution_mode TEXT NOT NULL DEFAULT 'test' CHECK(execution_mode='test'))''',
        '''CREATE TABLE IF NOT EXISTS stage4_catalog_revisions(
            revision_id TEXT PRIMARY KEY NOT NULL, physical_item_id TEXT NOT NULL REFERENCES stage4_company_items(physical_item_id),
            revision_no INTEGER NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, item_condition TEXT NOT NULL,
            price_yen BIGINT NOT NULL CHECK(typeof(price_yen)='integer' AND price_yen BETWEEN 1 AND 9000000000000), currency TEXT NOT NULL DEFAULT 'JPY' CHECK(currency='JPY'),
            photo_refs_json TEXT NOT NULL, created_at TEXT NOT NULL, actor_user_id INTEGER NOT NULL REFERENCES users(id),
            evidence_reference TEXT NOT NULL, UNIQUE(physical_item_id,revision_no), UNIQUE(physical_item_id,revision_id))''',
        '''CREATE TABLE IF NOT EXISTS stage4_catalog_publications(
            physical_item_id TEXT PRIMARY KEY REFERENCES stage4_company_items(physical_item_id),
            revision_id TEXT NOT NULL, published_at TEXT NOT NULL,
            FOREIGN KEY(physical_item_id,revision_id) REFERENCES stage4_catalog_revisions(physical_item_id,revision_id))''',
        '''CREATE TABLE IF NOT EXISTS stage4_purchase_orders(
            order_id TEXT PRIMARY KEY NOT NULL, physical_item_id TEXT NOT NULL REFERENCES stage4_company_items(physical_item_id),
            revision_id TEXT NOT NULL, customer_id INTEGER NOT NULL REFERENCES users(id),
            amount_yen BIGINT NOT NULL CHECK(typeof(amount_yen)='integer' AND amount_yen BETWEEN 1 AND 9000000000000), custody_choice TEXT NOT NULL CHECK(custody_choice IN ('kaika','self')),
            consent_reference TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            reservation_record_id TEXT NOT NULL UNIQUE REFERENCES stage3_fund_records(record_id),
            execution_mode TEXT NOT NULL DEFAULT 'test' CHECK(execution_mode='test'),
            FOREIGN KEY(physical_item_id,revision_id) REFERENCES stage4_catalog_revisions(physical_item_id,revision_id))''',
        '''CREATE TABLE IF NOT EXISTS stage4_purchase_claims(
            physical_item_id TEXT PRIMARY KEY REFERENCES stage4_company_items(physical_item_id),
            order_id TEXT NOT NULL UNIQUE REFERENCES stage4_purchase_orders(order_id))''',
        '''CREATE TABLE IF NOT EXISTS stage4_catalog_events(
            event_id TEXT PRIMARY KEY NOT NULL, physical_item_id TEXT NOT NULL REFERENCES stage4_company_items(physical_item_id),
            order_id TEXT REFERENCES stage4_purchase_orders(order_id), event_no INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('published','reserved','cancelled','expired','paid_ownership_pending','confirmed','handed_over')),
            actor_user_id INTEGER NOT NULL REFERENCES users(id), occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
            evidence_reference TEXT NOT NULL, details_json TEXT NOT NULL, UNIQUE(physical_item_id,event_no))''',
        'CREATE INDEX IF NOT EXISTS stage4_orders_customer ON stage4_purchase_orders(customer_id,created_at)',
    ]
    if dialect=='postgres':
        for column in ('purchase_cost_yen','price_yen','amount_yen'):
            statements=[sql.replace("typeof("+column+")='integer' AND ",'') for sql in statements]
    for table in ('stage4_company_items', 'stage4_catalog_revisions', 'stage4_purchase_orders', 'stage4_catalog_events'):
        if dialect == 'sqlite':
            statements += [f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'append_only'); END"
                           for op in ('UPDATE', 'DELETE')]
        else:
            statements += ["""CREATE OR REPLACE FUNCTION stage4_catalog_reject_change() RETURNS trigger AS $$
                BEGIN RAISE EXCEPTION 'append_only'; END; $$ LANGUAGE plpgsql""",
                f'DROP TRIGGER IF EXISTS {table}_immutable ON {table}',
                f'''CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table}
                    FOR EACH ROW EXECUTE FUNCTION stage4_catalog_reject_change()''']
    return statements
