"""Reviewable, additive SQL for manual/test marketplace records. No connector."""

_TABLES = (
    ('stage4_channel_accounts', '''account_id TEXT PRIMARY KEY NOT NULL,
        channel TEXT NOT NULL, display_name TEXT NOT NULL, actor_user_id INTEGER NOT NULL REFERENCES users(id),
        recorded_at TEXT NOT NULL, evidence_reference TEXT NOT NULL, source_key TEXT NOT NULL UNIQUE,
        live_enabled INTEGER NOT NULL DEFAULT 0 CHECK(live_enabled=0)'''),
    ('stage4_channel_reviews', '''review_id TEXT PRIMARY KEY NOT NULL,
        account_id TEXT NOT NULL REFERENCES stage4_channel_accounts(account_id), revision INTEGER NOT NULL CHECK(revision>0),
        capabilities_json TEXT NOT NULL, evidence_reference TEXT NOT NULL, actor_user_id INTEGER NOT NULL REFERENCES users(id),
        recorded_at TEXT NOT NULL, source_key TEXT NOT NULL UNIQUE,
        live_enabled INTEGER NOT NULL DEFAULT 0 CHECK(live_enabled=0), UNIQUE(account_id,revision)'''),
    ('stage4_channel_listings', '''listing_id TEXT PRIMARY KEY NOT NULL,
        physical_item_id TEXT NOT NULL REFERENCES business_items(physical_item_id), customer_id INTEGER NOT NULL REFERENCES users(id),
        ownership_assignment_id TEXT NOT NULL REFERENCES business_item_assignments(assignment_id),
        version_id TEXT NOT NULL REFERENCES stage4_listing_versions(version_id), account_id TEXT NOT NULL REFERENCES stage4_channel_accounts(account_id),
        external_listing_id TEXT NOT NULL, approved_snapshot_json TEXT NOT NULL, occurred_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL, actor_user_id INTEGER NOT NULL REFERENCES users(id), evidence_reference TEXT NOT NULL,
        source_key TEXT NOT NULL UNIQUE, payload_hash TEXT NOT NULL,
        mode TEXT NOT NULL DEFAULT 'test_only' CHECK(mode='test_only'), UNIQUE(account_id,external_listing_id)'''),
    ('stage4_channel_orders', '''order_id TEXT PRIMARY KEY NOT NULL,
        listing_id TEXT NOT NULL REFERENCES stage4_channel_listings(listing_id),
        account_id TEXT NOT NULL REFERENCES stage4_channel_accounts(account_id), external_order_id TEXT NOT NULL,
        physical_item_id TEXT NOT NULL REFERENCES business_items(physical_item_id), customer_id INTEGER NOT NULL REFERENCES users(id),
        sale_gross_yen INTEGER NOT NULL CHECK(sale_gross_yen BETWEEN 1 AND 9000000000000 AND typeof(sale_gross_yen)='integer'),
        occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL, actor_user_id INTEGER NOT NULL REFERENCES users(id),
        evidence_reference TEXT NOT NULL, raw_snapshot_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
        initial_status TEXT NOT NULL CHECK(initial_status IN ('accepted','conflict')),
        conflict_reason TEXT, conflicting_order_id TEXT REFERENCES stage4_channel_orders(order_id),
        source_key TEXT NOT NULL UNIQUE, mode TEXT NOT NULL DEFAULT 'test_only' CHECK(mode='test_only'),
        UNIQUE(account_id,external_order_id)'''),
    ('stage4_order_claims', '''physical_item_id TEXT PRIMARY KEY NOT NULL REFERENCES business_items(physical_item_id),
        order_id TEXT NOT NULL UNIQUE REFERENCES stage4_channel_orders(order_id)'''),
    ('stage4_order_corrections', '''correction_id TEXT PRIMARY KEY NOT NULL,
        order_id TEXT NOT NULL REFERENCES stage4_channel_orders(order_id), revision INTEGER NOT NULL CHECK(revision>0),
        sale_gross_yen INTEGER NOT NULL CHECK(sale_gross_yen BETWEEN 1 AND 9000000000000 AND typeof(sale_gross_yen)='integer'),
        reason TEXT NOT NULL, occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL, actor_user_id INTEGER NOT NULL REFERENCES users(id),
        evidence_reference TEXT NOT NULL, source_key TEXT NOT NULL UNIQUE, UNIQUE(order_id,revision)'''),
    ('stage4_stop_tasks', '''task_id TEXT PRIMARY KEY NOT NULL,
        order_id TEXT REFERENCES stage4_channel_orders(order_id),
        listing_id TEXT NOT NULL REFERENCES stage4_channel_listings(listing_id),
        created_at TEXT NOT NULL, reason TEXT NOT NULL, reason_key TEXT NOT NULL UNIQUE,
        UNIQUE(order_id,listing_id)'''),
    ('stage4_stop_events', '''event_id TEXT PRIMARY KEY NOT NULL, task_id TEXT NOT NULL REFERENCES stage4_stop_tasks(task_id),
        revision INTEGER NOT NULL CHECK(revision>0), status TEXT NOT NULL CHECK(status IN ('confirmed_stopped','failed','retry_requested')),
        evidence_reference TEXT NOT NULL, occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
        actor_user_id INTEGER NOT NULL REFERENCES users(id), source_key TEXT NOT NULL UNIQUE, UNIQUE(task_id,revision)'''),
    ('stage4_order_tasks', '''task_id TEXT PRIMARY KEY NOT NULL, order_id TEXT NOT NULL REFERENCES stage4_channel_orders(order_id),
        kind TEXT NOT NULL CHECK(kind IN ('customer_response','packing','shipping','conflict_resolution')),
        created_at TEXT NOT NULL, UNIQUE(order_id,kind)'''),
    ('stage4_order_task_events', '''event_id TEXT PRIMARY KEY NOT NULL, task_id TEXT NOT NULL REFERENCES stage4_order_tasks(task_id),
        revision INTEGER NOT NULL CHECK(revision>0), status TEXT NOT NULL CHECK(status IN ('pending','working','done','blocked')),
        assignee_user_id INTEGER REFERENCES users(id), deadline_at TEXT, note TEXT NOT NULL,
        evidence_reference TEXT NOT NULL, occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
        actor_user_id INTEGER NOT NULL REFERENCES users(id), source_key TEXT NOT NULL UNIQUE, UNIQUE(task_id,revision)'''),
    ('stage4_order_shipments', '''shipment_id TEXT PRIMARY KEY NOT NULL, order_id TEXT NOT NULL UNIQUE REFERENCES stage4_channel_orders(order_id),
        tracking_reference TEXT NOT NULL, occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
        actor_user_id INTEGER NOT NULL REFERENCES users(id), evidence_reference TEXT NOT NULL,
        payload_hash TEXT NOT NULL, draft_result_json TEXT NOT NULL, source_key TEXT NOT NULL UNIQUE'''),
    ('stage4_order_returns', '''return_id TEXT PRIMARY KEY NOT NULL, order_id TEXT NOT NULL UNIQUE REFERENCES stage4_channel_orders(order_id),
        kind TEXT NOT NULL CHECK(kind IN ('returned','sale_cancelled')), reason TEXT NOT NULL,
        occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL, actor_user_id INTEGER NOT NULL REFERENCES users(id),
        evidence_reference TEXT NOT NULL, source_key TEXT NOT NULL UNIQUE, payload_hash TEXT NOT NULL'''),
    ('stage4_legacy_projections', '''projection_id TEXT PRIMARY KEY NOT NULL,
        physical_item_id TEXT NOT NULL REFERENCES business_items(physical_item_id), merchandise_id INTEGER NOT NULL REFERENCES merchandise(id),
        customer_id INTEGER NOT NULL REFERENCES users(id), actor_user_id INTEGER NOT NULL REFERENCES users(id),
        kind TEXT NOT NULL, occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL, before_json TEXT NOT NULL,
        after_json TEXT NOT NULL, evidence_reference TEXT NOT NULL, source_key TEXT NOT NULL UNIQUE'''),
)


