"""Additive billing drafts. SQL generation only; no connections or live invoices."""


_TABLES = (
    ('stage3_agreements', '''
        agreement_id TEXT PRIMARY KEY NOT NULL,
        customer_id INTEGER NOT NULL REFERENCES users(id),
        actor_user_id INTEGER NOT NULL REFERENCES users(id),
        recorded_at TEXT NOT NULL, evidence_json TEXT NOT NULL, catalog_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','fixture_approved')),
        source_key TEXT NOT NULL UNIQUE,
        live_enabled INTEGER NOT NULL DEFAULT 0 CHECK(live_enabled=0)
    '''),
    ('stage3_charge_drafts', '''
        draft_id TEXT PRIMARY KEY NOT NULL,
        customer_id INTEGER NOT NULL REFERENCES users(id),
        physical_item_id TEXT REFERENCES business_items(physical_item_id),
        agreement_id TEXT REFERENCES stage3_agreements(agreement_id),
        source_event_id TEXT REFERENCES business_item_observations(observation_id),
        original_draft_id TEXT REFERENCES stage3_charge_drafts(draft_id),
        fee_kind TEXT NOT NULL CHECK(fee_kind IN ('work_bundle','sales_fee','expense','digital_monthly','physical_monthly','adjustment')),
        amount_net_yen INTEGER CHECK(amount_net_yen IS NULL OR typeof(amount_net_yen)='integer'),
        tax_rate_percent INTEGER CHECK(tax_rate_percent IS NULL OR tax_rate_percent IN (8,10)),
        tax_treatment TEXT CHECK(tax_treatment IS NULL OR tax_treatment IN ('exclusive','exempt','outside')),
        currency TEXT NOT NULL DEFAULT 'JPY' CHECK(currency='JPY'),
        occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
        evidence_reference TEXT NOT NULL, snapshot_json TEXT NOT NULL, reasons_json TEXT NOT NULL,
        source_key TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK(status IN ('draft','needs_review')),
        live_enabled INTEGER NOT NULL DEFAULT 0 CHECK(live_enabled=0),
        UNIQUE(customer_id,source_event_id,fee_kind),
        CHECK(amount_net_yen IS NULL OR amount_net_yen>=0 OR (fee_kind='adjustment' AND original_draft_id IS NOT NULL)),
        CHECK(fee_kind!='adjustment' OR original_draft_id IS NOT NULL)
    '''),
    ('stage3_invoice_revisions', '''
        invoice_id TEXT PRIMARY KEY NOT NULL,
        customer_id INTEGER NOT NULL REFERENCES users(id),
        period TEXT NOT NULL, timezone_name TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision>=1),
        previous_invoice_id TEXT REFERENCES stage3_invoice_revisions(invoice_id),
        content_hash TEXT NOT NULL, snapshot_json TEXT NOT NULL,
        recorded_at TEXT NOT NULL, source_key TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK(status IN ('draft','needs_review')),
        live_enabled INTEGER NOT NULL DEFAULT 0 CHECK(live_enabled=0),
        UNIQUE(customer_id,period,timezone_name,revision)
    '''),
)