def migration_statements(dialect='sqlite'):
    if dialect not in ('sqlite', 'postgres'):
        raise ValueError('Unsupported dialect')
    statements = []
    for table, columns in _TABLES:
        if dialect == 'postgres':
            columns = columns.replace("typeof(sale_gross_yen)='integer'", 'sale_gross_yen IS NOT NULL')
            columns = columns.replace('sale_gross_yen INTEGER', 'sale_gross_yen BIGINT')
        statements.append('CREATE TABLE IF NOT EXISTS ' + table + ' (' + columns + ')')
    statements += [
        'CREATE INDEX IF NOT EXISTS stage4_channel_listing_item ON stage4_channel_listings(physical_item_id)',
        'CREATE INDEX IF NOT EXISTS stage4_channel_order_customer ON stage4_channel_orders(customer_id,occurred_at)',
    ]
    guards = (
        ('stage4_channel_listings', '''NOT EXISTS(SELECT 1 FROM stage4_listing_versions v
            WHERE v.version_id=NEW.version_id AND v.physical_item_id=NEW.physical_item_id
            AND v.customer_id=NEW.customer_id AND v.ownership_assignment_id=NEW.ownership_assignment_id)'''),
        ('stage4_channel_orders', '''NOT EXISTS(SELECT 1 FROM stage4_channel_listings l
            WHERE l.listing_id=NEW.listing_id AND l.account_id=NEW.account_id
            AND l.physical_item_id=NEW.physical_item_id AND l.customer_id=NEW.customer_id)'''),
        ('stage4_order_claims', '''NOT EXISTS(SELECT 1 FROM stage4_channel_orders o
            WHERE o.order_id=NEW.order_id AND o.physical_item_id=NEW.physical_item_id AND o.initial_status='accepted')'''),
        ('stage4_stop_tasks', '''NEW.order_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM stage4_channel_orders o
            JOIN stage4_channel_listings l ON l.physical_item_id=o.physical_item_id
            WHERE o.order_id=NEW.order_id AND l.listing_id=NEW.listing_id AND o.customer_id=l.customer_id)'''),
    )
    for table, condition in guards:
        if dialect == 'sqlite':
            statements.append(f"CREATE TRIGGER IF NOT EXISTS {table}_scope_insert BEFORE INSERT ON {table} WHEN {condition} BEGIN SELECT RAISE(ABORT,'channel_scope_mismatch'); END")
        else:
            statements += [f"CREATE OR REPLACE FUNCTION {table}_scope_guard() RETURNS trigger AS $$ BEGIN IF {condition} THEN RAISE EXCEPTION 'channel_scope_mismatch'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql",
                f'DROP TRIGGER IF EXISTS {table}_scope_insert ON {table}',
                f'CREATE TRIGGER {table}_scope_insert BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION {table}_scope_guard()']
    for table, _ in _TABLES:
        if table == 'stage4_order_claims':
            continue
        if dialect == 'sqlite':
            statements += [f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'append_only'); END" for action in ('UPDATE', 'DELETE')]
        else:
            statements += ["CREATE OR REPLACE FUNCTION stage4_channels_reject_change() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'append_only'; END; $$ LANGUAGE plpgsql",
                f'DROP TRIGGER IF EXISTS {table}_immutable ON {table}',
                f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION stage4_channels_reject_change()']
    return statements