def migration_statements(dialect='sqlite'):
    if dialect not in ('sqlite', 'postgres'):
        raise ValueError('Unsupported dialect')
    statements = []
    for name, columns in _TABLES:
        if dialect == 'postgres':
            columns = columns.replace("typeof(amount_net_yen)='integer'", 'amount_net_yen IS NOT NULL')
            columns = columns.replace('amount_net_yen INTEGER', 'amount_net_yen BIGINT')
        statements.append('CREATE TABLE IF NOT EXISTS ' + name + ' (' + columns + ')')
    statements.extend([
        'CREATE INDEX IF NOT EXISTS stage3_charge_customer_time ON stage3_charge_drafts(customer_id,occurred_at)',
        'CREATE INDEX IF NOT EXISTS stage3_charge_item ON stage3_charge_drafts(physical_item_id,customer_id)',
    ])
    if dialect == 'sqlite':
        for table, _ in _TABLES:
            for operation in ('UPDATE', 'DELETE'):
                statements.append('CREATE TRIGGER IF NOT EXISTS ' + table + '_no_' + operation.lower() +
                    ' BEFORE ' + operation + ' ON ' + table + " BEGIN SELECT RAISE(ABORT,'append-only financial draft'); END")
        statements.extend([
            '''CREATE TRIGGER IF NOT EXISTS stage3_charge_agreement_owner BEFORE INSERT ON stage3_charge_drafts
               WHEN NEW.agreement_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM stage3_agreements a
                   WHERE a.agreement_id=NEW.agreement_id AND a.customer_id=NEW.customer_id)
               BEGIN SELECT RAISE(ABORT,'agreement customer mismatch'); END''',
            '''CREATE TRIGGER IF NOT EXISTS stage3_charge_event_owner BEFORE INSERT ON stage3_charge_drafts
               WHEN NEW.source_event_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM business_item_observations e
                   WHERE e.observation_id=NEW.source_event_id AND e.customer_id=NEW.customer_id
                   AND e.physical_item_id=NEW.physical_item_id AND e.kind='shipped')
               BEGIN SELECT RAISE(ABORT,'shipping event customer mismatch'); END''',
            '''CREATE TRIGGER IF NOT EXISTS stage3_charge_original_owner BEFORE INSERT ON stage3_charge_drafts
               WHEN NEW.original_draft_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM stage3_charge_drafts d
                   WHERE d.draft_id=NEW.original_draft_id AND d.customer_id=NEW.customer_id
                   AND (d.physical_item_id=NEW.physical_item_id OR (d.physical_item_id IS NULL AND NEW.physical_item_id IS NULL)))
               BEGIN SELECT RAISE(ABORT,'correction customer or item mismatch'); END''',
            '''CREATE TRIGGER IF NOT EXISTS stage3_invoice_previous_owner BEFORE INSERT ON stage3_invoice_revisions
               WHEN NEW.previous_invoice_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM stage3_invoice_revisions i
                   WHERE i.invoice_id=NEW.previous_invoice_id AND i.customer_id=NEW.customer_id
                   AND i.period=NEW.period AND i.timezone_name=NEW.timezone_name AND i.revision=NEW.revision-1)
               BEGIN SELECT RAISE(ABORT,'invoice previous revision mismatch'); END''',
        ])
    else:
        statements.append("CREATE OR REPLACE FUNCTION stage3_billing_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'append-only financial draft'; END $$")
        for table, _ in _TABLES:
            statements.append('DROP TRIGGER IF EXISTS ' + table + '_immutable ON ' + table)
            statements.append('CREATE TRIGGER ' + table + '_immutable BEFORE UPDATE OR DELETE ON ' + table + ' FOR EACH ROW EXECUTE FUNCTION stage3_billing_immutable()')
        statements.append('''CREATE OR REPLACE FUNCTION stage3_billing_reference_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            IF NEW.agreement_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM stage3_agreements a WHERE a.agreement_id=NEW.agreement_id AND a.customer_id=NEW.customer_id) THEN RAISE EXCEPTION 'agreement customer mismatch'; END IF;
            IF NEW.source_event_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM business_item_observations e WHERE e.observation_id=NEW.source_event_id AND e.customer_id=NEW.customer_id AND e.physical_item_id=NEW.physical_item_id AND e.kind='shipped') THEN RAISE EXCEPTION 'shipping event customer mismatch'; END IF;
            IF NEW.original_draft_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM stage3_charge_drafts d WHERE d.draft_id=NEW.original_draft_id AND d.customer_id=NEW.customer_id AND d.physical_item_id IS NOT DISTINCT FROM NEW.physical_item_id) THEN RAISE EXCEPTION 'correction customer or item mismatch'; END IF;
            RETURN NEW; END $$''')
        statements.append('DROP TRIGGER IF EXISTS stage3_charge_reference_guard ON stage3_charge_drafts')
        statements.append('CREATE TRIGGER stage3_charge_reference_guard BEFORE INSERT ON stage3_charge_drafts FOR EACH ROW EXECUTE FUNCTION stage3_billing_reference_guard()')
        statements.append('''CREATE OR REPLACE FUNCTION stage3_invoice_reference_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            IF NEW.previous_invoice_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM stage3_invoice_revisions i WHERE i.invoice_id=NEW.previous_invoice_id AND i.customer_id=NEW.customer_id AND i.period=NEW.period AND i.timezone_name=NEW.timezone_name AND i.revision=NEW.revision-1) THEN RAISE EXCEPTION 'invoice previous revision mismatch'; END IF; RETURN NEW; END $$''')
        statements.append('DROP TRIGGER IF EXISTS stage3_invoice_reference_guard ON stage3_invoice_revisions')
        statements.append('CREATE TRIGGER stage3_invoice_reference_guard BEFORE INSERT ON stage3_invoice_revisions FOR EACH ROW EXECUTE FUNCTION stage3_invoice_reference_guard()')
    return statements
